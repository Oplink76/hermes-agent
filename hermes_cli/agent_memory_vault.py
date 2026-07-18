"""Append-only, external Markdown storage for concise agent session gists."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
from typing import Mapping

from agent.redact import redact_sensitive_text


_MAX_RECORDED_CHARS = 2_000
_MAX_EVIDENCE_CHARS = 500
_MAX_RECALL_RESULTS = 20
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


def initialize_vault(vault: Path) -> None:
    """Create the documented external vault layout without touching other paths."""
    vault = Path(vault)
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
    initialize_vault(vault)
    vault = Path(vault)
    gist_id = _gist_id(gist.gist_id)
    if _gist_exists(vault, gist_id):
        return False

    occurred_on = _occurred_on(gist.occurred_at)
    history_path = vault / "memory" / f"{occurred_on.isoformat()}.md"
    entry = _render_gist(gist, gist_id)
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
    for entry in _valid_entries(Path(vault)):
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
    initialize_vault(vault)
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
    (vault / ".derived" / "functions.json").write_text(
        json.dumps(projection, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (vault / "index.md").write_text(_index_markdown(projection["functions"]), encoding="utf-8")
    (vault / "snapshot.md").write_text(_snapshot_markdown(projection["functions"]), encoding="utf-8")
    (vault / "log.md").write_text(
        f"# Agent Memory Log\n\n- Lint: {len(entries)} valid, {invalid_entries} invalid Session Gists.\n",
        encoding="utf-8",
    )
    return LintReport(len(entries), invalid_entries, len(functions))


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
    normalized = " ".join(redacted.split())
    return _clip(normalized, _MAX_RECORDED_CHARS)


def _clip(value: str, maximum: int) -> str:
    return value if len(value) <= maximum else value[: maximum - 1] + "…"


def _gist_id(value: object) -> str:
    identifier = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(value).strip())
    identifier = identifier.strip("-._:")
    if not identifier:
        raise ValueError("gist_id must contain an opaque identifier")
    return _clip(identifier, 200)


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
