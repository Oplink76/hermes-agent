"""Append-only, external Markdown storage for concise agent session gists."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit

from agent.redact import redact_sensitive_text


_MAX_RECORDED_CHARS = 2_000
_MAX_EVIDENCE_CHARS = 500
_MAX_RECALL_RESULTS = 20
_MAX_RECALL_FILES = 31
_MAX_RECALL_GISTS = 100
_VAULT_LOCK_TIMEOUT_SECONDS = 5.0
_VAULT_LOCK_POLL_SECONDS = 0.05
_SAFE_GIST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_GIST_RE = re.compile(
    r"(?ms)^## (?P<header>[^\n]+)\n"
    r"<!-- gist_id: (?P<gist_id>[^<>\n]+) -->\n"
    r"(?P<body>.*?)(?=^## |\Z)"
)
_FIELD_RE = re.compile(r"(?m)^- (?P<name>[^:]+): (?P<value>.*)$")
_REQUIRED_FIELDS = (
    "Function",
    "Context",
    "Summary",
    "Reused",
    "Result",
    "Maturity",
    "Evidence",
    "Behavior",
    "Decisions",
    "Open loops",
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_PR_RE = re.compile(r"(?i)(?:PR\s*)?#\d+|\d+")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,199}")
_UNSAFE_IDENTIFIER_RE = re.compile(
    r"(?i)chain[_-]?of[_-]?thought|reasoning|deliberation|transcript|user:"
)
_EVIDENCE_KEYS = {
    "branch",
    "branch_name",
    "candidate_sha",
    "commit",
    "commit_sha",
    "deployment",
    "deployment_id",
    "pr",
    "pull_request",
    "release",
    "release_evidence",
    "repo",
    "repository",
    "repository_url",
    "review",
    "review_evidence",
    "reviewed_branch",
    "reviewed_commit",
    "rollback_target",
    "runtime_evidence",
    "sha",
    "smoke_result",
    "source_sha",
    "test_evidence",
    "test_result",
    "tests",
    "tests_run",
    "verification",
    "verification_summary",
}
_COMMIT_EVIDENCE_KEYS = {
    "candidate_sha",
    "commit",
    "commit_sha",
    "reviewed_commit",
    "sha",
    "source_sha",
}
_PR_EVIDENCE_KEYS = {"pr", "pull_request"}
_REPOSITORY_EVIDENCE_KEYS = {"repo", "repository", "repository_url"}

_log = logging.getLogger(__name__)


@dataclass
class SessionGist:
    """A bounded, redacted record appended by one agent handover."""

    gist_id: str
    occurred_at: datetime
    agent_id: str
    role: str
    function_id: str
    title: str
    context: str
    summary: str
    reused: str
    result: str
    maturity: str
    evidence: str
    behavior: str
    decisions: str
    open_loops: str


@dataclass(frozen=True)
class MemoryMatch:
    """Bounded historical evidence returned by advisory recall."""

    function_id: str
    title: str
    gist_id: str
    evidence: str
    snippet: str
    score: int


@dataclass(frozen=True)
class LintReport:
    """Deterministic result of validating history and rebuilding projections."""

    valid_entries: int
    invalid_entries: int
    functions: int


@dataclass(frozen=True)
class _ParsedGist:
    gist_id: str
    function_id: str
    title: str
    fields: dict[str, str]
    source: Path


def configured_vault_path(
    config: dict | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Return the explicitly configured shared vault, without a local fallback."""
    environment = os.environ if environ is None else environ
    environment_path = (environment.get("HERMES_AGENT_MEMORY_VAULT") or "").strip()
    if environment_path:
        return Path(environment_path).expanduser()

    if config is None:
        from hermes_cli.config import load_config

        config = load_config()
    agent_memory = config.get("agent_memory", {}) if isinstance(config, dict) else {}
    if not isinstance(agent_memory, dict) or agent_memory.get("enabled") is False:
        return None
    configured_path = str(agent_memory.get("vault_path") or "").strip()
    return Path(configured_path).expanduser() if configured_path else None


def remember_kanban_run(
    conn: Any,
    *,
    board: str,
    task_id: str,
    run_id: int | None,
    outcome: str,
    summary: str | None = None,
    transition_event_id: int | None = None,
) -> bool:
    """Append one functionality-first gist from durable Kanban records."""
    try:
        vault = configured_vault_path()
        if vault is None:
            return False
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            return False
        run = _kanban_run(conn, task_id, run_id)
        contract = _kanban_work_contract(conn, task["work_contract_id"])
        work = _functional_work(task, contract)
        if work is None:
            _log.warning(
                "skipping Agent Memory capture for board=%s task=%s: "
                "no stable functional boundary",
                board,
                task_id,
            )
            return False
        if transition_event_id is None:
            _log.warning(
                "skipping Agent Memory capture for board=%s task=%s: "
                "no transition event identity",
                board,
                task_id,
            )
            return False
        function_id = _function_id(work)
        recorded_run_id = int(run["id"]) if run is not None else run_id
        event = _kanban_event(conn, task_id, transition_event_id)
        occurred_at = datetime.fromtimestamp(
            int(run["ended_at"] or run["started_at"])
            if run is not None and (run["ended_at"] or run["started_at"])
            else int(task["completed_at"] or task["started_at"] or task["created_at"])
        )
        phase = (
            str(run["step_key"] or "") if run is not None
            else str(task["current_step_key"] or "")
        )
        summary_text = _transition_summary(
            outcome=outcome,
            status=str(task["status"]),
            phase=phase,
            event_id=transition_event_id,
        )
        with _vault_lock(vault):
            _initialize_vault_unlocked(vault)
            gist = SessionGist(
                gist_id=_kanban_gist_id(
                    board,
                    task_id,
                    transition_event_id,
                    recorded_run_id,
                    outcome,
                ),
                occurred_at=occurred_at,
                agent_id=str(
                    (run["profile"] if run is not None else None)
                    or task["assignee"]
                    or task["created_by"]
                    or "hermes"
                ),
                role=phase or "worker",
                function_id=function_id,
                title=str(
                    (work or {}).get("title") or task["title"] or function_id
                ),
                context=_kanban_context(board, task, recorded_run_id),
                summary=summary_text,
                reused="none",
                result=(
                    f"{outcome}; status={task['status']}; "
                    f"phase={task['current_step_key'] or 'none'}"
                ),
                maturity=_kanban_maturity(outcome, phase, str(task["status"])),
                evidence=_kanban_evidence(task, run, event),
                behavior="none",
                decisions="none",
                open_loops=(
                    "none"
                    if outcome == "completed"
                    else f"Current phase: {task['current_step_key'] or 'none'}"
                ),
            )
            appended = _append_gist_unlocked(vault, gist)
            _lint_vault_unlocked(vault)
            return appended
    except Exception as exc:
        _log.warning(
            "Agent Memory capture failed for board=%s task=%s run=%s: %s",
            board,
            task_id,
            run_id,
            exc,
        )
        return False


def recall_for_qualification(raw_request: object) -> list[dict[str, str]]:
    """Return bounded, JSON-ready historical evidence for qualification."""
    try:
        vault = configured_vault_path()
        if vault is None:
            return []
        query = (
            json.dumps(raw_request, ensure_ascii=False, sort_keys=True, default=str)
            if not isinstance(raw_request, str)
            else raw_request
        )
        return [
            {
                "function_id": match.function_id,
                "title": match.title,
                "gist_id": match.gist_id,
                "evidence": match.evidence,
                "snippet": match.snippet,
            }
            for match in recall(vault, query, limit=5)
        ]
    except Exception as exc:
        _log.warning("Agent Memory qualification recall failed: %s", exc)
        return []


def recall_for_task(title: str, body: str | None) -> list[MemoryMatch]:
    """Return bounded historical evidence for one worker context."""
    try:
        vault = configured_vault_path()
        if vault is None:
            return []
        return recall(vault, " ".join(filter(None, (title, body))), limit=5)
    except Exception as exc:
        _log.warning("Agent Memory worker recall failed: %s", exc)
        return []


def _kanban_run(conn: Any, task_id: str, run_id: int | None) -> Any:
    if run_id is not None:
        return conn.execute(
            "SELECT * FROM task_runs WHERE id = ? AND task_id = ?",
            (int(run_id), task_id),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _kanban_work_contract(conn: Any, contract_id: object) -> dict[str, Any] | None:
    if not contract_id:
        return None
    row = conn.execute(
        "SELECT canonical_json FROM work_contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    if row is None:
        return None
    value = json.loads(row["canonical_json"])
    return value if isinstance(value, dict) else None


def _functional_work(
    task: Any, contract: dict[str, Any] | None
) -> dict[str, Any] | None:
    work = contract.get("work") if isinstance(contract, dict) else None
    if isinstance(work, dict):
        candidate = {
            key: work.get(key)
            for key in (
                "item_kind", "work_type", "title", "outcome", "scope", "out_of_scope"
            )
        }
        if any(
            candidate.get(key) not in (None, "", [], {})
            for key in ("item_kind", "work_type", "outcome", "scope", "out_of_scope")
        ):
            return candidate
    if task["idempotency_key"]:
        return {
            "title": task["title"],
            "idempotency_key": str(task["idempotency_key"]),
        }
    return None


def _function_id(work: dict[str, Any]) -> str:
    functional_boundary = {
        key: work.get(key)
        for key in (
            "idempotency_key",
            "item_kind",
            "work_type",
            "outcome",
            "scope",
            "out_of_scope",
        )
        if key in work
    }
    canonical = json.dumps(
        functional_boundary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "function-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _kanban_event(
    conn: Any,
    task_id: str,
    event_id: int,
) -> Any:
    return conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE id = ? AND task_id = ?",
        (int(event_id), task_id),
    ).fetchone()


def _kanban_gist_id(
    board: str,
    task_id: str,
    event_id: int | None,
    run_id: int | None,
    outcome: str,
) -> str:
    identity = json.dumps(
        [board, task_id, event_id, run_id, outcome],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "kanban-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _kanban_context(board: str, task: Any, run_id: int | None) -> str:
    values = [f"board={board}", f"card={task['id']}"]
    for name, value in (
        ("run", run_id),
        ("work_contract", task["work_contract_id"]),
        ("project", task["project_id"]),
        ("repository", task["workspace_path"]),
    ):
        if value:
            values.append(f"{name}={value}")
    return "; ".join(values)


def _kanban_maturity(outcome: str, phase: str, status: str) -> str:
    if outcome == "completed" or status == "done":
        return "released"
    if outcome == "advanced":
        return {
            "development": "code_complete",
            "test": "tested",
            "review": "reviewed",
        }.get(phase, "planned")
    if phase == "development":
        return "in_development"
    return "planned"


def _transition_summary(
    *,
    outcome: str,
    status: str,
    phase: str,
    event_id: int | None,
) -> str:
    return (
        f"Kanban transition {outcome}: status={status}; "
        f"phase={phase or 'none'}; event={event_id or 'none'}."
    )


def _kanban_evidence(task: Any, run: Any, event: Any) -> str:
    values: list[str] = []
    if task["workspace_path"]:
        values.append(f"repository={_recorded_text(task['workspace_path'])}")
    if task["branch_name"]:
        values.append(f"branch={_recorded_text(task['branch_name'])}")
    if run is not None:
        if run["metadata"]:
            values.extend(_evidence_values(_json_mapping(run["metadata"])))
    if event is not None:
        values.extend(_evidence_values(_json_mapping(event["payload"])))
    return "; ".join(dict.fromkeys(values)) or "none"


def _json_mapping(raw: object) -> Mapping[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return raw
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _evidence_values(mapping: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for raw_key, value in mapping.items():
        key = str(raw_key).strip().lower()
        if key not in _EVIDENCE_KEYS:
            continue
        if isinstance(value, Mapping):
            nested = _evidence_values(value)
            values.extend(f"{key}.{item}" for item in nested)
            continue
        if isinstance(value, (list, tuple)):
            rendered_values = []
            for item in value:
                if isinstance(item, (Mapping, list, tuple)):
                    continue
                validated = _validated_evidence_value(key, item)
                if validated is not None:
                    rendered_values.append(validated)
            rendered = ", ".join(rendered_values)
        else:
            rendered = _validated_evidence_value(key, value) or ""
        if rendered:
            values.append(f"{key}={_clip(rendered, _MAX_EVIDENCE_CHARS)}")
    return values


def _validated_evidence_value(key: str, value: object) -> str | None:
    candidate = " ".join(str(value).split())
    if not candidate or len(candidate) > _MAX_EVIDENCE_CHARS:
        return None
    if _UNSAFE_IDENTIFIER_RE.search(candidate):
        return None
    if key in _COMMIT_EVIDENCE_KEYS:
        valid = bool(_COMMIT_RE.fullmatch(candidate))
    elif key in _PR_EVIDENCE_KEYS:
        valid = bool(_PR_RE.fullmatch(candidate) or _is_pr_url(candidate))
    elif key in _REPOSITORY_EVIDENCE_KEYS:
        valid = _is_repository_reference(candidate)
    else:
        valid = "://" not in candidate and bool(_IDENTIFIER_RE.fullmatch(candidate))
    return _recorded_text(candidate) if valid else None


def _is_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def _is_pr_url(value: str) -> bool:
    if not _is_http_url(value):
        return False
    path = urlsplit(value).path.lower()
    return any(marker in path for marker in ("/pull/", "/pulls/", "/merge_requests/"))


def _is_repository_reference(value: str) -> bool:
    return (
        _is_http_url(value)
        or value.startswith("/")
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    )


def initialize_vault(vault: Path) -> None:
    """Create the documented external vault layout without touching other paths."""
    vault = Path(vault)
    with _vault_lock(vault):
        _initialize_vault_unlocked(vault)


def _initialize_vault_unlocked(vault: Path) -> None:
    for directory in (
        vault,
        vault / "memory",
        vault / "wiki" / "functions",
        vault / "wiki" / "learnings",
        vault / "raw",
        vault / ".derived",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    _write_if_missing(vault / "agents.md", _agents_template())
    _write_if_missing(vault / "snapshot.md", "# Agent Memory Snapshot\n")
    _write_if_missing(vault / "index.md", "# Agent Memory Index\n")
    _write_if_missing(vault / "log.md", "# Agent Memory Log\n")
    _write_if_missing(
        vault / ".derived" / "functions.json",
        json.dumps({"functions": []}, indent=2, sort_keys=True) + "\n",
    )


def append_gist(vault: Path, gist: SessionGist) -> bool:
    """Append one redacted Session Gist, returning false for an existing gist ID."""
    vault = Path(vault)
    _gist_id(gist.gist_id)
    with _vault_lock(vault):
        _initialize_vault_unlocked(vault)
        return _append_gist_unlocked(vault, gist)


def _append_gist_unlocked(vault: Path, gist: SessionGist) -> bool:
    gist_id = _gist_id(gist.gist_id)
    entry = _render_gist(gist, gist_id)
    if _gist_exists(vault, gist_id):
        return False

    occurred_on = _occurred_on(gist.occurred_at)
    history_path = vault / "memory" / f"{occurred_on.isoformat()}.md"
    with history_path.open("a", encoding="utf-8") as handle:
        if history_path.stat().st_size:
            handle.write("\n")
        handle.write(entry)
    return True


def recall(vault: Path, query: str, limit: int = 5) -> list[MemoryMatch]:
    """Return capped lexical matches from valid historical Session Gists."""
    query_text = _recorded_text(query)
    query_words = _words(query_text)
    query_folded = query_text.lower()
    if not query_words and not query_folded:
        return []

    matches: list[MemoryMatch] = []
    for entry in _recent_valid_entries(Path(vault)):
        title_words = _words(entry.title)
        all_text = " ".join((entry.function_id, entry.title, *entry.fields.values()))
        shared_words = query_words & _words(all_text)
        exact_identifier = query_folded in {entry.function_id.lower(), entry.gist_id.lower()}
        score = (1_000 if exact_identifier else 0) + len(shared_words) + 10 * len(query_words & title_words)
        if score == 0:
            continue
        summary = entry.fields["Summary"]
        matches.append(
            MemoryMatch(
                function_id=entry.function_id,
                title=entry.title,
                gist_id=entry.gist_id,
                evidence=_clip(entry.fields["Evidence"], _MAX_EVIDENCE_CHARS),
                snippet=_clip(f"{entry.title}: {summary}", _MAX_EVIDENCE_CHARS),
                score=score,
            )
        )

    capped_limit = max(0, min(int(limit), _MAX_RECALL_RESULTS))
    return sorted(matches, key=lambda match: (-match.score, match.function_id, match.gist_id))[:capped_limit]


def lint_vault(vault: Path) -> LintReport:
    """Validate append-only history and deterministically rebuild derived views."""
    vault = Path(vault)
    with _vault_lock(vault):
        _initialize_vault_unlocked(vault)
        return _lint_vault_unlocked(vault)


def _lint_vault_unlocked(vault: Path) -> LintReport:
    entries = _valid_entries(vault)
    invalid_entries = _invalid_entry_count(vault, len(entries))
    functions: dict[str, dict[str, str]] = {}
    for entry in entries:
        functions[entry.function_id] = {
            "function_id": entry.function_id,
            "title": entry.title,
            "maturity": entry.fields["Maturity"],
            "last_gist_id": entry.gist_id,
            "evidence": entry.fields["Evidence"],
        }

    projection = {"functions": [functions[key] for key in sorted(functions)]}
    _atomic_write_text(
        vault / ".derived" / "functions.json",
        json.dumps(projection, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write_text(vault / "index.md", _index_markdown(projection["functions"]))
    _atomic_write_text(
        vault / "snapshot.md", _snapshot_markdown(projection["functions"])
    )
    _atomic_write_text(
        vault / "log.md",
        f"# Agent Memory Log\n\n- Lint: {len(entries)} valid, {invalid_entries} invalid Session Gists.\n",
    )
    return LintReport(len(entries), invalid_entries, len(functions))


@contextlib.contextmanager
def _vault_lock(vault: Path) -> Iterator[None]:
    """Acquire a bounded host-wide lock for one vault mutation."""
    lock_path = Path(vault) / ".derived" / "agent-memory.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        deadline = time.monotonic() + _VAULT_LOCK_TIMEOUT_SECONDS
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring Agent Memory vault lock: {lock_path}"
                    )
                time.sleep(_VAULT_LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _agents_template() -> str:
    return """# Agent Memory Vault\n\nAppend-only Session Gists use this schema:\n\n```markdown\n## HH:MM | <agent-id> | <role>\n<!-- gist_id: <opaque-id> -->\n- Function: <function_id> | <title>\n- Context: <board/card/project/repository evidence>\n- Summary: <1-3 sentences>\n- Reused: <existing functionality/evidence, or none>\n- Result: <what changed or was learned>\n- Maturity: <allowed maturity value>\n- Evidence: <commits, PRs, tests, review/release references>\n- Behavior: <learning, or none>\n- Decisions: <decisions, or none>\n- Open loops: <remaining work, or none>\n```\n\nDo not store transcripts, private reasoning, credentials, secrets, or unrelated conversation.\n"""


def _gist_exists(vault: Path, gist_id: str) -> bool:
    marker = f"<!-- gist_id: {gist_id} -->"
    return any(marker in path.read_text(encoding="utf-8") for path in (vault / "memory").glob("*.md"))


def _render_gist(gist: SessionGist, gist_id: str) -> str:
    occurred_at = gist.occurred_at
    if not isinstance(occurred_at, datetime):
        raise TypeError("occurred_at must be a datetime")
    fields = (
        ("Function", f"{_recorded_text(gist.function_id)} | {_recorded_text(gist.title)}"),
        ("Context", _recorded_text(gist.context)),
        ("Summary", _recorded_text(gist.summary)),
        ("Reused", _recorded_text(gist.reused)),
        ("Result", _recorded_text(gist.result)),
        ("Maturity", _recorded_text(gist.maturity)),
        ("Evidence", _recorded_text(gist.evidence)),
        ("Behavior", _recorded_text(gist.behavior)),
        ("Decisions", _recorded_text(gist.decisions)),
        ("Open loops", _recorded_text(gist.open_loops)),
    )
    header = " | ".join(
        (_recorded_text(occurred_at.strftime("%H:%M")), _recorded_text(gist.agent_id), _recorded_text(gist.role))
    )
    lines = [f"## {header}", f"<!-- gist_id: {gist_id} -->"]
    lines.extend(f"- {name}: {value}" for name, value in fields)
    return "\n".join(lines) + "\n"


def _recorded_text(value: object) -> str:
    redacted = redact_sensitive_text("" if value is None else str(value), force=True)
    redacted = _HTTP_URL_RE.sub(_redact_http_url_values, redacted)
    normalized = " ".join(redacted.split())
    return _clip(normalized, _MAX_RECORDED_CHARS)


def _clip(value: str, maximum: int) -> str:
    return value if len(value) <= maximum else value[: maximum - 1] + "…"


def _redact_http_url_values(match: re.Match[str]) -> str:
    """Keep an HTTP(S) URL recognizable without storing any credential value."""
    try:
        parts = urlsplit(match.group(0))
    except ValueError:
        return "«redacted-url»"

    userinfo, separator, host = parts.netloc.rpartition("@")
    if separator and ":" in userinfo:
        username, _, _password = userinfo.partition(":")
        netloc = f"{username}:«redacted-secret»@{host}"
    else:
        netloc = parts.netloc
    return urlunsplit(
        (
            parts.scheme,
            netloc,
            parts.path,
            "[REDACTED]" if parts.query else "",
            "[REDACTED]" if parts.fragment else "",
        )
    )


def _gist_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_GIST_ID_RE.fullmatch(value):
        raise ValueError("gist_id must be a bounded opaque identifier")
    if redact_sensitive_text(value, force=True) != value:
        raise ValueError("gist_id must be a bounded opaque identifier")
    return value


def _occurred_on(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("occurred_at must be a datetime")


def _valid_entries(vault: Path) -> list[_ParsedGist]:
    entries: list[_ParsedGist] = []
    memory_dir = vault / "memory"
    if not memory_dir.exists():
        return entries
    for path in sorted(memory_dir.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        for match in _GIST_RE.finditer(content):
            parsed = _parse_gist(match, path)
            if parsed is not None:
                entries.append(parsed)
    return entries


def _recent_valid_entries(vault: Path) -> list[_ParsedGist]:
    """Read only bounded recent history for advisory recall."""
    memory_dir = vault / "memory"
    if not memory_dir.exists():
        return []

    entries: list[_ParsedGist] = []
    for path in sorted(memory_dir.glob("*.md"), reverse=True)[:_MAX_RECALL_FILES]:
        matches = list(_GIST_RE.finditer(path.read_text(encoding="utf-8")))
        for match in reversed(matches):
            parsed = _parse_gist(match, path)
            if parsed is not None:
                entries.append(parsed)
                if len(entries) == _MAX_RECALL_GISTS:
                    return entries
    return entries


def _parse_gist(match: re.Match[str], source: Path) -> _ParsedGist | None:
    fields = {item.group("name"): item.group("value") for item in _FIELD_RE.finditer(match.group("body"))}
    if any(name not in fields or not fields[name] for name in _REQUIRED_FIELDS):
        return None
    function_id, separator, title = fields["Function"].partition(" | ")
    if not separator or not function_id or not title:
        return None
    return _ParsedGist(
        gist_id=match.group("gist_id"),
        function_id=function_id,
        title=title,
        fields=fields,
        source=source,
    )


def _invalid_entry_count(vault: Path, valid_entries: int) -> int:
    memory_dir = vault / "memory"
    if not memory_dir.exists():
        return 0
    headings = sum(
        len(re.findall(r"(?m)^## ", path.read_text(encoding="utf-8")))
        for path in memory_dir.glob("*.md")
    )
    return max(0, headings - valid_entries)


def _words(value: str) -> set[str]:
    return set(_WORD_RE.findall(value.lower()))


def _index_markdown(functions: list[dict[str, str]]) -> str:
    lines = ["# Agent Memory Index", "", "| Function | Title | Maturity | Latest gist |", "| --- | --- | --- | --- |"]
    lines.extend(
        f"| `{item['function_id']}` | {item['title']} | {item['maturity']} | `{item['last_gist_id']}` |"
        for item in functions
    )
    return "\n".join(lines) + "\n"


def _snapshot_markdown(functions: list[dict[str, str]]) -> str:
    lines = ["# Agent Memory Snapshot", "", "## Known functions"]
    lines.extend(f"- `{item['function_id']}` — {item['title']} ({item['maturity']})" for item in functions)
    return "\n".join(lines) + "\n"
