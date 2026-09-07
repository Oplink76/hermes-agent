"""SQLite-backed Kanban board shared across profiles (the cross-profile coordination primitive).

Lives under the shared Hermes root: ``default`` board DB at ``<root>/kanban.db`` (pre-boards
back-compat), other boards at ``<root>/kanban/boards/<slug>/``; a worker on one board never sees
another. Board resolution: ``board=`` arg > ``HERMES_KANBAN_BOARD`` > ``HERMES_KANBAN_DB`` (pins the
file path) > ``<root>/kanban/current`` > ``default``; the dispatcher injects these into workers.
Concurrency: WAL + ``BEGIN IMMEDIATE`` + compare-and-swap on ``tasks.status``/``claim_lock`` —
SQLite serializes writers so one claimer wins, losers see zero rows (no retries, no distributed
locks). Schema: tasks, task_links, task_comments, task_events, task_runs, attachments, notify subs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Tuple

from hermes_cli.kanban_intake import (
    ACTIVE_REQUALIFICATION_STATUSES,
    DEFAULT_POLICY_VERSION,
    qualification_max_total_attempts,
)
from hermes_cli.kanban_repository import (
    EvidenceWorkspaceError,
    EvidenceWorkspaceResult,
    RepositoryConfigurationError,
    RepositoryContract,
    RefreshRequest,
    RefreshResult,
    RELEASE_CANDIDATE_REF_PREFIX,
    TargetHeadsObservation,
    VerificationProfile,
    VerificationResult,
    VerificationStepResult,
    advance_prepared_candidate_ref,
    build_verification_receipt_key,
    commit_contains,
    delete_release_candidate_ref,
    inspect_evidence_workspace,
    load_repository_contract,
    observe_ci_workflow_runs,
    observe_target_heads,
    refresh_story_branch,
    resolve_commit,
    restore_generated_paths,
    run_verification,
    validate_release_candidate_ref,
    verification_receipt_from_payload,
    verification_receipt_matches,
    verification_result_payload,
)
from hermes_cli.kanban_epic_release import (
    EpicReadiness,
    EpicReleaseCIObservation,
    EpicReleaseCIObservationError,
    EpicReleaseHandoff,
    EpicReleaseHandoffError,
    EpicReleaseInvalidation,
    EpicReleaseInvalidationError,
    EpicReleaseMember,
    EpicReleasePreparationError,
    EpicReleaseSnapshot,
    EpicTerminalSource,
    derive_epic_readiness,
    epic_release_member_from_row,
    epic_release_snapshot_from_row,
)
from hermes_cli.kanban_product_outcomes import (
    ApprovedCandidate,
    CandidateEligibility,
    CandidateEligibilityError,
    OutcomeValidationError,
    PassedTest,
    ProductOutcomeError,
    TerminalOutcome,
    TerminalRunRecord,
    candidate_eligibility,
    latest_review_authority,
    latest_test_authority,
    validate_terminal_outcome,
)

from toolsets import get_toolset_names

_log = logging.getLogger(__name__)

_GOVERNANCE_WRITE_AUTHORIZED: ContextVar[bool] = ContextVar(
    "kanban_governance_write_authorized", default=False
)


@contextlib.contextmanager
def authorized_governance_write():
    """Authorize one service-owned governance write in this context."""
    token = _GOVERNANCE_WRITE_AUTHORIZED.set(True)
    try:
        yield
    finally:
        _GOVERNANCE_WRITE_AUTHORIZED.reset(token)


# --- Shared micro-helpers (row access, JSON, env, git) ---

def _row_get(row: Any, col: str, default: Any = None) -> Any:
    """``row[col]`` tolerant of the column being absent from the SELECT / schema."""
    if row is None or col not in row.keys():
        return default
    return row[col]


def _json_or(value: Any, default: Any = None) -> Any:
    """Decode a JSON text column; any decode failure or empty value yields ``default``."""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_dict(value: Any) -> dict:
    """Decode a JSON text column that must be an object; anything else yields ``{}``."""
    parsed = _json_or(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Integer env override: absent/empty/non-integer/below ``minimum`` falls back to ``default``."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return default
        if parsed >= minimum:
            return parsed
    return default


def _git_out(cwd: Path, *args: str, timeout: int = 30) -> Optional[str]:
    """Run ``git -C cwd args`` and return stripped stdout, or ``None`` on any failure / empty output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


# --- Constants ---

VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
VALID_INITIAL_STATUSES = {"running", "blocked"}

# Typed block reasons (routing in ``_route_block``); ``None`` = legacy un-typed.
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}

# Same-reason block -> unblock -> re-block cycles before routing to ``triage``.
# Counts unblock recurrences, NOT dispatcher failures (``DEFAULT_FAILURE_LIMIT``).
BLOCK_RECURRENCE_LIMIT = 2
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}

# Product-board workflow contracts retained by the fork. These are durable
# governance state, not presentation-only columns.
PRODUCT_BOARD_COLUMNS: list[dict[str, str]] = [
    {"name": "backlog", "label": "Backlog", "status": "ready", "help": "Prioritized user-story cards before architecture/development starts."},
    {"name": "architecture", "label": "Architecture", "status": "ready", "help": "Architecture child work after stories are ready."},
    {"name": "development", "label": "Development", "status": "ready", "help": "Implementation work using branch/worktree and local AI writer."},
    {"name": "test", "label": "Test", "status": "ready", "help": "Test/verification work from acceptance criteria."},
    {"name": "review", "label": "Review", "status": "review", "help": "Independent reviewer pass; self-reports remain advisory."},
    {"name": "release_measure", "label": "Release / Measure", "status": "ready", "help": "Human release/measurement gate; no auto-dispatch unless explicitly approved."},
    {"name": "done", "label": "Done", "status": "done", "help": "Completed and verified."},
    {"name": "blocked", "label": "Blocked", "status": "blocked", "help": "Needs human input or a real blocker cleared."},
]
PRODUCT_WORKFLOW_TRANSITIONS: dict[str, dict[str, Optional[str]]] = {
    "backlog": {"next_step": "architecture", "status": "ready", "assignee_role": "architect"},
    "architecture": {"next_step": "development", "status": "ready", "assignee_role": "developer"},
    "development": {"next_step": "test", "status": "ready", "assignee_role": "tester"},
    "test": {"next_step": "review", "status": "review", "assignee_role": "reviewer"},
    "review": {"next_step": "release_measure", "status": "ready", "assignee_role": None},
}
PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES = {
    "architect": "architect", "developer": "developer", "tester": "tester", "reviewer": "reviewer",
}
PRODUCT_QUALIFICATION_DEFAULTS: dict[str, Any] = {
    "required": False, "contract_version": 1, "policy_version": DEFAULT_POLICY_VERSION,
    "max_total_attempts": 3, "paths": ["po", "hermes"],
    "work_types": ["story", "bug", "maintenance", "ops", "spike"],
    "phase_assignees": {"backlog": "productowner", "architecture": "architect", "development": "developer", "test": "tester", "review": "reviewer", "release_measure": None},
}
DEFAULT_PRODUCT_WORKFLOW: dict[str, Any] = {"handoff_v2": True, "assignees": PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES}
PRODUCT_HUMAN_BLOCK_KINDS = {None, "needs_input", "capability"}
PRODUCT_WORKFLOW_PRECHECK_EVENT = "human_input_preflight"
PRODUCT_REWORK_ROUTES = {
    ("test", "changes_requested"): "development",
    ("review", "changes_requested"): "development",
    ("review", "architecture_invalid"): "architecture",
}
PRODUCT_POSITIVE_OUTCOMES = {"test": "passed", "review": "approved"}
PRODUCT_POSITIVE_OUTCOME_STEPS = {verdict: step for step, verdict in PRODUCT_POSITIVE_OUTCOMES.items()}
PRODUCT_PROVENANCE_REQUIRED_STEPS = {"development", "test", "review"}
_PRODUCT_COMMIT_REQUIRED_STEPS = {"development"}
PRODUCT_WORKFLOW_COMMIT_REQUIRED_STEPS = _PRODUCT_COMMIT_REQUIRED_STEPS
PRODUCT_PROVENANCE_BLOCKED_EVENT = "completion_blocked_provenance"
PRODUCT_WORKFLOW_TEMPLATE_ID = "product"
PRODUCT_WORKFLOW_STEPS = ("backlog", "architecture", "development", "test", "review", "release_measure", "done")
PRODUCT_WORKFLOW_STEP_SET = frozenset(PRODUCT_WORKFLOW_STEPS)
PRODUCT_WORKFLOW_ROLE_TO_STEP = {
    "productowner": "backlog", "architect": "architecture", "developer": "development",
    "tester": "test", "reviewer": "review",
}


@dataclass(frozen=True)
class RetryState:
    attempts_used: int
    attempts_limit: int
    allowed: bool
    reason: Optional[str]


def _is_engine_owned_integration_state(row: Any) -> bool:
    """Identify lifecycle states owned only by Epic coordinators."""
    if row is None:
        return False
    return row["workflow_template_id"] == "product_epic" or row["current_step_key"] == "integration_pending"


def normalize_reasoning_effort(effort: Optional[str]) -> Optional[str]:
    """``VALID_REASONING_EFFORTS`` or ``"none"`` (thinking off), case-insensitive;
    empty/None = inherit the profile's own effort (NULL). Anything else raises —
    a typo'd level must not quietly hand the task back to the profile default."""
    from hermes_constants import VALID_REASONING_EFFORTS

    value = str(effort or "").strip().lower()
    if not value:
        return None
    if value == "none" or value in VALID_REASONING_EFFORTS:
        return value
    allowed = ", ".join(("none", *VALID_REASONING_EFFORTS))
    raise ValueError(f"reasoning_effort must be one of {allowed}, got {effort!r}")


KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_IS_WINDOWS = sys.platform == "win32"
KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024  # one cap for dashboard, tools and CLI


def _assert_not_delegated_child_mutation() -> None:
    """Reject Kanban mutations from ``delegate_task`` child contexts.

    The tool/CLI fast-fail guards are UX, not a trust boundary (a child can shell
    out or import this module); the invariant lives here so every ``write_txn``
    user and board-metadata mutator fails closed before touching durable state.
    """
    try:
        from agent.delegation_context import is_delegated_child_process_context

        delegated = is_delegated_child_process_context()
    except Exception:
        delegated = bool(os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"))
    if delegated:
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")


def _fire_kanban_lifecycle_hook(event: str, task_id: str, **fields: Any) -> None:
    """Best-effort lifecycle hook. Call AFTER the write txn commits (plugins never
    run under the SQLite write lock, always see durable state); failures are
    swallowed so an observer can never break a transition."""
    try:
        from hermes_cli.lifecycle import invoke_hook

        invoke_hook(event, task_id=task_id, profile_name=_hook_profile_name(), **fields)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban lifecycle hook %s failed: %s", event, exc)


def _fire_task_hook(event: str, task: Optional["Task"], task_id: str, run_id: Optional[int], **fields: Any) -> None:
    """Lifecycle hook for a task transition; ``assignee`` from the (possibly missing) row."""
    _fire_kanban_lifecycle_hook(
        event, task_id, board=get_current_board(),
        assignee=task.assignee if task else None, run_id=run_id, **fields,
    )


def _hook_profile_name() -> str:
    """Active profile for hook payloads; ``"default"`` when it cannot be resolved."""
    from hermes_cli.profiles import get_active_profile_name

    try:
        return get_active_profile_name()
    except Exception:
        return "default"


def _kanban_observer_consumed(event: str) -> bool:
    """Hot-path short-circuit: skip payload assembly when nothing subscribes.
    Inspection failure counts as unconsumed (dropping an observer is always safe)."""
    try:
        from hermes_cli.lifecycle import has_hook

        return has_hook(event)
    except Exception:  # pragma: no cover - defensive
        return False


def _fire_worker_spawned_hook(
    conn: sqlite3.Connection, task: "Task", workspace_path: str, pid: Optional[int], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_worker_spawned`` AFTER the PID is durably persisted; best-effort."""
    if not _kanban_observer_consumed("on_kanban_worker_spawned"):
        return
    try:
        _fire_kanban_lifecycle_hook(
            "on_kanban_worker_spawned", task.id, board=board or get_current_board(),
            assignee=task.assignee, run_id=_current_run_id(conn, task.id),
            worker_pid=int(pid) if pid else None, workspace_path=str(workspace_path),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban worker spawned hook failed: %s", exc)


def notify_task_updated(
    conn: sqlite3.Connection, task_id: str, changed_fields: Iterable[str], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_task_updated`` AFTER a non-lifecycle task mutation commits
    (also for direct-SQL surfaces like dashboard field editors).
    ``changed_fields`` carries field NAMES only, never values."""
    if not _kanban_observer_consumed("on_kanban_task_updated"):
        return
    try:
        row = conn.execute(
            "SELECT assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        _fire_kanban_lifecycle_hook(
            "on_kanban_task_updated", task_id, board=board or get_current_board(),
            assignee=row["assignee"] if row else None,
            run_id=row["current_run_id"] if row else None, changed_fields=list(changed_fields),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban task updated hook failed: %s", exc)


# DispatchResult counters whose non-zero value means the tick did something.
_TICK_ACTIVITY_FIELDS = (
    "spawned", "reclaimed", "promoted", "reconciled_orphans", "crashed", "stale",
    "timed_out", "auto_blocked", "rate_limited", "auto_assigned_default",
    "respawn_guarded", "skipped_per_profile_capped", "skipped_unassigned",
    "skipped_nonspawnable",
)


def _fire_dispatch_tick_hook(
    result: "DispatchResult", *, board: Optional[str] = None, dry_run: bool = False,
) -> None:
    """``on_kanban_dispatch_tick`` — strictly AFTER ``_dispatch_tick_lock`` is
    released so a slow subscriber cannot stall a sibling dispatcher.

    Re-port of PR #56066 per the #64231 batch disposition: renamed to the taxonomy form and called by
    ``dispatch_once`` strictly AFTER ``_dispatch_tick_lock`` has been released — the original fired inside
    the lock, so a slow subscriber could extend the single-writer critical section and stall a sibling
    dispatcher's tick. Observer-only and fully best-effort: any subscriber failure is swallowed.
    """
    if not _kanban_observer_consumed("on_kanban_dispatch_tick"):
        return
    try:
        from hermes_cli.lifecycle import invoke_hook

        profile_name = _hook_profile_name()
        if board is None:
            try:
                board = get_current_board()
            except Exception:
                board = None
        outcome = "ok"
        if result.skipped_locked:
            outcome = "skipped_locked"
        elif not any(getattr(result, f) for f in _TICK_ACTIVITY_FIELDS):
            outcome = "idle"
        invoke_hook(
            "on_kanban_dispatch_tick", board=board, profile_name=profile_name,
            dry_run=bool(dry_run), outcome=outcome, result=result,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban dispatch tick hook failed: %s", exc)


# Claim window before the next tick reclaims a running task; long workers
# ``heartbeat_claim`` or raise it via HERMES_KANBAN_CLAIM_TTL_SECONDS.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60

# A live PID with a heartbeat older than this is wedged and reclaimed anyway
# (``_touch_activity`` keeps genuinely active workers fresh).
# If a worker's PID is still alive but its ``last_heartbeat_at`` is older than this when
# ``release_stale_claims`` runs, treat the worker as wedged and reclaim regardless of PID liveness (#29747
# gap 3). This catches the logic-loop case where the process is technically running but not making
# observable progress. ``_touch_activity`` bridges chunk-level liveness into ``last_heartbeat_at`` via
# #31752, so any genuinely active worker keeps its heartbeat fresh as a side effect of normal API traffic.
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60

# Grace when a host-local worker survived termination (e.g. parked in D state
# under memory.high, SIGKILL pending): releasing now would spawn a duplicate.
RECLAIM_DEFER_GRACE_SECONDS = 120


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Explicit ``ttl_seconds`` > ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` > default."""
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    return _env_int("HERMES_KANBAN_CLAIM_TTL_SECONDS", DEFAULT_CLAIM_TTL_SECONDS, minimum=1)


# ``detect_crashed_workers`` skips ``_pid_alive`` this long after start: the
# fork -> /proc window can report a fresh worker dead.
DEFAULT_CRASH_GRACE_SECONDS = 30

# Worker exit "provider rate-limited": released WITHOUT counting a failure (the
# breaker must never trip on a throttle). 75 == BSD EX_TEMPFAIL.
KANBAN_RATE_LIMIT_EXIT_CODE = 75


def _resolve_crash_grace_seconds() -> int:
    """``HERMES_KANBAN_CRASH_GRACE_SECONDS`` (0 = immediate, for tests) else default."""
    return _env_int("HERMES_KANBAN_CRASH_GRACE_SECONDS", DEFAULT_CRASH_GRACE_SECONDS)


def _resolve_rate_limit_cooldown_seconds() -> int:
    """``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` (0 = next tick, for tests) else default."""
    return _env_int("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)


# build_worker_context() caps, sized for a ~100k-char prompt with headroom.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # per comment


def _relative_age(ts: Optional[int], now: Optional[int] = None) -> str:
    """``just now`` / ``18h ago`` / ``3d ago``; "" for a missing/invalid ts. An LLM
    reads a bare absolute timestamp as current fact — the relative age is what
    prompts a worker to re-verify stale sibling work."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if now is None:
        now = int(time.time())
    delta = now - ts
    if delta < 60:  # includes negative = clock skew across machines; never claim "in the future"
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# --- Paths ---

DEFAULT_BOARD = "default"
_CURRENT_BOARD_OVERRIDE: ContextVar[str | None] = ContextVar(
    "hermes_kanban_current_board_override", default=None,
)


@contextlib.contextmanager
def scoped_current_board(slug: str):
    """Pin the active board for the current context only."""
    token: Token[str | None] = _CURRENT_BOARD_OVERRIDE.set(slug)
    try:
        yield
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)


# Slug = directory name: strict enough to stop traversal / separators, loose
# enough for kebab-case. Display names (spaces, emoji) live in board.json.
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    s = str(slug).strip().lower() if slug is not None else ""
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def _slug_or_default(board: Optional[str]) -> str:
    return _normalize_board_slug(board) or DEFAULT_BOARD


def _require_slug(slug: str) -> str:
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    return normed


def kanban_home() -> Path:
    """``HERMES_KANBAN_HOME`` else ``get_default_hermes_root()``. Shared across
    profiles BY DESIGN: resolving through the active profile's HERMES_HOME would
    fork the board per profile and break the dispatcher/worker handoff."""
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """``<root>/kanban/boards`` — parent of the *additional* named boards.
    ``default`` is deliberately not here (its DB stays at ``<root>/kanban.db``)."""
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """``<root>/kanban/current`` — one-line slug written by ``boards switch``; absent = ``default``."""
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Active slug: context override -> ``HERMES_KANBAN_BOARD`` -> ``<root>/kanban/current``
    (only while that board exists) -> ``DEFAULT_BOARD``. A malformed/stale slug
    falls through — the dispatcher must never crash on a hand-edited file."""
    def _existing(candidate: str) -> Optional[str]:
        if not candidate:
            return None
        try:
            normed = _normalize_board_slug(candidate)
        except ValueError:
            return None
        return normed if normed and board_exists(normed) else None

    for candidate in (
        (_CURRENT_BOARD_OVERRIDE.get() or "").strip(),
        os.environ.get("HERMES_KANBAN_BOARD", "").strip(),
    ):
        found = _existing(candidate)
        if found:
            return found
    try:
        f = current_board_path()
        if f.exists():
            found = _existing(f.read_text(encoding="utf-8").strip())
            if found:
                return found
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board; returns the file written. Does NOT
    check the board exists — callers do (so ``boards switch <typo>`` errors)."""
    _assert_not_delegated_child_mutation()
    normed = _require_slug(slug)
    path = current_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    _assert_not_delegated_child_mutation()
    with contextlib.suppress(FileNotFoundError):
        current_board_path().unlink()


def board_dir(board: Optional[str] = None) -> Path:
    """``<root>/kanban/boards/<slug>/``. For ``default`` this holds metadata
    only (board.json, workspaces/, logs/) — its DB stays at ``<root>/kanban.db``
    for back-compat (:func:`kanban_db_path`).
    """
    return boards_root() / _slug_or_default(board)


def board_exists(board: Optional[str] = None) -> bool:
    """Board has ``board.json`` or ``kanban.db`` on disk; ``default`` always exists."""
    slug = _slug_or_default(board)
    if slug == DEFAULT_BOARD:
        return True
    return _dir_holds_board(board_dir(slug))


def _dir_holds_board(d: Path) -> bool:
    return (d / "board.json").exists() or (d / "kanban.db").exists()


def _board_path(
    env_var: Optional[str], board: Optional[str], default_parts: tuple[str, ...], leaf: str,
) -> Path:
    """Shared resolver: ``env_var`` override, else legacy ``<root>/<default_parts>``
    for the ``default`` board, else ``board_dir(slug)/leaf``."""
    if env_var:
        override = os.environ.get(env_var, "").strip()
        if override:
            return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home().joinpath(*default_parts)
    return board_dir(slug) / leaf


def kanban_db_path(board: Optional[str] = None) -> Path:
    """``kanban.db`` path: ``HERMES_KANBAN_DB`` pins it (injected into workers);
    ``default`` -> ``<root>/kanban.db`` (back-compat), else the board dir."""
    return _board_path("HERMES_KANBAN_DB", board, ("kanban.db",), "kanban.db")


def workspaces_root(board: Optional[str] = None) -> Path:
    """Per-board scratch workspace root (``HERMES_KANBAN_WORKSPACES_ROOT`` wins);
    ``default`` keeps the legacy ``<root>/kanban/workspaces/``."""
    return _board_path("HERMES_KANBAN_WORKSPACES_ROOT", board, ("kanban", "workspaces"), "workspaces")


def attachments_root(board: Optional[str] = None) -> Path:
    """Per-board attachments root (``HERMES_KANBAN_ATTACHMENTS_ROOT`` wins). Workers
    read attachments by absolute path, so remote terminal backends must mount it."""
    return _board_path("HERMES_KANBAN_ATTACHMENTS_ROOT", board, ("kanban", "attachments"), "attachments")


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    """Return the per-task attachment directory ``<root>/<task_id>/``."""
    return attachments_root(board=board) / task_id


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Per-board worker log dir (logs follow the board so ``hermes kanban log``
    is unambiguous when two boards share a task id)."""
    return _board_path(None, board, ("kanban", "logs"), "logs")


def board_metadata_path(board: Optional[str] = None) -> Path:
    """``board.json`` path — display metadata only; the directory slug is the identity."""
    return board_dir(_slug_or_default(board)) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """``atm10-server`` -> ``Atm10 Server``."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def read_board_metadata(board: Optional[str] = None) -> dict:
    """``board.json`` merged over defaults, plus ``slug`` and ``db_path``. Never
    raises — a missing/malformed file yields the synthesized entry."""
    slug = _slug_or_default(board)
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        # Project scope: new tasks inherit it (deterministic worktree + branch).
        "project_id": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str], *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, archived: Optional[bool] = None,
    default_workdir: Optional[str] = None, project_id: Optional[str] = None,
    preset: Optional[str] = None, columns: Optional[list[dict[str, str]]] = None,
    repository: Optional[Mapping[str, object]] = None,
) -> dict:
    """Create/update ``board.json``; unmentioned fields are preserved, ``created_at``
    set on first write. ``project_id``/``default_workdir``: ``None`` = unchanged,
    "" = clear (``project_id`` is not validated here)."""
    _assert_not_delegated_child_mutation()
    slug = _slug_or_default(board)
    meta = read_board_metadata(slug)
    # db_path is derived on every read; never persist it into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    for key, value in (("description", description), ("icon", icon), ("color", color)):
        if value is not None:
            meta[key] = str(value)
    if archived is not None:
        meta["archived"] = bool(archived)
    for key, value in (("default_workdir", default_workdir), ("project_id", project_id)):
        if value is not None:
            meta[key] = str(value) if value else None
    if preset is not None:
        preset_name = str(preset).strip().lower() or "generic"
        if preset_name not in {"generic", "product"}:
            raise ValueError(f"unknown board preset: {preset!r}")
        meta["preset"] = preset_name
    if columns is not None:
        meta["columns"] = [dict(column) for column in columns]
    if repository is not None:
        meta["repository"] = dict(repository)
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    if "repository" in meta:
        repository_contract_for_metadata(meta)
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def product_workflow_defaults_for_board(board: Optional[str] = None) -> dict:
    """Canonical metadata defaults for Hermes product boards."""

    slug = _normalize_board_slug(board) if board is not None else None
    return {
        "preset": "product",
        "columns": [dict(column) for column in PRODUCT_BOARD_COLUMNS],
        "product_workflow": {
            "handoff_v2": True,
            "deployment_policy": "manual",
            "assignees": dict(PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES),
        },
        "qualification": {
            **PRODUCT_QUALIFICATION_DEFAULTS,
            "paths": list(PRODUCT_QUALIFICATION_DEFAULTS["paths"]),
            "phase_assignees": dict(PRODUCT_QUALIFICATION_DEFAULTS["phase_assignees"]),
        },
        **({"slug": slug} if slug else {}),
    }


def _ensure_worktrees_gitignore(default_workdir: Optional[str]) -> None:
    """Best-effort .gitignore guard for Hermes per-card worktrees."""

    if not default_workdir:
        return
    try:
        repo = Path(str(default_workdir)).expanduser()
        if not repo.exists() or not repo.is_dir():
            return
        if not (repo / ".git").exists():
            return
        gitignore = repo / ".gitignore"
        text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        lines = [line.strip() for line in text.splitlines()]
        if ".worktrees/" in lines or ".worktrees" in lines:
            return
        prefix = "" if not text or text.endswith("\n") else "\n"
        gitignore.write_text(f"{text}{prefix}.worktrees/\n", encoding="utf-8")
    except OSError:
        return


def ensure_product_board_defaults(
    slug: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    default_workdir: Optional[str] = None,
    switch: bool = False,
    repository: Optional[Mapping[str, object]] = None,
) -> dict:
    """Create/update a product board with canonical Kanban V2 defaults."""

    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")

    defaults = product_workflow_defaults_for_board(normed)
    meta = write_board_metadata(
        normed,
        name=name,
        description=description,
        icon=icon,
        color=color,
        default_workdir=default_workdir,
        preset="product",
        columns=defaults["columns"],
        repository=repository,
    )
    meta.pop("db_path", None)
    existing_wf = meta.get("product_workflow")
    wf = dict(existing_wf) if isinstance(existing_wf, dict) else {}
    wf["handoff_v2"] = True
    wf.setdefault("deployment_policy", "manual")
    existing_assignees = wf.get("assignees") if isinstance(wf.get("assignees"), dict) else {}
    assignees = dict(PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES)
    assignees.update({str(k): str(v) for k, v in existing_assignees.items() if str(v).strip()})
    wf["assignees"] = assignees
    meta["product_workflow"] = wf
    existing_qualification = (
        meta.get("qualification") if isinstance(meta.get("qualification"), dict) else {}
    )
    qualification = {
        **PRODUCT_QUALIFICATION_DEFAULTS,
        **existing_qualification,
    }
    configured_phase_assignees = existing_qualification.get("phase_assignees")
    qualification["phase_assignees"] = {
        **PRODUCT_QUALIFICATION_DEFAULTS["phase_assignees"],
        **(
            configured_phase_assignees
            if isinstance(configured_phase_assignees, dict)
            else {}
        ),
    }
    qualification["paths"] = list(
        existing_qualification.get("paths", PRODUCT_QUALIFICATION_DEFAULTS["paths"])
    )
    meta["qualification"] = qualification
    meta["preset"] = "product"
    meta["columns"] = defaults["columns"]
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())

    path = board_metadata_path(normed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    init_db(board=normed)
    if switch:
        set_current_board(normed)
    _ensure_worktrees_gitignore(default_workdir or meta.get("default_workdir"))
    meta["db_path"] = str(kanban_db_path(normed))
    return meta


def create_board(
    slug: str, *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, default_workdir: Optional[str] = None,
    project_id: Optional[str] = None,
    preset: Optional[str] = None,
    repository: Optional[Mapping[str, object]] = None,
) -> dict:
    """Create board dir + DB + metadata (``mkdir -p`` semantics: existing board returns its metadata)."""
    normed = _require_slug(slug)
    meta = write_board_metadata(
        normed, name=name, description=description, icon=icon, color=color,
        default_workdir=default_workdir, project_id=project_id,
        preset=preset,
        columns=(
            [dict(column) for column in PRODUCT_BOARD_COLUMNS]
            if str(preset or "").strip().lower() == "product"
            else None
        ),
        repository=repository,
    )
    # Touch the DB so list_boards() sees it immediately.
    init_db(board=normed)
    return meta


def product_board_metadata(board: Optional[str] = None) -> Optional[dict]:
    """Return metadata for a product-preset board, else ``None``.

    ``read_board_metadata(None)`` intentionally synthesizes the legacy default
    board, so lifecycle callers that are operating on the *active* worker board
    must resolve ``None`` through :func:`get_current_board` first.
    """
    slug = _normalize_board_slug(board) if board is not None else get_current_board()
    meta = read_board_metadata(slug or DEFAULT_BOARD)
    return meta if str(meta.get("preset") or "").lower() == "product" else None


def repository_contract_for_metadata(
    metadata: Mapping[str, object], *, repo_root: Optional[Path] = None
) -> Optional[RepositoryContract]:
    """Validate a board's repository policy when one is configured.

    Repository policy is opt-in for older boards while the migration is
    rolling out.  Once the ``repository`` key is present, every write and
    governed Epic materialization goes through the same strict loader.
    """
    if "repository" not in metadata:
        return None
    raw_root = repo_root
    if raw_root is None:
        raw_default = metadata.get("default_workdir")
        if not isinstance(raw_default, str) or not raw_default.strip():
            raise RepositoryConfigurationError("missing_repo_root", "default_workdir")
        raw_root = Path(raw_default).expanduser()
    else:
        raw_root = Path(raw_root).expanduser()
    if not raw_root.is_absolute():
        raise RepositoryConfigurationError("invalid_repo_root", str(raw_root))
    return load_repository_contract(metadata, repo_root=raw_root)


def repository_contract_for_board(
    board: Optional[str] = None, *, repo_root: Optional[Path] = None
) -> Optional[RepositoryContract]:
    """Return the validated repository policy for a product board, if set."""
    metadata = product_board_metadata(board)
    if metadata is None:
        return None
    return repository_contract_for_metadata(metadata, repo_root=repo_root)


def is_product_board(board: Optional[str] = None) -> bool:
    return product_board_metadata(board) is not None


def _product_workflow_dict(meta: Optional[dict]) -> dict:
    if not isinstance(meta, dict):
        return {}
    raw = meta.get("product_workflow") or meta.get("workflow") or {}
    return raw if isinstance(raw, dict) else {}


def _handoff_v2_enabled(board_meta: Optional[dict]) -> bool:
    wf = _product_workflow_dict(board_meta)
    return wf.get("handoff_v2") is True


def _known_board_slug_for_connection(conn: sqlite3.Connection) -> Optional[str]:
    """Return the slug when an open connection points at a managed board DB.

    Most lifecycle helpers receive only a sqlite connection. Resolve the board
    from SQLite's ``main`` database path so board-level workflow policy still
    applies when callers use ``connect(board=...)`` without setting
    ``HERMES_KANBAN_BOARD``.
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        main = next((row for row in rows if row["name"] == "main"), None)
        filename = str(main["file"] if main is not None else "").strip()
        if filename:
            db_path = Path(filename).expanduser().resolve(strict=False)
            if db_path == (kanban_home() / "kanban.db").resolve(strict=False):
                return DEFAULT_BOARD
            root = boards_root().resolve(strict=False)
            if db_path.name == "kanban.db" and db_path.parent.parent == root:
                slug = _normalize_board_slug(db_path.parent.name)
                if slug:
                    return slug
    except Exception:
        pass
    return None


def _board_slug_for_connection(conn: sqlite3.Connection) -> str:
    """Best-effort board slug for lifecycle helpers, with active-board fallback."""

    return _known_board_slug_for_connection(conn) or get_current_board()


def _product_release_measure_unblocks_dependents(meta: Optional[dict]) -> bool:
    """Return True when Release / Measure counts as dependency-satisfied.

    Some product boards are intended to run fully autonomously overnight: the
    Release / Measure bucket remains visible for audit/measurement, but should
    not freeze downstream architecture/development cards. Keep the old human
    gate as the default and require an explicit board opt-in.
    """
    wf = _product_workflow_dict(meta)
    keys = (
        "release_measure_unblocks_dependents",
        "release_measure_satisfies_dependencies",
        "autonomous_dependency_flow",
    )
    for key in keys:
        if key in wf:
            return wf.get(key) is not False
    if isinstance(meta, dict):
        for key in keys:
            if key in meta:
                return meta.get(key) is not False
    return False


def _product_merge_after_green(meta: Optional[dict]) -> bool:
    """Return True when a Done product story's branch should auto-merge into
    LOCAL main. OFF by default -- an explicit per-board opt-in
    (``product_workflow.merge_after_green``) because, unlike the other product
    flags, this MUTATES git history. Requires an explicit ``True`` (not merely
    "not False"): the merge-back must never fire on an unset/ambiguous flag.
    """
    wf = _product_workflow_dict(meta)
    if "merge_after_green" in wf:
        return wf.get("merge_after_green") is True
    if isinstance(meta, dict) and "merge_after_green" in meta:
        return meta.get("merge_after_green") is True
    return False


def _dependency_parent_satisfied(row: sqlite3.Row, *, release_measure_unblocks: bool) -> bool:
    status = row["status"]
    if status in ("done", "archived"):
        return True
    if not release_measure_unblocks:
        return False
    return (
        status == "ready"
        and row["workflow_template_id"] == "product"
        and row["current_step_key"] == "release_measure"
    )


def _product_role_assignee(
    meta: Optional[dict],
    role: Optional[str],
    overrides: Optional[dict[str, str]] = None,
) -> Optional[str]:
    if not role:
        return None
    merged: dict[str, str] = dict(PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES)
    wf = _product_workflow_dict(meta)
    meta_assignees = None
    if isinstance(wf.get("assignees"), dict):
        meta_assignees = wf.get("assignees")
    elif isinstance(meta, dict) and isinstance(meta.get("assignees"), dict):
        meta_assignees = meta.get("assignees")
    if isinstance(meta_assignees, dict):
        merged.update({str(k): str(v) for k, v in meta_assignees.items() if str(v).strip()})
    if isinstance(overrides, dict):
        merged.update({str(k): str(v) for k, v in overrides.items() if str(v).strip()})
    assignee = str(merged.get(role) or "").strip()
    return assignee or None


def _column_status_for_step(meta: Optional[dict], step_key: Optional[str]) -> str:
    if not step_key:
        return "ready"
    columns = meta.get("columns") if isinstance(meta, dict) else None
    if isinstance(columns, list):
        for col in columns:
            if isinstance(col, dict) and col.get("name") == step_key:
                status = str(col.get("status") or "").strip()
                return status if status in VALID_STATUSES else "ready"
    for col in PRODUCT_BOARD_COLUMNS:
        if col.get("name") == step_key:
            status = str(col.get("status") or "").strip()
            return status if status in VALID_STATUSES else "ready"
    return "ready"


def _is_product_board_metadata(meta: Optional[dict]) -> bool:
    """Return True when board metadata opts into the product/Relay workflow."""
    if not isinstance(meta, dict):
        return False
    preset = str(meta.get("preset") or meta.get("workflow") or "").strip().lower()
    return preset in {"product", "relay"} or isinstance(meta.get("product_workflow"), dict)


def _looks_like_product_story(title: str) -> bool:
    lowered = (title or "").strip().lower()
    return lowered.startswith(("user story:", "story:", "user story -", "story -"))


class ProductWorkflowStateError(ValueError):
    def __init__(self, task_id: str, step_key: Optional[str], reason: str):
        self.task_id = task_id
        self.step_key = step_key
        self.reason = reason
        super().__init__(
            f"invalid product workflow step for {task_id}: {step_key!r} ({reason})"
        )


def _validate_product_workflow_state(
    template_id: Optional[str],
    step_key: Optional[str],
    *,
    allow_terminal: bool = True,
) -> None:
    template = str(template_id or "").strip() or None
    step = str(step_key or "").strip() or None
    if template == PRODUCT_WORKFLOW_TEMPLATE_ID:
        allowed = PRODUCT_WORKFLOW_STEP_SET
        if not allow_terminal:
            allowed = allowed - {"done"}
        if step not in allowed:
            raise ProductWorkflowStateError(
                "<unsaved>",
                step,
                f"valid steps: {', '.join(sorted(allowed))}",
            )


def _validate_stored_product_workflow_state(
    conn: sqlite3.Connection, task_id: str
) -> None:
    row = conn.execute(
        "SELECT workflow_template_id, current_step_key FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    try:
        _validate_product_workflow_state(
            row["workflow_template_id"], row["current_step_key"]
        )
    except ProductWorkflowStateError as exc:
        error = ProductWorkflowStateError(task_id, row["current_step_key"], exc.reason)
        with write_txn(conn):
            _append_event(
                conn,
                task_id,
                "completion_blocked_invalid_workflow",
                {
                    "workflow_template_id": row["workflow_template_id"],
                    "current_step_key": row["current_step_key"],
                    "reason": exc.reason,
                },
            )
        raise error from exc


def _infer_product_step(
    *,
    title: str,
    assignee: Optional[str],
    explicit_step: Optional[str],
    product_intent: bool = False,
) -> Optional[str]:
    """Infer the product workflow step for a card from an explicit step, its
    role assignee (architect -> architecture, ...), or a story-shaped title."""
    if explicit_step:
        return explicit_step
    role = (assignee or "").strip().lower()
    if product_intent and role in PRODUCT_WORKFLOW_ROLE_TO_STEP:
        return PRODUCT_WORKFLOW_ROLE_TO_STEP[role]
    if _looks_like_product_story(title):
        return "backlog"
    return None


def _project_is_bound_to_product_board(
    project_id: Optional[str], board_slug: str
) -> bool:
    if not project_id:
        return False
    try:
        from hermes_cli import projects_db as _pdb

        with _pdb.connect_closing() as project_conn:
            project = _pdb.get_project(project_conn, project_id)
        return bool(
            project is not None
            and project.board_slug
            and _normalize_board_slug(project.board_slug)
            == _normalize_board_slug(board_slug)
        )
    except Exception:
        return False


def _can_preserve_project_worktree_on_adoption(
    *,
    workspace_kind: Optional[str],
    workspace_path: Optional[str],
    branch_name: Optional[str],
    project_primary_path: Optional[str],
    task_id: str,
) -> bool:
    """Return whether an adopted project can keep its existing worktree."""
    if (
        workspace_kind != "worktree"
        or not workspace_path
        or not str(branch_name or "").strip()
        or not project_primary_path
    ):
        return False
    try:
        expected = (
            Path(project_primary_path).expanduser().resolve(strict=False)
            / ".worktrees"
            / task_id
        ).resolve(strict=False)
        current_path = Path(str(workspace_path)).expanduser()
        if not current_path.is_absolute():
            return False
        current = current_path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return current == expected


def _repair_product_workflow_metadata_if_needed(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
    actor: str = "framework",
) -> Optional[dict[str, Any]]:
    """Repair legacy/plain role cards on product boards before they dispatch.

    Regression target: product-board stories mistakenly created as plain
    ``assignee=architect`` tasks with NULL workflow fields. On a board whose
    metadata opts into ``preset=product`` / ``product_workflow``, infer the
    missing product step from the assignee/title and persist an audit event.
    Returns the repair metadata when a repair happened, ``None`` otherwise.
    Caller must already hold any desired write transaction. Advancement between
    steps is NOT done here — that is handoff_v2's job; this only fixes metadata.
    """
    board_slug = board if board else get_current_board()
    meta = read_board_metadata(board_slug)
    if not _is_product_board_metadata(meta):
        return None
    row = conn.execute(
        "SELECT id, title, assignee, status, project_id, workflow_template_id, current_step_key, "
        "workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    workflow_template = (row["workflow_template_id"] or "").strip() or None
    current_step = (row["current_step_key"] or "").strip() or None
    if workflow_template and workflow_template != PRODUCT_WORKFLOW_TEMPLATE_ID:
        return None
    if (
        workflow_template == PRODUCT_WORKFLOW_TEMPLATE_ID
        and current_step in PRODUCT_WORKFLOW_STEP_SET
    ):
        return None
    inferred = _infer_product_step(
        title=row["title"] or "",
        assignee=row["assignee"],
        explicit_step=current_step if current_step in PRODUCT_WORKFLOW_STEP_SET else None,
        product_intent=bool(
            workflow_template == PRODUCT_WORKFLOW_TEMPLATE_ID
            or _project_is_bound_to_product_board(row["project_id"], board_slug)
            or _looks_like_product_story(row["title"] or "")
        ),
    )
    if not inferred:
        return None
    updates = ["workflow_template_id = ?", "current_step_key = ?"]
    params: list[Any] = [PRODUCT_WORKFLOW_TEMPLATE_ID, inferred]
    target_status = _column_status_for_step(meta, inferred)
    if row["status"] in {"ready", "review"} and target_status != row["status"]:
        updates.append("status = ?")
        params.append(target_status)
    params.append(task_id)
    conn.execute(
        f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    payload = {
        "workflow_template_id": PRODUCT_WORKFLOW_TEMPLATE_ID,
        "current_step_key": inferred,
        "reason": "product_board_missing_workflow_metadata",
        "actor": actor,
    }
    _append_event(conn, task_id, "workflow_repaired", payload)
    return payload


def _legacy_status(row: Any, meta: Optional[dict] = None) -> str:
    """Compute the legacy ``status`` string from handoff_v2 state.

    ``(current_step_key, running, blocked)`` is the canonical card state;
    this derives the old ``status`` view for existing consumers. Precedence:
    blocked > running > column status for the current phase. Does not
    reconstruct dependency-gated ``"todo"`` — that remains owned by the
    existing dependency-gating code path.
    """
    keys = row.keys()
    blocked = row["blocked"] if "blocked" in keys else None
    if blocked:
        return "blocked"
    running = row["running"] if "running" in keys else None
    if running:
        return "running"
    step_key = row["current_step_key"] if "current_step_key" in keys else None
    return _column_status_for_step(meta, step_key)


def _assert_card_consistent(row: Any) -> None:
    """Raise ``ValueError`` iff a card's flags represent an impossible state.

    Under the handoff_v2 state model ``(phase, running, blocked)``, ``phase``
    is a single field so it can't disagree with itself; the one remaining
    representable contradiction is a card that is both ``running`` (actively
    executing) and ``blocked`` (escalated to a human) at the same time. Every
    other combination -- running-only, blocked-only, neither -- is valid
    regardless of phase, so this deliberately does not validate ``phase``.
    """
    keys = row.keys()
    running = row["running"] if "running" in keys else None
    blocked = row["blocked"] if "blocked" in keys else None
    if running and blocked:
        if "id" in keys:
            raise ValueError(f"card {row['id']} cannot be both running and blocked")
        raise ValueError("card cannot be both running and blocked")


def _sync_legacy_status(conn: sqlite3.Connection, task_id: str, meta: Optional[dict]) -> None:
    """Read back a card's canonical state, enforce the invariant, and store
    the derived legacy ``status`` column.

    Shared by :func:`set_phase`, :func:`set_running`, and :func:`set_blocked`
    so the running/blocked invariant is enforced in exactly one place. Must
    be called from inside the caller's ``write_txn`` block: a raised
    ``ValueError`` here rolls back the whole transaction, so the write that
    would have created the contradiction is never committed.
    """
    row = conn.execute(
        "SELECT current_step_key, running, blocked FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    _assert_card_consistent(row)
    status = _legacy_status(row, meta)
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _apply_v2_flags(
    conn: sqlite3.Connection,
    task_id: str,
    meta: Optional[dict],
    *,
    running: Optional[bool] = None,
    blocked: Optional[bool] = None,
) -> None:
    """v2 seam: set the given running/blocked flag(s) then re-derive legacy
    status via _sync_legacy_status (which asserts the limbo invariant). No-op
    if meta is not a handoff_v2 board. Call from WITHIN the caller's write_txn.

    This is the ONE seam every v2 writer that mutates the canonical
    ``(running, blocked)`` flags goes through -- ``claim_task`` here, and
    the block/terminal/reclaim remediation to follow -- so status and flags
    can never drift apart on a handoff_v2 board.
    """
    if meta is None or not _handoff_v2_enabled(meta):
        return
    sets = []
    params: list = []
    if running is not None:
        sets.append("running = ?")
        params.append(1 if running else 0)
    if blocked is not None:
        sets.append("blocked = ?")
        params.append(1 if blocked else 0)
    if not sets:
        return
    params.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
    _sync_legacy_status(conn, task_id, meta)


def _v2_flags_for_status(status: str) -> Tuple[int, int]:
    """Pure mapping: legacy ``status`` -> the ``(running, blocked)`` flag pair
    that agrees with it. This is the inverse of :func:`_legacy_status`'s
    blocked > running precedence, and the single source of truth for that
    mapping -- shared by :func:`_apply_v2_flags_for_status` (single-card,
    v2-gated) and :func:`migrate_cards_to_v2_flags` (bulk reconciliation).
    """
    if status == "running":
        return (1, 0)
    if status == "blocked":
        return (0, 1)
    return (0, 0)


def _apply_v2_flags_for_status(
    conn: sqlite3.Connection,
    task_id: str,
    new_status: str,
    *,
    board: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """Set the v2 running/blocked flags to MATCH a directly-written legacy
    status, for manual/dashboard/schedule/archive transitions. v2-gated (no-op
    on legacy boards). Runs in the caller's write_txn. Does NOT re-derive
    status (scheduled/archived/todo/triage are not flag-derivable -- the
    caller's explicit status stands); this only keeps the flags from
    disagreeing with it.
    """
    meta = meta or product_board_metadata(board or _board_slug_for_connection(conn))
    if meta is None or not _handoff_v2_enabled(meta):
        return
    running, blocked = _v2_flags_for_status(new_status)
    conn.execute(
        "UPDATE tasks SET running = ?, blocked = ? WHERE id = ?",
        (running, blocked, task_id),
    )


def migrate_cards_to_v2_flags(conn: sqlite3.Connection, *, board: Optional[str] = None) -> int:
    """Phase 6 migration primitive: reconcile every existing card's
    ``(phase, running, blocked)`` state to MATCH its current legacy
    ``status``, the inverse of :func:`_legacy_status`. Reconciles two things,
    in the SAME ``write_txn``:

    1. ``(running, blocked)`` flags -- a board's cards accumulate real
       statuses (running/blocked/ready/...) over time, but the flag columns
       default to 0, so without this, flipping a board to ``handoff_v2``
       would leave an already-running card reading ``status='running',
       running=0``, a direct disagreement with :func:`_legacy_status`. This
       applies the same mapping as CR2's :func:`_apply_v2_flags_for_status`
       (via the shared :func:`_v2_flags_for_status`) to every card via a
       direct ``UPDATE``.
    2. ``current_step_key`` (phase) for terminal ``done`` cards only -- a
       dry run on a copy of a production board found real cards with
       ``status='done'`` still parked at a non-done phase (e.g.
       ``'release_measure'``), legacy completions that predate the "done
       ⟹ phase=done" rule; their derived ``_legacy_status`` reads something
       other than ``'done'``. A single bulk ``UPDATE`` advances
       ``current_step_key`` to ``'done'`` for every ``status='done'`` card
       not already there. No other status's phase is touched --
       running/ready/blocked/review/todo/archived cards keep their workflow
       position exactly as-is -- and ``status`` itself is never written by
       this function.

    Deliberately NOT gated on :func:`_handoff_v2_enabled` -- this function
    *is* the migration, and may be run just before or just after the
    board.json ``handoff_v2`` flip. It never writes ``status``, so it is
    additive, idempotent (a second run recomputes the same flags, and a done
    card already at phase 'done' is left unchanged), and reversible-ish: the
    flags are simply ignored again if the board reverts to legacy, and
    setting a completed card's phase to 'done' is just its correct terminal
    position, so nothing is lost either way. Runs in one ``write_txn``.
    ``board`` is accepted for parity with sibling connection-scoped helpers;
    a connection is already scoped to a single board's tasks table, so it
    does not filter here.

    Returns the number of cards whose flags were reconciled (the phase-for-
    done fixup is a secondary bulk statement and is not separately counted).
    """
    del board  # unused: conn is already scoped to one board's tasks table
    updated = 0
    with write_txn(conn):
        rows = conn.execute("SELECT id, status FROM tasks").fetchall()
        for row in rows:
            running, blocked = _v2_flags_for_status(row["status"])
            cur = conn.execute(
                "UPDATE tasks SET running = ?, blocked = ? WHERE id = ?",
                (running, blocked, row["id"]),
            )
            updated += cur.rowcount
        conn.execute(
            "UPDATE tasks SET current_step_key = 'done' "
            "WHERE status = 'done' AND current_step_key IS NOT 'done'"
        )
    return updated


def set_phase(
    conn: sqlite3.Connection,
    task_id: str,
    phase: str,
    *,
    board: Optional[str] = None,
) -> bool:
    """Move a handoff_v2 card's canonical phase (``current_step_key``).

    No-ops (returns ``False``) on non-handoff_v2 boards — those keep using
    the existing status-based writers. Also returns ``False`` if
    ``task_id`` doesn't exist. On success, re-derives and stores the legacy
    ``status`` column via :func:`_legacy_status` so old consumers reading
    ``status`` directly stay correct.
    """
    meta = product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return False
    scope = conn.execute(
        "SELECT workflow_template_id, current_step_key FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if _is_engine_owned_integration_state(scope):
        return False
    _validate_product_workflow_state(PRODUCT_WORKFLOW_TEMPLATE_ID, phase)
    _validate_resolver_cas_fields({"phase": phase})
    with authorized_governance_write(), write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET current_step_key = ? WHERE id = ?",
            (phase, task_id),
        )
        if cur.rowcount != 1:
            return False
        _sync_legacy_status(conn, task_id, meta)
    return True


def set_running(
    conn: sqlite3.Connection,
    task_id: str,
    running: bool,
    *,
    board: Optional[str] = None,
) -> bool:
    """Set the canonical ``running`` flag on a handoff_v2 card.

    See :func:`set_phase` for the v2-gating / legacy no-op / missing-task
    contract; behaves identically, mutating ``running`` instead of
    ``current_step_key``.
    """
    meta = product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return False
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET running = ? WHERE id = ?",
            (1 if running else 0, task_id),
        )
        if cur.rowcount != 1:
            return False
        _sync_legacy_status(conn, task_id, meta)
    return True


def set_blocked(
    conn: sqlite3.Connection,
    task_id: str,
    blocked: bool,
    *,
    board: Optional[str] = None,
    reason: Optional[str] = None,
) -> bool:
    """Set the canonical ``blocked`` flag on a handoff_v2 card.

    See :func:`set_phase` for the v2-gating / legacy no-op / missing-task
    contract; behaves identically, mutating ``blocked`` instead of
    ``current_step_key``.

    ``reason`` is accepted for interface parity with the eventual
    block-reason/event plumbing, but is intentionally NOT persisted here.
    Full block-reason routing (where it's stored, how it surfaces to
    consumers) is Phase 3 (T1.4+) scope; inventing ad hoc storage for it now
    would just be thrown away.
    """
    meta = product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return False
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET blocked = ? WHERE id = ?",
            (1 if blocked else 0, task_id),
        )
        if cur.rowcount != 1:
            return False
        _sync_legacy_status(conn, task_id, meta)
    return True


def _product_ai_provenance_required(meta: Optional[dict]) -> bool:
    wf = _product_workflow_dict(meta)
    for key in ("ai_provenance_required", "require_ai_provenance"):
        if key in wf:
            return wf.get(key) is not False
    if isinstance(meta, dict):
        for key in ("ai_provenance_required", "require_ai_provenance"):
            if key in meta:
                return meta.get(key) is not False
    return True


def _clean_agent_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("agent", "name", "tool", "provider"):
            if value.get(key):
                return str(value.get(key)).strip() or None
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _agent_compare_key(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _provenance_payload(metadata: Optional[dict]) -> dict:
    if not isinstance(metadata, dict):
        return {}
    raw = metadata.get("ai_provenance")
    if isinstance(raw, dict):
        return raw
    # Accept a flat metadata object for CLI/manual callers while still
    # documenting ``metadata.ai_provenance`` as the canonical shape.
    flat_keys = {
        "writer", "external_writer", "writer_agent", "writer_ai",
        "tester", "tester_agent", "verifier", "verifier_agent",
        "reviewer", "reviewer_agent", "reviewer_ai",
    }
    return metadata if any(key in metadata for key in flat_keys) else {}


def _lookup_provenance_value(prov: dict, *paths: Any) -> Optional[str]:
    for path in paths:
        if isinstance(path, str):
            value = prov.get(path)
        else:
            node: Any = prov
            for part in path:
                if not isinstance(node, dict) or part not in node:
                    node = None
                    break
                node = node.get(part)
            value = node
        cleaned = _clean_agent_name(value)
        if cleaned:
            return cleaned
    return None


def _clean_provenance_text(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("toolchain", "tool", "provider", "model", "name"):
            if value.get(key):
                return str(value.get(key)).strip() or None
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _lookup_provenance_text(prov: dict, *paths: Any) -> Optional[str]:
    for path in paths:
        if isinstance(path, str):
            value = prov.get(path)
        else:
            node: Any = prov
            for part in path:
                if not isinstance(node, dict) or part not in node:
                    node = None
                    break
                node = node.get(part)
            value = node
        cleaned = _clean_provenance_text(value)
        if cleaned:
            return cleaned
    return None


def _writer_agent_from_metadata(metadata: Optional[dict]) -> Optional[str]:
    prov = _provenance_payload(metadata)
    return _lookup_provenance_value(
        prov,
        ("writer", "agent"),
        ("external_writer", "agent"),
        ("implementation", "agent"),
        "writer_agent",
        "writer_ai",
        "external_writer_agent",
    )


def _tester_agent_from_metadata(metadata: Optional[dict]) -> Optional[str]:
    prov = _provenance_payload(metadata)
    return _lookup_provenance_value(
        prov,
        ("tester", "agent"),
        ("verifier", "agent"),
        ("test", "agent"),
        "tester_agent",
        "verifier_agent",
        "test_agent",
    )


def _reviewer_agent_from_metadata(metadata: Optional[dict]) -> Optional[str]:
    prov = _provenance_payload(metadata)
    return _lookup_provenance_value(
        prov,
        ("reviewer", "agent"),
        ("review", "agent"),
        "reviewer_agent",
        "reviewer_ai",
        "review_agent",
    )


def _latest_product_writer_agent(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    rows = conn.execute(
        """
        SELECT profile, outcome, metadata FROM task_runs
         WHERE task_id = ?
           AND metadata IS NOT NULL AND metadata != ''
         ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
        """,
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except Exception:
            metadata = {}
        if not _ordinary_product_evidence(row["profile"], row["outcome"], metadata):
            continue
        writer = _writer_agent_from_metadata(metadata if isinstance(metadata, dict) else {})
        if writer:
            return writer
    return None


def _executor_from_run_metadata(metadata: object) -> Optional[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return None
    executor = metadata.get("executor")
    required = {"profile", "provider", "model", "effort", "surface"}
    if not isinstance(executor, dict) or not required <= set(executor):
        return None
    cleaned = {key: str(executor.get(key) or "").strip() for key in required}
    if not all(cleaned.values()):
        return None
    cleaned["source"] = str(executor.get("source") or "dispatcher")
    cleaned["version"] = 1
    return cleaned


def _ordinary_product_evidence(profile: object, outcome: object, metadata: object) -> bool:
    """Recovery attempts carry no product authority, even at a product phase.

    Durable stamps cover configured Resolver roles. The literal profile is a
    conservative fallback for historical recovery runs without those stamps.
    Failed ordinary attempts remain eligible records so they invalidate older
    success; eligibility is deliberately not an outcome-success filter.
    """
    return (
        str(profile or "").strip().casefold() != "resolver"
        and outcome not in ("preflight", "preflight_resolved")
        and not (isinstance(metadata, dict) and "resolver" in metadata)
    )


def _latest_product_step_executor(
    conn: sqlite3.Connection,
    task_id: str,
    step_key: str,
) -> Optional[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT profile, outcome, metadata FROM task_runs
         WHERE task_id = ? AND step_key = ?
         ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
        """,
        (task_id, step_key),
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not _ordinary_product_evidence(row["profile"], row["outcome"], metadata):
            continue
        # Missing identity on the latest ordinary attempt is not permission to
        # borrow an older developer's identity.
        return _executor_from_run_metadata(metadata)
    return None


def _canonicalize_product_ai_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    step_key: Optional[str],
    metadata: Optional[dict],
) -> Optional[dict]:
    """Prefer dispatcher-stamped executor facts over worker-authored aliases."""
    step = str(step_key or "")
    role_key = {
        "development": "writer",
        "test": "tester",
        "review": "reviewer",
    }.get(step)
    if role_key is None:
        return metadata
    task = get_task(conn, task_id)
    run = get_run(conn, task.current_run_id) if task and task.current_run_id else None
    executor = _executor_from_run_metadata(run.metadata if run else None)
    writer_executor = (
        _latest_product_step_executor(conn, task_id, "development")
        if step == "review"
        else None
    )
    if executor is None and writer_executor is None:
        return metadata

    canonical = dict(metadata or {})
    provenance = canonical.get("ai_provenance")
    provenance = dict(provenance) if isinstance(provenance, dict) else {}

    def _stamp(role: str, identity: dict[str, Any]) -> None:
        existing = provenance.get(role)
        role_facts = dict(existing) if isinstance(existing, dict) else {}
        role_facts.update(
            {
                "agent": identity["provider"],
                "provider": identity["provider"],
                "model": identity["model"],
                "effort": identity["effort"],
                "profile": identity["profile"],
                "surface": identity["surface"],
            }
        )
        provenance[role] = role_facts

    if executor is not None:
        _stamp(role_key, executor)
    if writer_executor is not None:
        _stamp("writer", writer_executor)
    canonical["ai_provenance"] = provenance
    if step == "review":
        # Release evidence is read from ``ai_provenance.reviewer``. Reviewer
        # runs have recorded the same facts at the metadata root instead, in
        # more than one shape, so canonicalize them here rather than relying
        # on the worker to author the right keys.
        pinned_review_head = ""
        if run is not None and isinstance(run.metadata, dict):
            pinned_review_head = str(run.metadata.get("review_head_sha") or "").strip()
        evidence_metadata = dict(canonical)
        if pinned_review_head:
            evidence_metadata["review_head_sha"] = pinned_review_head
        verdict, reviewed_branch, reviewed_commit = _reviewer_evidence(evidence_metadata)
        reviewer_facts = provenance.get("reviewer")
        reviewer_facts = dict(reviewer_facts) if isinstance(reviewer_facts, dict) else {}
        for key, value in (
            ("verdict", verdict),
            ("reviewed_branch", reviewed_branch),
            ("reviewed_commit", reviewed_commit),
        ):
            if value and (
                (key == "reviewed_commit" and pinned_review_head)
                or not str(reviewer_facts.get(key) or "").strip()
            ):
                reviewer_facts[key] = value
        if reviewer_facts:
            provenance["reviewer"] = reviewer_facts
            canonical["ai_provenance"] = provenance
    return canonical


def _record_product_provenance_rejection(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    step_key: str,
    reason: str,
    missing: Optional[list[str]] = None,
    writer_agent: Optional[str] = None,
    reviewer_agent: Optional[str] = None,
) -> None:
    with write_txn(conn):
        _append_event(
            conn,
            task_id,
            PRODUCT_PROVENANCE_BLOCKED_EVENT,
            {
                "step_key": step_key,
                "reason": reason,
                "missing": missing or [],
                "writer_agent": writer_agent,
                "reviewer_agent": reviewer_agent,
            },
        )


def _reject_worker_recovery_metadata(
    conn: sqlite3.Connection, task_id: str, step: str, metadata: Optional[dict],
) -> None:
    if isinstance(metadata, dict) and "resolver" in metadata:
        reason = "resolver metadata is reserved for canonical recovery, not ordinary completion"
        _record_product_provenance_rejection(conn, task_id, step_key=step, reason=reason)
        raise ProductProvenanceError(reason, task_id, step)


def _validate_product_ai_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    step_key: Optional[str],
    metadata: Optional[dict],
    meta: Optional[dict],
) -> None:
    step = str(step_key or "")
    _reject_worker_recovery_metadata(conn, task_id, step, metadata)
    if step not in PRODUCT_PROVENANCE_REQUIRED_STEPS:
        return
    if not _product_ai_provenance_required(meta):
        return
    if step == "development":
        writer = _writer_agent_from_metadata(metadata)
        if not writer:
            reason = (
                "Development completion requires AI provenance: set "
                "metadata.ai_provenance.writer.agent (or writer_agent) to "
                "the coding AI that wrote the change."
            )
            _record_product_provenance_rejection(
                conn, task_id, step_key=step, reason=reason,
                missing=["ai_provenance.writer.agent"],
            )
            raise ProductProvenanceError(reason, task_id, step)
        return
    if step == "test":
        tester = _tester_agent_from_metadata(metadata)
        if not tester:
            reason = (
                "Test completion requires AI provenance: set "
                "metadata.ai_provenance.tester.agent or verifier.agent to "
                "the AI/persona that performed verification."
            )
            _record_product_provenance_rejection(
                conn, task_id, step_key=step, reason=reason,
                missing=["ai_provenance.tester.agent"],
            )
            raise ProductProvenanceError(reason, task_id, step)
        return
    if step == "review":
        task = get_task(conn, task_id)
        current_run = (
            get_run(conn, task.current_run_id)
            if task is not None and task.current_run_id is not None
            else None
        )
        reviewer_executor = _executor_from_run_metadata(
            current_run.metadata if current_run is not None else None
        )
        writer_executor = _latest_product_step_executor(
            conn, task_id, "development"
        )
        if (reviewer_executor is None) != (writer_executor is None):
            writer_provider = (
                writer_executor["provider"]
                if writer_executor is not None
                else None
            )
            reviewer_provider = (
                reviewer_executor["provider"]
                if reviewer_executor is not None
                else None
            )
            reason = (
                "Review completion rejected: canonical writer and reviewer "
                "executor identities are both required when either dispatched "
                "run is canonically stamped; Hermes will not compare a trusted "
                "runtime identity with a worker-authored alias."
            )
            _record_product_provenance_rejection(
                conn,
                task_id,
                step_key=step,
                reason=reason,
                writer_agent=writer_provider,
                reviewer_agent=reviewer_provider,
            )
            raise ProductProvenanceError(reason, task_id, step)

        reviewer = _reviewer_agent_from_metadata(metadata)
        supplied_writer = _writer_agent_from_metadata(metadata)
        writer = supplied_writer or _latest_product_writer_agent(conn, task_id)
        if reviewer_executor is not None and writer_executor is not None:
            reviewer = reviewer_executor["provider"]
            writer = writer_executor["provider"]
        missing: list[str] = []
        if not reviewer:
            missing.append("ai_provenance.reviewer.agent")
        if not writer:
            missing.append("ai_provenance.writer.agent or prior development writer")
        if missing:
            reason = (
                "Review completion requires AI provenance for both the "
                "reviewer and the writer being reviewed."
            )
            _record_product_provenance_rejection(
                conn, task_id, step_key=step, reason=reason,
                missing=missing, writer_agent=writer, reviewer_agent=reviewer,
            )
            raise ProductProvenanceError(reason, task_id, step, missing=missing)
        if _agent_compare_key(writer) == _agent_compare_key(reviewer):
            reason = (
                "Review completion rejected: canonical reviewer provider must "
                "differ from the canonical writer provider "
                f"(both were {reviewer!r}). Select an independently configured "
                "reviewer runtime; Hermes will not choose a fallback."
            )
            _record_product_provenance_rejection(
                conn, task_id, step_key=step, reason=reason,
                writer_agent=writer, reviewer_agent=reviewer,
            )
            raise ProductProvenanceError(reason, task_id, step)


def _ai_provenance_summary_from_metadata(
    step_key: Optional[str],
    metadata: Optional[dict],
    run_summary: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    step = str(step_key or "")
    out: dict[str, Any] = {}
    writer = _writer_agent_from_metadata(metadata)
    tester = _tester_agent_from_metadata(metadata)
    reviewer = _reviewer_agent_from_metadata(metadata)
    if writer:
        out["writer_agent"] = writer
    if tester:
        out["tester_agent"] = tester
    if reviewer:
        out["reviewer_agent"] = reviewer
    prov = _provenance_payload(metadata)
    if isinstance(prov, dict):
        model = _lookup_provenance_text(
            prov,
            "model",
            ("writer", "model"),
            ("external_writer", "model"),
            ("tester", "model"),
            ("verifier", "model"),
            ("reviewer", "model"),
            ("review", "model"),
        )
        toolchain = _lookup_provenance_text(
            prov,
            "toolchain",
            ("writer", "toolchain"),
            ("external_writer", "toolchain"),
            ("tester", "toolchain"),
            ("verifier", "toolchain"),
            ("reviewer", "toolchain"),
            ("review", "toolchain"),
            "tool",
            ("writer", "tool"),
            ("tester", "tool"),
            ("reviewer", "tool"),
        )
        if model:
            out["model"] = model
        if toolchain:
            out["toolchain"] = toolchain
        for key in ("branch", "worktree", "commit"):
            value = prov.get(key)
            if value:
                out[key] = str(value)
        writer_obj = prov.get("writer") or prov.get("external_writer")
        if isinstance(writer_obj, dict):
            for key in ("model", "toolchain", "effort", "branch", "worktree", "commit"):
                value = writer_obj.get(key)
                if value and key not in out:
                    out[key] = str(value)
        tester_obj = prov.get("tester") or prov.get("verifier")
        if isinstance(tester_obj, dict):
            for key in ("model", "toolchain"):
                value = tester_obj.get(key)
                if value and key not in out:
                    out[key] = str(value)
            value = tester_obj.get("result") or tester_obj.get("verdict")
            if value:
                out["test_result"] = str(value)
        reviewer_obj = prov.get("reviewer") or prov.get("review")
        if isinstance(reviewer_obj, dict):
            for key in ("model", "toolchain", "verdict", "reviewed_commit", "reviewed_branch"):
                value = reviewer_obj.get(key)
                if value:
                    out[key] = str(value)
    if run_summary:
        summary_text = str(run_summary).strip()
        if summary_text:
            out["summary"] = summary_text
            if step == "test" or tester or out.get("test_result"):
                out["verification_summary"] = summary_text
    if out:
        out["step_key"] = step
    return out


def _merge_ai_provenance_summary(
    aggregate: dict[str, Any],
    step_key: Optional[str],
    metadata: Optional[dict],
    run_summary: Optional[str] = None,
) -> None:
    summary = _ai_provenance_summary_from_metadata(step_key, metadata, run_summary)
    if not summary:
        return
    step = str(step_key or summary.get("step_key") or "unknown")
    by_step = aggregate.setdefault("by_step", {})
    by_step[step] = summary
    for key in (
        "writer_agent", "tester_agent", "reviewer_agent",
        "model", "toolchain", "branch", "worktree", "commit", "test_result",
        "verdict", "reviewed_commit", "reviewed_branch",
    ):
        if summary.get(key):
            aggregate[key] = summary[key]
    if summary.get("verification_summary"):
        aggregate["verification_summary"] = summary["verification_summary"]
    writer = aggregate.get("writer_agent")
    reviewer = aggregate.get("reviewer_agent")
    if writer and reviewer:
        aggregate["review_rule"] = {
            "writer_agent": writer,
            "reviewer_agent": reviewer,
            "different_agent": _agent_compare_key(writer) != _agent_compare_key(reviewer),
        }


def _latest_unresolved_product_preflight(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[tuple[int, dict]]:
    rows = conn.execute(
        """
        SELECT id, kind, payload FROM task_events
         WHERE task_id = ?
           AND kind IN (?, 'human_input_preflight_resolved', 'blocked',
                        'block_loop_detected', 'completed', 'workflow_advanced')
         ORDER BY id DESC
         LIMIT 1
        """,
        (task_id, PRODUCT_WORKFLOW_PRECHECK_EVENT),
    ).fetchall()
    if not rows or rows[0]["kind"] != PRODUCT_WORKFLOW_PRECHECK_EVENT:
        return None
    try:
        payload = json.loads(rows[0]["payload"]) if rows[0]["payload"] else {}
    except Exception:
        payload = {}
    return int(rows[0]["id"]), (payload if isinstance(payload, dict) else {})


def _recovery_metadata_for_claim(
    conn: sqlite3.Connection,
    task_id: str,
    assignee: Optional[str],
    step_key: Optional[str],
) -> dict:
    """Bind run purpose before identity resolution or spawning can fail."""
    entry = _latest_unresolved_product_preflight(conn, task_id)
    if entry is None:
        return {}
    _, preflight = entry
    if (preflight.get("hermes_assignee") == assignee
            and preflight.get("step_key") == step_key):
        return {"resolver": {"profile": assignee}}
    return {}


def resolver_expected_snapshot(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[dict[str, Any]]:
    """Return the exact compare-and-swap object accepted by the Resolver."""
    row = conn.execute(
        """
        SELECT status, assignee, project_id, workflow_template_id,
               current_step_key, workspace_kind, workspace_path, branch_name,
               running, blocked, current_run_id
          FROM tasks
         WHERE id = ?
        """,
        (task_id,),
    ).fetchone()
    preflight = _latest_unresolved_product_preflight(conn, task_id)
    if row is None or preflight is None:
        return None
    preflight_event_id, _payload = preflight
    snapshot = {
        "task_id": task_id,
        "run_id": row["current_run_id"],
        "preflight_event_id": preflight_event_id,
        "status": row["status"],
        "phase": row["current_step_key"],
        "assignee": row["assignee"],
        "project_id": row["project_id"],
        "workflow_template_id": row["workflow_template_id"],
        "workspace_kind": row["workspace_kind"],
        "workspace_path": row["workspace_path"],
        "branch_name": row["branch_name"],
        "running": bool(row["running"]),
        "blocked": bool(row["blocked"]),
    }
    _validate_resolver_cas_fields(snapshot)
    snapshot.pop("task_id")
    return snapshot


def has_unresolved_product_preflight(
    conn: sqlite3.Connection,
    task_id: str,
) -> bool:
    """Return whether product obstacle resolution must run before completion."""
    return _latest_unresolved_product_preflight(conn, task_id) is not None


#: The privileged repair profile. Not an ordinary worker: ``_default_spawn``
#: pins it to exactly ``resolver_readonly`` and ``_check_ordinary_worker_mode``
#: withholds every ordinary lifecycle exit from it.
RESOLVER_PROFILE = "resolver"


def resolver_routing_error(
    conn: sqlite3.Connection,
    task_id: str,
    assignee: Optional[str],
) -> Optional[str]:
    """Diagnose an incompatible task -> Resolver routing; ``None`` when valid.

    Resolver's only mutation is ``kanban_resolve``, which applies to an
    unresolved product preflight. Routed to a card without one it holds no
    lifecycle exit at all — not ``kanban_complete``, ``kanban_block``,
    attachments, creation, or linking — so the worker runs, finds nothing
    that can end its run, and the card deadlocks (2026-08-02 default-board
    incident: four goal cards assigned to ``resolver`` by hand).

    The rejection names the contract rather than letting the run fail later
    as "the worker never called kanban_complete".
    """
    if _canonical_assignee(assignee) != RESOLVER_PROFILE:
        return None
    if has_unresolved_product_preflight(conn, task_id):
        return None
    return (
        f"routing: task {task_id} has no unresolved product preflight, so it "
        f"cannot be assigned to the privileged '{RESOLVER_PROFILE}' profile. "
        "Resolver runs read-only (resolver_readonly) and its only mutation is "
        "kanban_resolve; it has no kanban_complete/kanban_block exit. Assign an "
        "ordinary worker profile instead."
    )


def _complete_product_workflow_step(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    expected_run_id: Optional[int] = None,
    product_role_assignees: Optional[dict[str, str]] = None,
) -> Optional[bool]:
    """Handle product-board non-terminal completion.

    Returns ``None`` when normal terminal completion should run. Returns a
    boolean when the product workflow consumed the completion.
    """
    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None:
        return None

    # Provenance rejection must happen before the mutating transaction so the
    # audit event can commit while the card itself remains untouched.
    pre_row = conn.execute(
        "SELECT status, current_run_id, workflow_template_id, current_step_key "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if pre_row is None:
        return False
    pre_step = pre_row["current_step_key"]
    if pre_row["workflow_template_id"] != "product" and not pre_step:
        return None
    if _latest_unresolved_product_preflight(conn, task_id):
        raise ValueError("unresolved product preflight; use kanban_resolve")
    transition = PRODUCT_WORKFLOW_TRANSITIONS.get(str(pre_step or ""))
    if transition is not None and transition.get("next_step"):
        metadata = _canonicalize_product_ai_provenance(
            conn, task_id, pre_step, metadata,
        )
        _validate_product_ai_provenance(
            conn, task_id, pre_step, metadata, meta,
        )

    with authorized_governance_write(), write_txn(conn):
        row = conn.execute(
            "SELECT status, assignee, workflow_template_id, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        step_key = row["current_step_key"]
        if row["workflow_template_id"] != "product" and not step_key:
            return None

        if _latest_unresolved_product_preflight(conn, task_id):
            raise ValueError("unresolved product preflight; use kanban_resolve")

        transition = PRODUCT_WORKFLOW_TRANSITIONS.get(str(step_key or ""))
        if transition is None:
            return None
        next_step = transition.get("next_step")
        if not next_step:
            return None
        next_status = str(transition.get("status") or _column_status_for_step(meta, next_step) or "ready")
        if next_status not in VALID_STATUSES or next_status in {"running", "done", "archived", "blocked"}:
            next_status = _column_status_for_step(meta, next_step)
        role = transition.get("assignee_role")
        next_assignee = _product_role_assignee(meta, role, product_role_assignees)
        _validate_resolver_cas_fields(
            {
                "status": next_status,
                "assignee": next_assignee,
                "workflow_template_id": PRODUCT_WORKFLOW_TEMPLATE_ID,
                "current_step_key": next_step,
            }
        )
        sql = """
            UPDATE tasks
               SET status        = ?,
                   assignee      = ?,
                   result        = ?,
                   completed_at  = NULL,
                   claim_lock    = NULL,
                   claim_expires = NULL,
                   worker_pid    = NULL,
                   block_kind    = NULL,
                   block_recurrences = 0,
                   workflow_template_id = 'product',
                   current_step_key = ?
             WHERE id = ?
               AND status IN ('running', 'ready', 'blocked', 'review')
        """ + ("" if expected_run_id is None else " AND current_run_id = ?")
        params = (
            next_status, next_assignee, result, next_step, task_id,
        ) if expected_run_id is None else (
            next_status, next_assignee, result, next_step, task_id, int(expected_run_id),
        )
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="advanced",
            status="completed",
            summary=summary if summary is not None else result,
            metadata=metadata,
            expected_run_id=expected_run_id,
        )
        if expected_run_id is not None and run_id is None:
            raise RuntimeError("workflow run ownership changed")
        if run_id is None and (summary or result or metadata):
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="advanced",
                summary=summary if summary is not None else result,
                metadata=metadata,
                step_key=step_key,
            )
        ev_summary = (summary if summary is not None else result) or ""
        advanced_payload: dict[str, Any] = {
            "from_step": step_key,
            "to_step": next_step,
            "status": next_status,
            "assignee_role": role,
            "assignee": next_assignee,
            "summary": ev_summary.strip().splitlines()[0][:400] if ev_summary else None,
        }
        provenance_summary = _ai_provenance_summary_from_metadata(step_key, metadata)
        if provenance_summary:
            advanced_payload["ai_provenance"] = provenance_summary
        _append_event(
            conn,
            task_id,
            "workflow_advanced",
            advanced_payload,
            run_id=run_id,
        )
    _clear_failure_counter(conn, task_id)
    recompute_ready(conn)
    return True


_RESOLVER_EXPECTED_KEYS = frozenset({
    "run_id",
    "preflight_event_id",
    "status",
    "phase",
    "assignee",
    "project_id",
    "workflow_template_id",
    "workspace_kind",
    "workspace_path",
    "branch_name",
    "running",
    "blocked",
})

# These fields are copied into the exact Resolver CAS and into the task view.
# Bound them at both the Unicode-character and UTF-8-byte level so a valid row
# cannot make the fixed 96 KiB Resolver response impossible. The limits are
# deliberately field-specific: paths need more room than enum-like workflow
# keys, while all remain far below the response ceiling after the values are
# repeated in the task and expected-snapshot objects.
_RESOLVER_CAS_FIELD_LIMITS: dict[str, tuple[int, int]] = {
    "task_id": (128, 512),
    "status": (32, 128),
    "assignee": (256, 1_024),
    "project_id": (256, 1_024),
    "workflow_template_id": (128, 512),
    "current_step_key": (128, 512),
    "phase": (128, 512),
    "workspace_kind": (32, 128),
    "workspace_path": (4_096, 16_384),
    "branch_name": (1_024, 4_096),
}


def _validate_resolver_cas_fields(fields: Mapping[str, Any]) -> None:
    """Reject values that cannot fit the exact Resolver CAS response.

    ``None`` remains valid for nullable task metadata. Values are not trimmed,
    truncated, stringified, or otherwise changed: exact CAS requires the
    persisted value to be the value that was validated before the write.
    """
    for field, value in fields.items():
        limits = _RESOLVER_CAS_FIELD_LIMITS.get(field)
        if limits is None or value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string for an exact Resolver snapshot")
        max_chars, max_bytes = limits
        value_bytes = len(value.encode("utf-8"))
        if len(value) > max_chars or value_bytes > max_bytes:
            raise ValueError(
                f"{field} exceeds exact Resolver snapshot bound "
                f"({max_chars} characters/{max_bytes} UTF-8 bytes)"
            )


def _validate_adopted_handoff_sha(
    conn: sqlite3.Connection,
    task_id: str,
    sha: str,
) -> str:
    """Validate an evidence-preserving Development handoff adoption."""
    normalized_sha = str(sha or "").strip()
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='handoff' "
        "ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    matched = False
    for event_row in rows:
        try:
            payload = json.loads(event_row["payload"]) if event_row["payload"] else {}
        except Exception:
            payload = {}
        if (
            isinstance(payload, dict)
            and payload.get("from_step") == "development"
            and str(payload.get("sha") or "").strip() == normalized_sha
        ):
            matched = True
            break
    if not matched:
        raise ValueError(
            "adopt_handoff_sha requires a same-task Development handoff event"
        )

    task = conn.execute(
        "SELECT project_id, workspace_kind, workspace_path, branch_name "
        "FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if (
        task is None
        or not task["project_id"]
        or task["workspace_kind"] != "worktree"
        or not task["workspace_path"]
        or not task["branch_name"]
    ):
        raise ValueError("adopt_handoff_sha requires a project-bound task worktree")
    workspace = Path(task["workspace_path"])
    repo_root = _git_toplevel(workspace)
    if repo_root is None:
        raise ValueError("adopt_handoff_sha task worktree is not a git repository")

    def git_output(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("adopt_handoff_sha git validation failed")
        return (result.stdout or "").strip()

    if git_output("status", "--porcelain"):
        raise ValueError("adopt_handoff_sha requires a clean task worktree")
    git_output("cat-file", "-e", f"{normalized_sha}^{{commit}}")
    branch_head = git_output("rev-parse", str(task["branch_name"]))
    checked_out_head = git_output("rev-parse", "HEAD")
    if normalized_sha != branch_head or normalized_sha != checked_out_head:
        raise ValueError("adopt_handoff_sha must equal the current task branch HEAD")
    return normalized_sha


def _latest_resolver_repair_payload(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id=? AND kind='resolver_repair_applied' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def resolve_product_preflight(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str],
    request: dict[str, Any],
    resolver_profile: str,
    resolver_model: Optional[str],
) -> bool:
    """Apply one Resolver decision under a complete task/run/preflight CAS."""
    if not isinstance(request, dict):
        raise ValueError("resolver request must be an object")
    decision = request.get("decision")
    allowed_keys = {
        "decision", "fault_domain", "diagnosis", "reason", "expected",
    }
    if decision not in {"resume", "repair", "escalate"}:
        raise ValueError("decision must be resume, repair, or escalate")
    if decision == "repair":
        allowed_keys.add("repair")
    if set(request) != allowed_keys:
        raise ValueError("resolver request contains missing or unexpected fields")
    fault_domain = request.get("fault_domain")
    if fault_domain not in {"task_state", "framework"}:
        raise ValueError("fault_domain must be task_state or framework")
    if fault_domain == "framework" and decision != "escalate":
        raise ValueError("framework faults must escalate")
    diagnosis = request.get("diagnosis")
    reason = request.get("reason")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        raise ValueError("diagnosis is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required")
    expected = request.get("expected")
    if not isinstance(expected, dict) or set(expected) != _RESOLVER_EXPECTED_KEYS:
        raise ValueError("expected must contain the complete Resolver snapshot")
    _validate_resolver_cas_fields(expected)
    repair = request.get("repair")
    if decision == "repair":
        if not isinstance(repair, dict) or not repair:
            raise ValueError("repair must contain workflow, adopt_handoff_sha, or both")
        if not set(repair) <= {"workflow", "adopt_handoff_sha"}:
            raise ValueError("repair contains unexpected fields")
        workflow_repair = repair.get("workflow")
        if workflow_repair is not None:
            if not isinstance(workflow_repair, dict) or not workflow_repair:
                raise ValueError("repair.workflow must contain at least one field")
            if not set(workflow_repair) <= {"phase", "assignee", "project_id"}:
                raise ValueError("repair.workflow contains unexpected fields")
        adopted_sha = repair.get("adopt_handoff_sha")
        if adopted_sha is not None and (
            not isinstance(adopted_sha, str) or not adopted_sha.strip()
        ):
            raise ValueError("repair.adopt_handoff_sha must be a non-empty string")
        if workflow_repair is None and adopted_sha is None:
            raise ValueError("repair must contain at least one semantic field")

    board = board or _board_slug_for_connection(conn)
    meta = product_board_metadata(board)
    if meta is None:
        raise ValueError("Resolver decisions require a product board")
    with authorized_governance_write(), write_txn(conn):
        row = conn.execute(
            "SELECT status, assignee, project_id, workflow_template_id, "
            "current_step_key, workspace_kind, workspace_path, branch_name, "
            "running, blocked, current_run_id, title FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        preflight_entry = _latest_unresolved_product_preflight(conn, task_id)
        run_id = expected.get("run_id")
        run = conn.execute(
            "SELECT id, task_id, status, profile, ended_at FROM task_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if row is None or preflight_entry is None:
            raise TaskSnapshotConflict("resolving preflight", {})
        preflight_event_id, preflight = preflight_entry
        current = {
            "run_id": row["current_run_id"],
            "preflight_event_id": preflight_event_id,
            "status": row["status"],
            "phase": row["current_step_key"],
            "assignee": row["assignee"],
            "project_id": row["project_id"],
            "workflow_template_id": row["workflow_template_id"],
            "workspace_kind": row["workspace_kind"],
            "workspace_path": row["workspace_path"],
            "branch_name": row["branch_name"],
            "running": bool(row["running"]),
            "blocked": bool(row["blocked"]),
        }
        _validate_resolver_cas_fields(current)
        if current != expected:
            raise TaskSnapshotConflict("resolving preflight", current)
        if (
            run is None
            or int(run["id"]) != int(run_id)
            or run["task_id"] != task_id
            or run["status"] != "running"
            or run["ended_at"] is not None
            or run["profile"] != resolver_profile
            or str(preflight.get("hermes_assignee") or "") != resolver_profile
        ):
            raise TaskSnapshotConflict("resolving preflight", current)

        resume_step = str(preflight.get("step_key") or row["current_step_key"] or "backlog")
        resume_status = str(
            preflight.get("resume_status")
            or _column_status_for_step(meta, resume_step)
            or "ready"
        )
        if resume_status not in VALID_STATUSES or resume_status in {
            "running", "done", "archived", "blocked",
        }:
            resume_status = _column_status_for_step(meta, resume_step)
        original_assignee = str(preflight.get("original_assignee") or "").strip() or None
        run_metadata: dict[str, Any] = {}
        run_metadata.update({
            "resolver": {
                "profile": resolver_profile,
                "model": resolver_model,
            },
            "fault_domain": fault_domain,
            "diagnosis": diagnosis.strip(),
            "reason": reason.strip(),
        })
        repair_before: Optional[dict[str, Any]] = None
        repair_after: Optional[dict[str, Any]] = None
        normalized_repair: Optional[dict[str, Any]] = None

        if decision == "repair":
            workflow_repair = repair.get("workflow") if isinstance(repair, dict) else None
            workflow_repair = workflow_repair if isinstance(workflow_repair, dict) else {}
            adopted_sha = repair.get("adopt_handoff_sha") if isinstance(repair, dict) else None
            if adopted_sha is not None:
                adopted_sha = _validate_adopted_handoff_sha(
                    conn, task_id, str(adopted_sha),
                )
            phase = str(
                workflow_repair.get("phase") or preflight.get("step_key")
                or row["current_step_key"] or "backlog"
            ).strip()
            allowed_phases = {"backlog", "architecture", "development", "test", "review"}
            if phase not in allowed_phases:
                raise ValueError("repair.workflow.phase must be a non-terminal product phase")
            phase_roles = {
                "backlog": "productowner",
                "architecture": "architect",
                "development": "developer",
                "test": "tester",
                "review": "reviewer",
            }
            ordinary_assignee = _product_role_assignee(meta, phase_roles[phase])
            if ordinary_assignee is None and phase == str(preflight.get("step_key") or ""):
                ordinary_assignee = original_assignee
            supplied_assignee = workflow_repair.get("assignee")
            if supplied_assignee is not None:
                supplied_assignee = str(supplied_assignee).strip()
                if supplied_assignee != ordinary_assignee:
                    raise ValueError("repair.workflow.assignee does not match board phase role")
            if ordinary_assignee is None:
                raise ValueError("board has no assignee for repaired phase")

            project_id = workflow_repair.get("project_id", row["project_id"])
            if project_id is not None:
                project_id = str(project_id).strip() or None
            workspace_kind = row["workspace_kind"]
            workspace_path = row["workspace_path"]
            branch_name = row["branch_name"]
            if project_id:
                from hermes_constants import get_default_hermes_root
                from hermes_cli import projects_db as _pdb

                with _pdb.connect_closing(
                    get_default_hermes_root() / "projects.db"
                ) as project_conn:
                    project = _pdb.get_project(project_conn, project_id)
                if project is None:
                    raise ValueError(f"unknown project: {project_id}")
                if (
                    not project.board_slug
                    or _normalize_board_slug(project.board_slug)
                    != _normalize_board_slug(board)
                ):
                    raise ValueError("repair project is not bound to the active board")
                project_id = project.id
                if project_id != row["project_id"]:
                    if not project.primary_path:
                        raise ValueError("repair project has no primary path")
                    if not _can_preserve_project_worktree_on_adoption(
                        workspace_kind=row["workspace_kind"],
                        workspace_path=row["workspace_path"],
                        branch_name=row["branch_name"],
                        project_primary_path=project.primary_path,
                        task_id=task_id,
                    ):
                        workspace_kind = "worktree"
                        workspace_path = os.path.join(
                            project.primary_path, ".worktrees", task_id,
                        )
                        branch_name = _pdb.branch_name_for(
                            project, task_id, title=row["title"],
                        )

            next_status = _column_status_for_step(meta, phase)
            if next_status not in VALID_STATUSES or next_status in {
                "running", "blocked", "done", "archived",
            }:
                next_status = "ready"
            repair_before = {
                "phase": row["current_step_key"],
                "assignee": row["assignee"],
                "project_id": row["project_id"],
                "status": row["status"],
                "workspace_kind": row["workspace_kind"],
                "workspace_path": row["workspace_path"],
                "branch_name": row["branch_name"],
            }
            repair_after = {
                "phase": phase,
                "assignee": ordinary_assignee,
                "project_id": project_id,
                "status": next_status,
                "workspace_kind": workspace_kind,
                "workspace_path": workspace_path,
                "branch_name": branch_name,
            }
            normalized_repair = {
                "workflow": {
                    key: value for key, value in {
                        "phase": phase,
                        "assignee": ordinary_assignee,
                        "project_id": project_id,
                    }.items() if value is not None
                }
            }
            if adopted_sha is not None:
                normalized_repair["adopt_handoff_sha"] = adopted_sha
                repair_before["adopt_handoff_sha"] = None
                repair_after["adopt_handoff_sha"] = adopted_sha
            _validate_resolver_cas_fields(
                {
                    "status": next_status,
                    "assignee": ordinary_assignee,
                    "project_id": project_id,
                    "workflow_template_id": PRODUCT_WORKFLOW_TEMPLATE_ID,
                    "current_step_key": phase,
                    "workspace_kind": workspace_kind,
                    "workspace_path": workspace_path,
                    "branch_name": branch_name,
                }
            )
            cur = conn.execute(
                "UPDATE tasks SET status=?, assignee=?, project_id=?, "
                "workspace_kind=?, workspace_path=?, branch_name=?, "
                "workflow_template_id='product', current_step_key=?, "
                "running=0, blocked=0, claim_lock=NULL, claim_expires=NULL, "
                "worker_pid=NULL, block_kind=NULL, block_recurrences=0 "
                "WHERE id=? AND current_run_id=?",
                (
                    next_status, ordinary_assignee, project_id, workspace_kind,
                    workspace_path, branch_name, phase, task_id, int(run_id),
                ),
            )
            outcome = "preflight_repaired"
        elif decision == "escalate":
            cur = conn.execute(
                "UPDATE tasks SET status='blocked', assignee='default', running=0, "
                "blocked=1, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                "WHERE id=? AND current_run_id=?",
                (task_id, int(run_id)),
            )
            outcome = "preflight_escalated"
            next_status = "blocked"
        else:
            cur = conn.execute(
                "UPDATE tasks SET status=?, assignee=?, result=NULL, "
                "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
                "block_kind=NULL, block_recurrences=0, running=0, blocked=0, "
                "workflow_template_id='product', current_step_key=? "
                "WHERE id=? AND current_run_id=?",
                (resume_status, original_assignee, resume_step, task_id, int(run_id)),
            )
            outcome = "preflight_resolved"
            next_status = resume_status
        if cur.rowcount != 1:
            raise TaskSnapshotConflict("resolving preflight", current)

        ended_run_id = _end_run(
            conn,
            task_id,
            outcome=outcome,
            status="blocked" if decision == "escalate" else "completed",
            summary=reason.strip(),
            metadata=run_metadata,
            expected_run_id=int(run_id),
        )
        if ended_run_id is None:
            raise TaskSnapshotConflict("resolving preflight", current)
        resolved_payload = {
            "action": decision,
            "fault_domain": fault_domain,
            "diagnosis": diagnosis.strip(),
            "resolution": reason.strip(),
            "step_key": resume_step,
            "status": next_status,
            "resolver_profile": resolver_profile,
            "resolver_model": resolver_model,
            # D4 provenance anchor: the exact preflight this Resolver run
            # resolved, not a later same-task preflight inferred by position.
            "preflight_event_id": preflight_event_id,
        }
        if decision == "resume":
            resolved_payload["assignee"] = original_assignee
        if decision == "repair":
            assert repair_before is not None
            assert repair_after is not None
            assert normalized_repair is not None
            _append_event(
                conn,
                task_id,
                "resolver_repair_applied",
                {
                    "schema_version": 1,
                    "fault_domain": "task_state",
                    "diagnosis": diagnosis.strip(),
                    "reason": reason.strip(),
                    "resolver": {
                        "profile": resolver_profile,
                        "model": resolver_model,
                        "run_id": int(run_id),
                    },
                    "before": repair_before,
                    "after": repair_after,
                    "repair": normalized_repair,
                },
                run_id=ended_run_id,
            )
            _append_event(
                conn,
                task_id,
                "needs_ole",
                {
                    "reason": "resolver_repair",
                    "diagnosis": diagnosis.strip(),
                    "resolver_profile": resolver_profile,
                },
                run_id=ended_run_id,
            )
        _append_event(
            conn,
            task_id,
            "human_input_preflight_resolved",
            resolved_payload,
            run_id=ended_run_id,
        )
        if decision == "escalate":
            _append_event(
                conn,
                task_id,
                "blocked",
                {
                    "reason": str(preflight.get("reason") or "unspecified"),
                    "kind": "resolver_escalation",
                    "attempted_resolutions": preflight.get("attempted_resolutions") or [],
                    "resolution": reason.strip(),
                },
                run_id=ended_run_id,
            )
    return True


def _route_product_human_block_to_preflight(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
    reason: Optional[str] = None,
    kind: Optional[str] = None,
    attempted_resolutions: Optional[Iterable[str]] = None,
    metadata: Optional[dict] = None,
    expected_run_id: Optional[int] = None,
    human_escalation_assignee: Optional[str] = None,
) -> Optional[bool]:
    """Route the first product-board human block to Hermes instead of Slack.

    The next human block for the same unresolved preflight falls through to the
    normal ``blocked`` path, which is where Slack/human notification happens.
    """
    if kind not in PRODUCT_HUMAN_BLOCK_KINDS:
        return None
    meta = product_board_metadata(board)
    if meta is None:
        return None
    hermes_assignee = str(human_escalation_assignee or "").strip()
    if not hermes_assignee:
        workflow = _product_workflow_dict(meta)
        hermes_assignee = (
            str(workflow.get("human_escalation_profile") or "").strip()
            or "default"
        )
    attempts = [str(a).strip() for a in (attempted_resolutions or []) if str(a).strip()]
    with authorized_governance_write(), write_txn(conn):
        if _latest_unresolved_product_preflight(conn, task_id):
            return None
        row = conn.execute(
            "SELECT status, assignee, workflow_template_id, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        step_key = row["current_step_key"] or "backlog"
        if row["workflow_template_id"] != "product" and not step_key:
            return None
        resume_status = _column_status_for_step(meta, step_key)
        if resume_status in {"running", "blocked", "done", "archived"}:
            resume_status = "ready"
        _validate_resolver_cas_fields(
            {
                "status": resume_status,
                "assignee": hermes_assignee,
                "workflow_template_id": PRODUCT_WORKFLOW_TEMPLATE_ID,
                "current_step_key": step_key,
            }
        )
        sql = """
            UPDATE tasks
               SET status        = ?,
                   assignee      = ?,
                   claim_lock    = NULL,
                   claim_expires = NULL,
                   worker_pid    = NULL,
                   block_kind    = ?,
                   workflow_template_id = 'product',
                   current_step_key = ?
             WHERE id = ?
               AND status IN ('running', 'ready', 'review')
        """ + ("" if expected_run_id is None else " AND current_run_id = ?")
        params: tuple[Any, ...] = (
            resume_status, hermes_assignee, kind, step_key, task_id,
        ) if expected_run_id is None else (
            resume_status, hermes_assignee, kind, step_key, task_id, int(expected_run_id),
        )
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        # v2 flag maintenance: the worker that hit the obstacle has stopped,
        # but ``default`` hasn't given up yet -- clear ``running`` only.
        # ``blocked`` stays 0. Direct UPDATE (no _sync_legacy_status): the
        # explicit ``resume_status`` above already accounts for cases the
        # sync seam does not (e.g. a column status of "running").
        if _handoff_v2_enabled(meta):
            conn.execute(
                "UPDATE tasks SET running = 0 WHERE id = ?", (task_id,),
            )
        run_metadata = dict(metadata or {})
        if attempts:
            run_metadata["attempted_resolutions"] = attempts
        run_id = _end_run(
            conn, task_id,
            outcome="preflight",
            status="blocked",
            summary=reason,
            metadata=run_metadata or None,
        )
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn,
                task_id,
                outcome="preflight",
                summary=reason,
                metadata=run_metadata or None,
                step_key=step_key,
            )
        _append_event(
            conn,
            task_id,
            PRODUCT_WORKFLOW_PRECHECK_EVENT,
            {
                "reason": reason,
                "kind": kind,
                "attempted_resolutions": attempts,
                "original_assignee": row["assignee"],
                "hermes_assignee": hermes_assignee,
                "step_key": step_key,
                "resume_status": resume_status,
            },
            run_id=run_id,
        )
    return True


# D4 operator re-entry is deliberately narrower than ``unblock_task`` or
# ``resolve_product_preflight``: it accepts only a product card whose current
# lifecycle is the exact terminal Resolver-escalation shape.  The answer is
# recorded as the next preflight event so the ordinary Resolver dispatcher can
# claim it; this seam never creates a run itself.
_RESOLVER_REENTRY_ANSWER_MAX_CHARS = 2048
_RESOLVER_REENTRY_ASSIGNEE_MAX_CHARS = 256
_RESOLVER_REENTRY_ANSWERED_BY_MAX_CHARS = 256
_RESOLVER_REENTRY_ATTEMPTED_RESOLUTIONS_MAX_ITEMS = 20
_RESOLVER_REENTRY_ATTEMPTED_RESOLUTION_ITEM_MAX_CHARS = 512
_RESOLVER_REENTRY_ATTEMPTED_RESOLUTIONS_TOTAL_MAX_CHARS = 4096
_RESOLVER_REENTRY_COPIED_TEXT_MAX_CHARS = 2048
_RESOLVER_ESCALATION_RUN_STATUS = "blocked"
_RESOLVER_ESCALATION_RUN_OUTCOME = "preflight_escalated"
_RESOLVER_ESCALATION_POST_ASSIGNEE = "default"
_RESOLVER_ESCALATION_BENIGN_EVENT_KINDS = frozenset({
    # Only comment/audit-only records may be appended without invalidating
    # ownership, provenance, dispatch, or review lifecycle CAS.
    "audit",
    "audit_only",
    "comment",
    "commented",
})
_RESOLVER_ESCALATION_EXPECTED_KEYS = frozenset(
    set(_RESOLVER_EXPECTED_KEYS) | {"escalation_event_id"}
)
_RESOLVER_RESUME_RETRY_EVENT_KIND = "spawn_failed"
_RESOLVER_RESUME_RETRY_RUN_STATUS = "spawn_failed"
_RESOLVER_RESUME_RETRY_RUN_OUTCOME = "spawn_failed"


@dataclass(frozen=True)
class _D4ResolvedPreflightProvenance:
    """The anchored Resolver resolution facts shared by D4 dispatch paths."""

    resolved: sqlite3.Row
    resolved_payload: dict[str, Any]
    preflight: sqlite3.Row
    preflight_payload: dict[str, Any]
    resolver_run: sqlite3.Row
    preflight_event_id: int


def _d4_event_payload(row: sqlite3.Row) -> Optional[dict[str, Any]]:
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _d4_latest_non_benign_event(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    before_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """Return only the newest lifecycle event, ignoring audit/comment noise."""
    benign = tuple(sorted(_RESOLVER_ESCALATION_BENIGN_EVENT_KINDS))
    placeholders = ", ".join("?" for _ in benign)
    query = (
        "SELECT id, kind, payload, run_id FROM task_events "
        f"WHERE task_id = ? AND kind NOT IN ({placeholders})"
    )
    params: list[Any] = [task_id, *benign]
    if before_id is not None:
        query += " AND id < ?"
        params.append(int(before_id))
    query += " ORDER BY id DESC LIMIT 1"
    return conn.execute(query, params).fetchone()


def _d4_resolved_preflight_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    resolved: Optional[sqlite3.Row],
    *,
    expected_run_id: Optional[int] = None,
) -> Optional[_D4ResolvedPreflightProvenance]:
    """Resolve one event's exact preflight anchor and Resolver run.

    The returned chain is task-local and position-checked: the resolved event
    must point at the newest preflight before it, and both the source event and
    Resolver run must belong to ``task_id``. Callers add action-specific CAS
    checks (resume versus escalation) on top of these shared facts.
    """
    if resolved is None or resolved["kind"] != "human_input_preflight_resolved":
        return None
    resolved_payload = _d4_event_payload(resolved)
    if not isinstance(resolved_payload, dict):
        return None
    resolver_run_id = _d4_positive_event_id(resolved["run_id"])
    if resolver_run_id is None:
        return None
    if expected_run_id is not None and resolver_run_id != expected_run_id:
        return None
    try:
        resolved_event_id = int(resolved["id"])
    except (TypeError, ValueError):
        return None
    preflight_event_id = _d4_positive_event_id(
        resolved_payload.get("preflight_event_id")
    )
    if preflight_event_id is None or preflight_event_id >= resolved_event_id:
        return None
    latest_preflight = conn.execute(
        "SELECT id FROM task_events "
        "WHERE task_id = ? AND kind = ? AND id < ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, PRODUCT_WORKFLOW_PRECHECK_EVENT, resolved_event_id),
    ).fetchone()
    if latest_preflight is None or int(latest_preflight["id"]) != preflight_event_id:
        return None
    source = conn.execute(
        "SELECT id, task_id, kind, payload, run_id FROM task_events "
        "WHERE id = ? AND task_id = ?",
        (preflight_event_id, task_id),
    ).fetchone()
    source_payload = _d4_event_payload(source) if source is not None else None
    if (
        source is None
        or int(source["id"]) >= resolved_event_id
        or source["kind"] != PRODUCT_WORKFLOW_PRECHECK_EVENT
        or not isinstance(source_payload, dict)
        or (
            "task_id" in source_payload
            and source_payload["task_id"] != task_id
        )
    ):
        return None
    resolver_run = conn.execute(
        "SELECT id, task_id, status, profile, step_key, outcome, ended_at, "
        "claim_lock, claim_expires, worker_pid "
        "FROM task_runs WHERE id = ?",
        (resolver_run_id,),
    ).fetchone()
    if resolver_run is None or resolver_run["task_id"] != task_id:
        return None
    return _D4ResolvedPreflightProvenance(
        resolved=resolved,
        resolved_payload=resolved_payload,
        preflight=source,
        preflight_payload=source_payload,
        resolver_run=resolver_run,
        preflight_event_id=preflight_event_id,
    )


def _d4_positive_event_id(value: Any) -> Optional[int]:
    if type(value) is not int or value <= 0:  # bool is intentionally rejected.
        return None
    return value


def _latest_resolver_escalation(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[tuple[int, dict[str, Any], int]]:
    """Return the current canonical Resolver escalation, ignoring audit noise.

    Event order is the lifecycle CAS.  A later event is tolerated only when
    it is explicitly audit/comment-only; an unknown event fails closed.  Thus
    a later assignment, status/step mutation, answer/preflight, or ordinary
    block makes the prior escalation stale even if a caller manually leaves
    the task row looking blocked.
    """
    row = _d4_latest_non_benign_event(conn, task_id)
    if row is None:
        return None
    payload = _d4_event_payload(row)
    if row["kind"] != "blocked" or not isinstance(payload, dict):
        return None
    if payload.get("kind") != "resolver_escalation":
        return None
    run_id = row["run_id"]
    if type(run_id) is not int or run_id <= 0:
        return None
    return int(row["id"]), payload, run_id


def _resolver_escalation_snapshot(
    row: sqlite3.Row,
    *,
    escalation_event_id: int,
    preflight_event_id: Optional[int],
    resolver_run_id: int,
) -> dict[str, Any]:
    snapshot = {
        "escalation_event_id": escalation_event_id,
        "preflight_event_id": preflight_event_id,
        "run_id": resolver_run_id,
        "status": row["status"],
        "phase": row["current_step_key"],
        "assignee": row["assignee"],
        "project_id": row["project_id"],
        "workflow_template_id": row["workflow_template_id"],
        "workspace_kind": row["workspace_kind"],
        "workspace_path": row["workspace_path"],
        "branch_name": row["branch_name"],
        "running": bool(row["running"]),
        "blocked": bool(row["blocked"]),
    }
    _validate_resolver_cas_fields(snapshot)
    return snapshot


def _d4_prior_task_event(
    conn: sqlite3.Connection,
    task_id: str,
    event_id: int,
) -> Optional[sqlite3.Row]:
    """Find the immediately preceding event in this task's own stream."""
    return conn.execute(
        "SELECT id, kind, payload, run_id FROM task_events "
        "WHERE task_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (task_id, event_id),
    ).fetchone()


def resolver_escalation_expected_snapshot(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[dict[str, Any]]:
    """Return the D4 CAS snapshot for the task's current escalation."""
    escalation = _latest_resolver_escalation(conn, task_id)
    if escalation is None:
        return None
    event_id, _payload, run_id = escalation
    resolved = _d4_prior_task_event(conn, task_id, event_id)
    if resolved is None or resolved["kind"] != "human_input_preflight_resolved":
        return None
    resolved_payload = _d4_event_payload(resolved)
    if not isinstance(resolved_payload, dict):
        return None
    preflight_event_id = _d4_positive_event_id(
        resolved_payload.get("preflight_event_id")
    )
    if preflight_event_id is None or resolved["run_id"] != run_id:
        return None
    row = conn.execute(
        "SELECT status, assignee, project_id, workflow_template_id, "
        "current_step_key, workspace_kind, workspace_path, branch_name, "
        "running, blocked FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return _resolver_escalation_snapshot(
        row,
        escalation_event_id=event_id,
        preflight_event_id=preflight_event_id,
        resolver_run_id=run_id,
    )


_D4_MISSING = object()


def _d4_bounded_copied_text(
    value: Any,
    limit: int,
    *,
    field: str,
) -> str:
    if value is _D4_MISSING:
        return ""
    if not isinstance(value, str):
        raise TaskSnapshotConflict("resolver re-entry", {field: "non-string"})
    return value[:limit]


def _d4_bounded_attempted_resolutions(
    value: Any,
    *,
    field: str = "attempted_resolutions",
) -> list[str]:
    if value is _D4_MISSING:
        return []
    if not isinstance(value, list):
        raise TaskSnapshotConflict("resolver re-entry", {field: "non-list"})
    for item in value:
        if not isinstance(item, str):
            raise TaskSnapshotConflict("resolver re-entry", {field: "non-string-item"})
    items = [
        item[:_RESOLVER_REENTRY_ATTEMPTED_RESOLUTION_ITEM_MAX_CHARS]
        for item in value[-_RESOLVER_REENTRY_ATTEMPTED_RESOLUTIONS_MAX_ITEMS:]
    ]
    bounded: list[str] = []
    total = 0
    for item in reversed(items):
        if total + len(item) > _RESOLVER_REENTRY_ATTEMPTED_RESOLUTIONS_TOTAL_MAX_CHARS:
            break
        bounded.append(item)
        total += len(item)
    bounded.reverse()
    return bounded


def _d4_canonical_assignee(raw: Any, *, field: str) -> str:
    """Validate raw assignee identity before applying profile normalization."""
    if not isinstance(raw, str):
        raise TaskSnapshotConflict(
            "resolver re-entry", {field: "non-string"}
        )
    value = raw.strip()
    if not value or len(value) > _RESOLVER_REENTRY_ASSIGNEE_MAX_CHARS:
        raise TaskSnapshotConflict(
            "resolver re-entry", {field: "invalid"}
        )
    try:
        canonical = _canonical_assignee(value)
    except Exception as exc:
        raise TaskSnapshotConflict(
            "resolver re-entry", {field: "invalid"}
        ) from exc
    if not isinstance(canonical, str):
        raise TaskSnapshotConflict(
            "resolver re-entry", {field: "non-string-canonical"}
        )
    canonical = canonical.strip()
    if not canonical or len(canonical) > _RESOLVER_REENTRY_ASSIGNEE_MAX_CHARS:
        raise TaskSnapshotConflict(
            "resolver re-entry", {field: "invalid-canonical"}
        )
    try:
        _validate_resolver_cas_fields({"assignee": canonical})
    except ValueError as exc:
        raise TaskSnapshotConflict(
            "resolver re-entry", {field: "invalid-canonical"}
        ) from exc
    return canonical


def _d4_raw_governed_assignee(
    meta: Optional[dict], step_key: str,
) -> tuple[bool, Any]:
    """Read the configured phase assignee without coercing its raw value."""
    if not isinstance(meta, dict):
        return False, None
    qualification = meta.get("qualification")
    if isinstance(qualification, dict) and qualification.get("required") is True:
        phase_assignees = qualification.get("phase_assignees")
        if not isinstance(phase_assignees, dict) or step_key not in phase_assignees:
            return False, None
        return True, phase_assignees[step_key]
    phase_roles = PRODUCT_QUALIFICATION_DEFAULTS.get("phase_assignees", {})
    if not isinstance(phase_roles, dict) or step_key not in phase_roles:
        return False, None
    role = phase_roles[step_key]
    if role is None:
        return True, None
    workflow_assignees = _product_workflow_dict(meta).get("assignees")
    if not isinstance(workflow_assignees, dict) or role not in workflow_assignees:
        return False, None
    return True, workflow_assignees[role]


def _d4_original_assignee(
    meta: Optional[dict],
    task_row: sqlite3.Row,
    source_payload: dict[str, Any],
    step_key: str,
) -> Optional[str]:
    # Presence is significant: falsey malformed values are not "missing".
    if "original_assignee" in source_payload:
        raw_original = source_payload["original_assignee"]
        if raw_original is not None:
            return _d4_canonical_assignee(raw_original, field="original_assignee")

    # Use the existing governance helper for ownership/derivation, then
    # inspect the raw configured value separately so a non-string/oversized
    # configuration cannot be silently stringified into authority.
    owns_assignee, derived = _product_unblock_assignee(meta, task_row)
    raw_configured_present, raw_configured = _d4_raw_governed_assignee(meta, step_key)
    if owns_assignee:
        if step_key == "release_measure" and raw_configured is None:
            if derived is not None:
                _d4_canonical_assignee(derived, field="derived_assignee")
            return None
        if not raw_configured_present or raw_configured is None:
            raise TaskSnapshotConflict(
                "resolver re-entry", {"derived_assignee": "missing"}
            )
        configured = _d4_canonical_assignee(
            raw_configured, field="configured_assignee"
        )
        if not isinstance(derived, str):
            raise TaskSnapshotConflict(
                "resolver re-entry", {"derived_assignee": "non-string"}
            )
        derived_canonical = _d4_canonical_assignee(
            derived, field="derived_assignee"
        )
        if configured != derived_canonical:
            raise TaskSnapshotConflict(
                "resolver re-entry",
                {"configured_assignee": configured, "derived_assignee": derived_canonical},
            )
        return configured

    # A configured value that is present but falsey must not be mistaken for
    # an absent mapping after the existing helper stringifies it.
    if raw_configured_present and raw_configured is not None:
        _d4_canonical_assignee(raw_configured, field="configured_assignee")
        raise TaskSnapshotConflict(
            "resolver re-entry", {"configured_assignee": "not-governed"}
        )

    phase_roles = PRODUCT_QUALIFICATION_DEFAULTS.get("phase_assignees", {})
    fallback = phase_roles.get(step_key) if isinstance(phase_roles, dict) else None
    if fallback is None:
        raise TaskSnapshotConflict(
            "resolver re-entry", {"original_assignee": "undetermined"}
        )
    return _d4_canonical_assignee(fallback, field="derived_assignee")


def reenter_resolver_escalation(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
    answer: str,
    answered_by: str,
    expected: Optional[dict[str, Any]] = None,
    expected_snapshot: Optional[dict[str, Any]] = None,
    expected_escalation_event_id: Optional[int] = None,
) -> int:
    """Atomically answer one live Resolver escalation and re-enter dispatch.

    The caller must provide the complete D4 snapshot.  The transaction
    validates event provenance and the row CAS, writes one fresh
    ``human_input_preflight`` event, and only then exposes the task to the
    ordinary ready/review dispatcher.  It never creates or claims a run.
    """
    if expected is not None and expected_snapshot is not None and expected != expected_snapshot:
        raise ValueError("expected and expected_snapshot disagree")
    expected = expected if expected is not None else expected_snapshot
    if not isinstance(expected, dict) or set(expected) != _RESOLVER_ESCALATION_EXPECTED_KEYS:
        raise ValueError("expected must contain the complete Resolver escalation snapshot")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer is required")
    answer = answer.strip()
    if len(answer) > _RESOLVER_REENTRY_ANSWER_MAX_CHARS:
        raise ValueError(
            f"answer exceeds {_RESOLVER_REENTRY_ANSWER_MAX_CHARS} characters"
        )
    if not isinstance(answered_by, str) or not answered_by.strip():
        raise ValueError("answered_by is required")
    answered_by = answered_by.strip()
    if len(answered_by) > _RESOLVER_REENTRY_ANSWERED_BY_MAX_CHARS:
        raise ValueError(
            f"answered_by exceeds {_RESOLVER_REENTRY_ANSWERED_BY_MAX_CHARS} characters"
        )

    board = board or _board_slug_for_connection(conn)
    meta = product_board_metadata(board)
    if meta is None:
        raise ValueError("resolver re-entry requires a product board")

    try:
        with authorized_governance_write(), write_txn(conn):
            row = conn.execute(
                "SELECT status, assignee, project_id, workflow_template_id, "
                "current_step_key, workspace_kind, workspace_path, branch_name, "
                "running, blocked, current_run_id, block_kind "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown task {task_id}")

            escalation = _latest_resolver_escalation(conn, task_id)
            if escalation is None:
                raise TaskSnapshotConflict("resolver re-entry", {})
            escalation_event_id, escalation_payload, escalation_run_id = escalation
            if (
                expected_escalation_event_id is not None
                and expected_escalation_event_id != escalation_event_id
            ):
                raise TaskSnapshotConflict(
                    "resolver re-entry", {"escalation_event_id": escalation_event_id}
                )

            resolved = _d4_prior_task_event(conn, task_id, escalation_event_id)
            provenance = _d4_resolved_preflight_provenance(
                conn, task_id, resolved, expected_run_id=escalation_run_id
            )
            if provenance is None:
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "resolved"})
            resolved_payload = provenance.resolved_payload
            preflight_event_id = provenance.preflight_event_id
            current = _resolver_escalation_snapshot(
                row,
                escalation_event_id=escalation_event_id,
                preflight_event_id=preflight_event_id,
                resolver_run_id=escalation_run_id,
            )
            if current != expected:
                raise TaskSnapshotConflict("resolver re-entry", current)
            if (
                row["workflow_template_id"] != PRODUCT_WORKFLOW_TEMPLATE_ID
                or row["status"] != "blocked"
                or not bool(row["blocked"])
                or bool(row["running"])
                or row["current_run_id"] is not None
                or row["assignee"] != _RESOLVER_ESCALATION_POST_ASSIGNEE
            ):
                raise TaskSnapshotConflict("resolver re-entry", current)

            if resolved_payload.get("action") != "escalate":
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "resolved"})

            source = provenance.preflight
            source_payload = provenance.preflight_payload

            run = provenance.resolver_run
            if (
                run["status"] != _RESOLVER_ESCALATION_RUN_STATUS
                or run["outcome"] != _RESOLVER_ESCALATION_RUN_OUTCOME
                or run["ended_at"] is None
                or not isinstance(run["profile"], str)
                or not run["profile"].strip()
            ):
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "run"})
            resolver_profile = _d4_canonical_assignee(
                run["profile"], field="resolver_profile"
            )
            governed_resolver_profile = "resolver"
            if resolver_profile != governed_resolver_profile:
                raise TaskSnapshotConflict(
                    "resolver re-entry",
                    {
                        "resolver_profile": resolver_profile,
                        "governed_resolver_profile": governed_resolver_profile,
                    },
                )
            try:
                resolver_step = run["step_key"]
                current_step = row["current_step_key"]
                if not isinstance(resolver_step, str) or not resolver_step.strip():
                    raise ValueError
                if not isinstance(current_step, str) or not current_step.strip():
                    raise ValueError
                resolver_step = resolver_step.strip()
                current_step = current_step.strip()
            except (TypeError, ValueError) as exc:
                raise TaskSnapshotConflict(
                    "resolver re-entry", {"provenance": "step"}
                ) from exc
            if resolver_step != current_step:
                raise TaskSnapshotConflict(
                    "resolver re-entry",
                    {"recorded_step": resolver_step, "current_step": current_step},
                )
            if source_payload.get("step_key") != resolver_step:
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "source-step"})
            if source_payload.get("hermes_assignee") != resolver_profile:
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "profile"})
            source_run_id = source["run_id"]
            source_run = conn.execute(
                "SELECT id, task_id, status, outcome, step_key, ended_at FROM task_runs "
                "WHERE id = ?",
                (source_run_id,),
            ).fetchone()
            if (
                type(source_run_id) is not int
                or source_run_id <= 0
                or source_run is None
                or source_run["task_id"] != task_id
                or source_run["status"] != "blocked"
                or source_run["outcome"] != "preflight"
                or source_run["ended_at"] is None
                or source_run["step_key"] != resolver_step
            ):
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "source-run"})
            if resolved_payload.get("resolver_profile") != resolver_profile:
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "resolved-profile"})
            if resolved_payload.get("step_key") != resolver_step:
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "resolved-step"})

            claimed = conn.execute(
                "SELECT id FROM task_events "
                "WHERE task_id = ? AND kind = 'claimed' AND run_id = ? "
                "ORDER BY id ASC LIMIT 1",
                (task_id, escalation_run_id),
            ).fetchone()
            source_before_claim = conn.execute(
                "SELECT id FROM task_events "
                "WHERE task_id = ? AND kind = ? AND id < ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id, PRODUCT_WORKFLOW_PRECHECK_EVENT, claimed["id"] if claimed else -1),
            ).fetchone()
            if (
                claimed is None
                or source_before_claim is None
                or int(source_before_claim["id"]) != preflight_event_id
            ):
                raise TaskSnapshotConflict("resolver re-entry", {"provenance": "claim"})

            original_assignee = _d4_original_assignee(
                meta, row, source_payload, resolver_step
            )
            resume_status = _column_status_for_step(meta, resolver_step)
            if resume_status not in VALID_STATUSES or resume_status in {
                "running", "blocked", "done", "archived",
            }:
                resume_status = "ready"
            copied_reason = _d4_bounded_copied_text(
                escalation_payload.get("reason", _D4_MISSING),
                _RESOLVER_REENTRY_COPIED_TEXT_MAX_CHARS,
                field="reason",
            )
            copied_attempted_resolutions = _d4_bounded_attempted_resolutions(
                escalation_payload.get("attempted_resolutions", _D4_MISSING)
            )
            copied_resolution = _d4_bounded_copied_text(
                escalation_payload.get("resolution", _D4_MISSING),
                _RESOLVER_REENTRY_COPIED_TEXT_MAX_CHARS,
                field="resolution",
            )
            _validate_resolver_cas_fields(
                {
                    "status": resume_status,
                    "assignee": resolver_profile,
                    "workflow_template_id": PRODUCT_WORKFLOW_TEMPLATE_ID,
                    "current_step_key": resolver_step,
                }
            )

            updated = conn.execute(
                "UPDATE tasks SET status = ?, assignee = ?, running = 0, blocked = 0, "
                "current_run_id = NULL, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, block_kind = NULL, block_recurrences = 0 "
                "WHERE id = ? AND status = 'blocked' AND blocked = 1 AND running = 0 "
                "AND current_run_id IS NULL AND assignee = ? AND current_step_key = ?",
                (
                    resume_status, resolver_profile, task_id,
                    _RESOLVER_ESCALATION_POST_ASSIGNEE, resolver_step,
                ),
            )
            if updated.rowcount != 1:
                raise TaskSnapshotConflict("resolver re-entry", current)
            transition_event_id = _append_event(
                conn,
                task_id,
                PRODUCT_WORKFLOW_PRECHECK_EVENT,
                {
                    "reason": copied_reason,
                    "kind": "resolver_reentry",
                    "attempted_resolutions": copied_attempted_resolutions,
                    "original_assignee": original_assignee,
                    "hermes_assignee": resolver_profile,
                    "step_key": resolver_step,
                    "resume_status": resume_status,
                    "reentry_of_event_id": escalation_event_id,
                    "resolver_escalation_reason": copied_resolution,
                    "human_answer": answer,
                    "answered_by": answered_by,
                },
                run_id=escalation_run_id,
            )
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            raise TaskSnapshotConflict("resolver re-entry", {}) from exc
        raise
    return transition_event_id


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Metadata for every board: ``default`` first (always present), then
    ``boards/<slug>/`` dirs holding a ``kanban.db`` or ``board.json``, sorted."""
    entries = [read_board_metadata(DEFAULT_BOARD)]
    seen = {DEFAULT_BOARD}
    root = boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            try:
                normed = _normalize_board_slug(child.name)  # skip junk dirs, don't raise
            except ValueError:
                continue
            if not normed or normed in seen or not _dir_holds_board(child):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Archive (to ``boards/_archived/<slug>-<ts>/``) or delete a board;
    ``default`` cannot be removed. Returns ``{"slug", "action", "new_path"}``."""
    _assert_not_delegated_child_mutation()
    normed = _require_slug(slug)
    if normed == DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # If the user removed the currently-active board, revert to default.
    if get_current_board() == normed:
        clear_current_board()

    # A concurrent connect() after the rename recreates an empty DB file; drop
    # the init cache first so the schema pass re-runs on it.
    _INITIALIZED_PATHS.discard(str((d / "kanban.db").resolve()))

    if archive:
        archive_root = boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        suffix = 1
        while target.exists():  # rapid double-archive
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    import shutil
    shutil.rmtree(d)
    return {"slug": normed, "action": "deleted", "new_path": ""}


# --- Data classes ---

@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Column semantics: see SCHEMA_SQL.
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    work_contract_id: Optional[str] = None
    work_item_kind: str = "card"
    skills: Optional[list] = None            # None = defaults only; [] = explicitly none
    model_override: Optional[str] = None
    provider_override: Optional[str] = None  # provider ``model_override`` belongs to
    reasoning_effort: Optional[str] = None   # VALID_REASONING_EFFORTS | "none"; NULL = profile's
    # Breaker trip count; None -> ``kanban.failure_limit`` -> DEFAULT_FAILURE_LIMIT.
    max_retries: Optional[int] = None
    # ``/goal``-style loop: a judge re-checks each turn IN THE SAME SESSION until
    # done / budget exhausted (-> kanban_block); ``goal_max_turns`` None -> goals default.
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    session_id: Optional[str] = None         # originating HERMES_SESSION_ID; NULL from CLI/dashboard
    # VALID_BLOCK_KINDS or None (legacy); kept across unblock so a same-kind re-block reads as a loop.
    block_kind: Optional[str] = None
    block_recurrences: int = 0               # unblock-loop counter, see BLOCK_RECURRENCE_LIMIT
    completion_contract: Optional[str] = None
    rework_count: int = 0
    source_commit_required: bool = False
    source_commit_forbidden: bool = False
    running: bool = False
    blocked: bool = False

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        g = lambda col, default=None: _row_get(row, col, default)  # noqa: E731
        parsed = _json_or(g("skills"))
        skills_value = [str(s) for s in parsed if s] if isinstance(parsed, list) else None
        return cls(
            **{col: row[col] for col in _TASK_REQUIRED_COLUMNS},
            **{col: g(col) for col in _TASK_OPTIONAL_COLUMNS},
            **{col: g(col) or None for col in _TASK_EMPTY_IS_NULL_COLUMNS},
            # Pre-migration fallbacks (spawn_failures / last_spawn_error) are only
            # reachable on a DB never opened since the rename migration landed.
            consecutive_failures=g("consecutive_failures", g("spawn_failures", 0)),
            last_failure_error=g("last_failure_error", g("last_spawn_error")),
            skills=skills_value,
            goal_mode=bool(g("goal_mode")),
            block_recurrences=int(g("block_recurrences") or 0),
            rework_count=int(g("rework_count") or 0),
            source_commit_required=bool(g("source_commit_required")),
            source_commit_forbidden=bool(g("source_commit_forbidden")),
            running=bool(g("running")),
            blocked=bool(g("blocked")),
        )


# Columns every schema version has (KeyError if the SELECT omitted them).
_TASK_REQUIRED_COLUMNS = (
    "id", "title", "body", "assignee", "status", "priority", "created_by", "created_at",
    "started_at", "completed_at", "workspace_kind", "workspace_path", "claim_lock", "claim_expires",
)
# Later-added columns read as NULL when absent from the row.
_TASK_OPTIONAL_COLUMNS = (
    "branch_name", "project_id", "tenant", "result", "idempotency_key", "worker_pid",
    "max_runtime_seconds", "last_heartbeat_at", "current_run_id", "workflow_template_id",
    "current_step_key", "work_contract_id", "work_item_kind", "max_retries",
    "session_id", "completion_contract",
)
# Text columns where "" is stored/read as "not set".
_TASK_EMPTY_IS_NULL_COLUMNS = (
    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",
)


@dataclass
class Run:
    """One attempt at a task (``task_runs`` row): opened on claim, closed on
    complete/block/crash/timeout/reclaim; carries the handoff summary."""

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        return cls(
            **{
                col: row[col] for col in (
                    "task_id", "profile", "step_key", "status", "claim_lock", "claim_expires",
                    "worker_pid", "max_runtime_seconds", "last_heartbeat_at", "outcome", "summary", "error",
                )
            },
            id=int(row["id"]),
            started_at=int(row["started_at"]),
            ended_at=_opt_int(row["ended_at"]),
            metadata=_json_or(row["metadata"]),
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Comment":
        return cls(
            id=r["id"], task_id=r["task_id"], author=r["author"],
            body=r["body"], created_at=r["created_at"],
        )


@dataclass
class Attachment:
    """In-memory view of a row from the ``task_attachments`` table."""

    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Attachment":
        return cls(
            id=r["id"], task_id=r["task_id"], filename=r["filename"],
            stored_path=r["stored_path"], content_type=r["content_type"],
            size=r["size"] or 0, uploaded_by=r["uploaded_by"], created_at=r["created_at"],
        )


@dataclass(frozen=True)
class ReworkDirective:
    """Durable, first-class instructions for the next product worker."""

    id: int
    task_id: str
    origin_kind: str
    origin_run_id: Optional[int]
    origin_intent_key: Optional[str]
    origin_phase: str
    target_phase: str
    rejected_branch: Optional[str]
    rejected_sha: Optional[str]
    epic_tip_sha: Optional[str]
    findings: tuple[str, ...]
    status: str
    created_at: int
    resolved_by_run_id: Optional[int]

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Attachment":
        return cls(
            id=r["id"], task_id=r["task_id"], filename=r["filename"],
            stored_path=r["stored_path"], content_type=r["content_type"],
            size=r["size"] or 0, uploaded_by=r["uploaded_by"], created_at=r["created_at"],
        )


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        run_id = _row_get(row, "run_id")
        return cls(
            id=row["id"], task_id=row["task_id"], kind=row["kind"],
            payload=_json_or(row["payload"]), created_at=row["created_at"], run_id=_opt_int(run_id),
        )


# --- Schema ---

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    -- Optional link to a first-class Project (hermes_cli/projects_db). When set,
    -- the task's worktree is anchored under the project's primary repo with a
    -- deterministic branch name instead of a random wt/<task-id> fallback.
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Signed qualification provenance. NULL keeps legacy and generic-board
    -- rows valid; strict product-board materialization fills this in.
    work_contract_id     TEXT,
    work_item_kind       TEXT NOT NULL DEFAULT 'card',
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Passed to the worker via `--skills`. NULL or empty array = no extras.
    skills               TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Provider the model override belongs to. When set (alongside
    -- model_override), the dispatcher passes --provider <name> so the
    -- worker resolves the model against the right backend instead of the
    -- profile's configured provider. NULL = profile provider.
    provider_override    TEXT,
    -- Per-task reasoning effort for the worker (minimal|low|medium|high|
    -- xhigh|max|ultra, or 'none' for thinking off). When set, the dispatcher
    -- passes --reasoning <level> so the worker runs at that depth regardless
    -- of the profile's agent.reasoning_effort. NULL = profile setting.
    reasoning_effort     TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an
    -- auxiliary judge re-evaluates the worker's response against the
    -- card title/body after each turn and feeds a continuation prompt
    -- back into the SAME session until the judge agrees the work is done
    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot
    -- worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var. Indexed so per-session list queries stay cheap on
    -- larger boards.
    session_id           TEXT,
    -- Typed block reason set by ``block_task`` (one of VALID_BLOCK_KINDS, or
    -- NULL for legacy/un-typed blocks). Drives routing: ``dependency`` never
    -- sits in ``blocked`` (goes to ``todo`` for parent-gating); the others go
    -- to ``blocked`` for a human. Preserved across unblock so a re-block for
    -- the SAME kind can be recognised as a loop.
    block_kind           TEXT,
    -- Unblock-loop counter. Incremented each time a task is re-blocked for the
    -- same truly-blocked reason after having been unblocked. When it reaches
    -- BLOCK_RECURRENCE_LIMIT the task is routed to ``triage`` instead of
    -- ``blocked`` so a cron can't spin it forever. Reset to 0 only on a
    -- successful completion — NOT on unblock (resetting on unblock is exactly
    -- the amnesia that let the loop run unbounded).
    block_recurrences    INTEGER NOT NULL DEFAULT 0,
    rework_count         INTEGER NOT NULL DEFAULT 0,
    -- Orthogonal handoff_v2 state-model flags. ``phase`` (the card's single
    -- position, canonically ``current_step_key`` above) plus these two
    -- independent booleans replace the old single-``status`` enum. Neither
    -- writers nor gating consult these yet (added in T1.2/T1.3/T1.4) — this
    -- migration only makes the columns exist.
    running               INTEGER NOT NULL DEFAULT 0,
    blocked               INTEGER NOT NULL DEFAULT 0,
    source_commit_required INTEGER NOT NULL DEFAULT 0,
    source_commit_forbidden INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS qualification_intake (
    id               TEXT PRIMARY KEY,
    raw_request      TEXT NOT NULL,
    source           TEXT NOT NULL,
    session_id       TEXT,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    idempotency_digest TEXT,
    status           TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN (
                             'pending', 'running', 'needs_clarification',
                             'attention_required', 'qualified', 'rejected', 'overridden'
                         )),
    current_run_id   INTEGER,
    claim_lock       TEXT,
    claim_expires    INTEGER,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS qualification_intake_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id        TEXT NOT NULL,
    profile          TEXT NOT NULL,
    provider         TEXT,
    model            TEXT,
    effort           TEXT,
    surface          TEXT NOT NULL DEFAULT 'work_inbox_intake',
    status           TEXT NOT NULL,
    claim_lock       TEXT,
    claim_expires    INTEGER,
    worker_pid       INTEGER,
    last_heartbeat_at INTEGER,
    validation_attempts INTEGER NOT NULL DEFAULT 0,
    started_at       INTEGER NOT NULL,
    ended_at         INTEGER,
    outcome          TEXT,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS qualification_intake_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id        TEXT NOT NULL,
    run_id           INTEGER,
    kind             TEXT NOT NULL,
    payload_json     TEXT,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS work_contracts (
    id               TEXT PRIMARY KEY,
    request_id       TEXT NOT NULL,
    canonical_json   TEXT NOT NULL,
    digest           TEXT NOT NULL UNIQUE,
    signature        TEXT NOT NULL,
    issuer_profile   TEXT NOT NULL,
    issuer_run_id    INTEGER,
    policy_version   TEXT NOT NULL,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS qualification_intake_decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id        TEXT NOT NULL,
    decision         TEXT NOT NULL
                         CHECK (decision IN ('qualified', 'rejected', 'overridden')),
    actor_profile    TEXT NOT NULL,
    reason           TEXT,
    contract_id      TEXT,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS epic_memberships (
    epic_id          TEXT NOT NULL,
    task_id          TEXT NOT NULL UNIQUE,
    created_at       INTEGER NOT NULL,
    PRIMARY KEY (epic_id, task_id)
);

CREATE TABLE IF NOT EXISTS epic_story_integrations (
    epic_id          TEXT NOT NULL,
    story_id         TEXT NOT NULL,
    source_sha       TEXT NOT NULL,
    candidate_sha    TEXT,
    integrated_at    INTEGER NOT NULL,
    PRIMARY KEY (epic_id, story_id, source_sha)
);

CREATE TABLE IF NOT EXISTS story_integration_intents (
    epic_id               TEXT NOT NULL,
    story_id              TEXT NOT NULL,
    source_sha            TEXT NOT NULL,
    source_branch         TEXT NOT NULL,
    review_run_id         INTEGER NOT NULL,
    review_base_sha       TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN (
                              'pending', 'running', 'prepared', 'rework_required',
                              'attention_required', 'integrated', 'superseded'
                          )),
    claim_lock            TEXT,
    claim_expires         INTEGER,
    attempt_count         INTEGER NOT NULL DEFAULT 0,
    target_pre_sha        TEXT,
    candidate_sha         TEXT,
    candidate_ref         TEXT,
    verification_event_id INTEGER,
    last_failure_code     TEXT,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,
    PRIMARY KEY (epic_id, story_id, source_sha)
);

CREATE TABLE IF NOT EXISTS epic_release_snapshots (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    epic_id                         TEXT NOT NULL,
    epic_tip_sha                    TEXT NOT NULL,
    target_branch                   TEXT NOT NULL,
    target_pre_sha                  TEXT NOT NULL,
    release_candidate_sha           TEXT NOT NULL,
    candidate_ref                   TEXT NOT NULL,
    aggregate_verification_event_id INTEGER NOT NULL,
    repository_contract_digest      TEXT NOT NULL,
    status                          TEXT NOT NULL CHECK (status IN (
                                        'awaiting_push', 'ci_pending', 'ci_failed',
                                        'released', 'invalidated'
                                    )),
    pushed_sha                      TEXT,
    created_at                      INTEGER NOT NULL,
    updated_at                      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS epic_release_members (
    snapshot_id  INTEGER NOT NULL,
    epic_id      TEXT NOT NULL,
    story_id     TEXT NOT NULL,
    source_sha   TEXT NOT NULL,
    candidate_sha TEXT NOT NULL,
    integrated_at INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, story_id)
);

CREATE TABLE IF NOT EXISTS board_governance (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    qualification_required INTEGER NOT NULL DEFAULT 0
                               CHECK (qualification_required IN (0, 1))
);
INSERT OR IGNORE INTO board_governance (id, qualification_required) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

CREATE TABLE IF NOT EXISTS product_rework_directives (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    origin_kind         TEXT NOT NULL,
    origin_run_id       INTEGER,
    origin_intent_key   TEXT,
    origin_phase        TEXT NOT NULL,
    target_phase        TEXT NOT NULL,
    rejected_branch     TEXT,
    rejected_sha        TEXT,
    epic_tip_sha        TEXT,
    findings_json       TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('active', 'resolved', 'superseded')),
    created_at          INTEGER NOT NULL,
    resolved_by_run_id  INTEGER
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    user_id_alt   TEXT,
    chat_type     TEXT,
    notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify',
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- idx_tasks_assignee_status lives in _migrate_add_optional_columns: minimal
-- legacy boards lack the assignee column, and a CREATE INDEX over a missing
-- column would abort executescript before the additive migration runs.
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_product_rework_directives_task
    ON product_rework_directives(task_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_product_rework_directives_active
    ON product_rework_directives(task_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
CREATE INDEX IF NOT EXISTS idx_qualification_intake_status
    ON qualification_intake(status, created_at);
CREATE INDEX IF NOT EXISTS idx_qualification_intake_runs
    ON qualification_intake_runs(intake_id, id);
CREATE INDEX IF NOT EXISTS idx_qualification_intake_events
    ON qualification_intake_events(intake_id, id);
CREATE INDEX IF NOT EXISTS idx_work_contracts_digest ON work_contracts(digest);
CREATE INDEX IF NOT EXISTS idx_qualification_decisions_intake
    ON qualification_intake_decisions(intake_id, id);
CREATE INDEX IF NOT EXISTS idx_epic_memberships_epic ON epic_memberships(epic_id, created_at);
CREATE INDEX IF NOT EXISTS idx_story_integration_intents_claim
    ON story_integration_intents(status, claim_expires, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_epic_release_one_active
    ON epic_release_snapshots(epic_id)
    WHERE status IN ('awaiting_push', 'ci_pending', 'ci_failed');

CREATE TRIGGER IF NOT EXISTS work_contracts_no_update
BEFORE UPDATE ON work_contracts BEGIN
    SELECT RAISE(ABORT, 'work_contracts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS work_contracts_no_delete
BEFORE DELETE ON work_contracts BEGIN
    SELECT RAISE(ABORT, 'work_contracts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS qualification_decisions_no_update
BEFORE UPDATE ON qualification_intake_decisions BEGIN
    SELECT RAISE(ABORT, 'qualification decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS qualification_decisions_no_delete
BEFORE DELETE ON qualification_intake_decisions BEGIN
    SELECT RAISE(ABORT, 'qualification decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS qualification_intake_immutable_fields
BEFORE UPDATE ON qualification_intake
WHEN NEW.id IS NOT OLD.id
  OR NEW.raw_request IS NOT OLD.raw_request
  OR NEW.source IS NOT OLD.source
  OR NEW.session_id IS NOT OLD.session_id
  OR NEW.attachments_json IS NOT OLD.attachments_json
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'qualification intake provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS qualification_intake_no_delete
BEFORE DELETE ON qualification_intake BEGIN
    SELECT RAISE(ABORT, 'qualification intake provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS qualification_intake_status_requires_decision
BEFORE UPDATE OF status ON qualification_intake
WHEN NEW.status IS NOT OLD.status
 AND NEW.status IN ('qualified', 'rejected', 'overridden')
 AND COALESCE(
     (SELECT decision FROM qualification_intake_decisions
      WHERE intake_id = OLD.id ORDER BY id DESC LIMIT 1),
     ''
 ) != NEW.status
BEGIN
    SELECT RAISE(ABORT, 'qualification status requires an append-only decision');
END;
CREATE TRIGGER IF NOT EXISTS qualification_intake_no_terminal_reopen
BEFORE UPDATE OF status ON qualification_intake
WHEN OLD.status IN ('qualified', 'rejected', 'overridden')
 AND NEW.status NOT IN ('qualified', 'rejected', 'overridden')
BEGIN
    SELECT RAISE(ABORT, 'terminal qualification intake cannot be reopened');
END;
CREATE TRIGGER IF NOT EXISTS board_governance_no_direct_update
BEFORE UPDATE ON board_governance
WHEN hermes_governance_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'board governance is service-owned');
END;
CREATE TRIGGER IF NOT EXISTS board_governance_no_delete
BEFORE DELETE ON board_governance BEGIN
    SELECT RAISE(ABORT, 'board governance is service-owned');
END;
CREATE TRIGGER IF NOT EXISTS work_contracts_service_insert
BEFORE INSERT ON work_contracts
WHEN hermes_governance_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'Work Contracts are service-owned');
END;
"""


# --- ID generation ---

def _new_task_id() -> str:
    """``t_`` + 4 hex bytes (collision ~1e-3 at 100k tasks; 2 bytes would hit 50%
    by 10k). Idempotency belongs to ``idempotency_key``, not id uniqueness."""
    return "t_" + secrets.token_hex(4)


def _new_qualification_intake_id() -> str:
    return "qi_" + secrets.token_hex(8)


def create_qualification_intake(
    conn: sqlite3.Connection,
    *,
    raw_request: str,
    source: str,
    session_id: Optional[str] = None,
    attachments: Iterable[dict[str, Any]] = (),
    created_at: Optional[int] = None,
) -> str:
    """Persist an inert request without creating an executable task."""

    if not isinstance(raw_request, str) or not raw_request:
        raise ValueError("raw_request is required")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required")
    normalized_source = source.strip()
    attachments_json = json.dumps(
        list(attachments), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest_input = json.dumps(
        {
            "raw_request": raw_request,
            "source": normalized_source,
            "session_id": session_id,
            "attachments": json.loads(attachments_json),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # A session identifier supplies the retry boundary. Without one, two
    # identical human submissions may be intentional and must remain distinct.
    idempotency_digest = (
        hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        if session_id
        else None
    )
    now = int(time.time()) if created_at is None else int(created_at)
    for attempt in range(2):
        intake_id = _new_qualification_intake_id()
        try:
            with write_txn(conn):
                existing = (
                    conn.execute(
                        "SELECT id FROM qualification_intake "
                        "WHERE idempotency_digest = ?",
                        (idempotency_digest,),
                    ).fetchone()
                    if idempotency_digest is not None
                    else None
                )
                if existing is not None:
                    return str(existing["id"])
                conn.execute(
                    """
                    INSERT INTO qualification_intake (
                        id, raw_request, source, session_id, attachments_json,
                        idempotency_digest, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        intake_id,
                        raw_request,
                        normalized_source,
                        session_id,
                        attachments_json,
                        idempotency_digest,
                        now,
                        now,
                    ),
                )
                append_qualification_intake_event(
                    conn,
                    intake_id=intake_id,
                    kind="submitted",
                    payload={"source": normalized_source},
                    created_at=now,
                )
            return intake_id
        except sqlite3.IntegrityError:
            existing = (
                conn.execute(
                    "SELECT id FROM qualification_intake WHERE idempotency_digest = ?",
                    (idempotency_digest,),
                ).fetchone()
                if idempotency_digest is not None
                else None
            )
            if existing is not None:
                return str(existing["id"])
            if attempt:
                raise
    raise RuntimeError("unreachable")


def _qualification_intake_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        attachments = json.loads(row["attachments_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        attachments = []
    return {
        "id": row["id"],
        "raw_request": row["raw_request"],
        "source": row["source"],
        "session_id": row["session_id"],
        "attachments": attachments,
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_qualification_intake(
    conn: sqlite3.Connection, intake_id: str
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM qualification_intake WHERE id = ?", (intake_id,)
    ).fetchone()
    return _qualification_intake_dict(row) if row else None


def list_qualification_intakes(
    conn: sqlite3.Connection, *, status: Optional[str] = None
) -> list[dict[str, Any]]:
    if status is None:
        rows = conn.execute(
            "SELECT * FROM qualification_intake ORDER BY created_at, id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM qualification_intake WHERE status = ? ORDER BY created_at, id",
            (status,),
        ).fetchall()
    return [_qualification_intake_dict(row) for row in rows]


def _qualification_intake_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def claim_qualification_intake(
    conn: sqlite3.Connection,
    intake_id: str,
    *,
    profile: str,
    runtime_identity: dict[str, Any],
    lease_seconds: int = 300,
    now: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Claim one pending intake and create its immutable runtime attempt."""

    if not profile or not profile.strip():
        raise ValueError("profile is required")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    started = int(time.time()) if now is None else int(now)
    claim_lock = secrets.token_urlsafe(24)
    claim_expires = started + int(lease_seconds)
    with write_txn(conn):
        current = conn.execute(
            "SELECT status FROM qualification_intake WHERE id = ?", (intake_id,)
        ).fetchone()
        if current is None:
            raise ValueError(f"unknown qualification intake: {intake_id}")
        if current["status"] != "pending":
            return None
        retry_state = qualification_retry_state(
            conn,
            intake_id,
            qualification_max_total_attempts(read_board_metadata(_board_slug_for_connection(conn))),
        )
        if not retry_state.allowed:
            conn.execute(
                "UPDATE qualification_intake SET status = 'attention_required', updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (started, intake_id),
            )
            return None
        cursor = conn.execute(
            """
            INSERT INTO qualification_intake_runs (
                intake_id, profile, provider, model, effort, surface, status,
                claim_lock, claim_expires, last_heartbeat_at, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                intake_id,
                profile.strip(),
                runtime_identity.get("provider"),
                runtime_identity.get("model"),
                runtime_identity.get("effort"),
                runtime_identity.get("surface") or "work_inbox_intake",
                claim_lock,
                claim_expires,
                started,
                started,
            ),
        )
        run_id = int(cursor.lastrowid)
        updated = conn.execute(
            """
            UPDATE qualification_intake
               SET status = 'running', current_run_id = ?, claim_lock = ?,
                   claim_expires = ?, updated_at = ?
             WHERE id = ? AND status = 'pending' AND current_run_id IS NULL
            """,
            (run_id, claim_lock, claim_expires, started, intake_id),
        )
        if updated.rowcount != 1:
            conn.execute(
                "UPDATE qualification_intake_runs "
                "SET status = 'released', ended_at = ?, outcome = 'claim_lost' "
                "WHERE id = ?",
                (started, run_id),
            )
            return None
        append_qualification_intake_event(
            conn,
            intake_id=intake_id,
            run_id=run_id,
            kind="claimed",
            payload={
                "profile": profile.strip(),
                "provider": runtime_identity.get("provider"),
                "model": runtime_identity.get("model"),
                "effort": runtime_identity.get("effort"),
            },
            created_at=started,
        )
    return get_qualification_intake_run(conn, run_id)


def qualification_retry_state(
    conn: sqlite3.Connection, intake_id: str, max_total_attempts: int
) -> RetryState:
    limit = int(max_total_attempts)
    if limit < 1:
        raise ValueError("max_total_attempts must be positive")
    exists = conn.execute(
        "SELECT 1 FROM qualification_intake WHERE id = ?", (intake_id,)
    ).fetchone()
    if exists is None:
        raise ValueError(f"unknown qualification intake: {intake_id}")
    used = int(
        conn.execute(
            "SELECT COUNT(*) FROM qualification_intake_runs WHERE intake_id = ?",
            (intake_id,),
        ).fetchone()[0]
    )
    return RetryState(
        attempts_used=used,
        attempts_limit=limit,
        allowed=used < limit,
        reason=None if used < limit else "attempt_budget_exhausted",
    )


def get_qualification_intake_run(
    conn: sqlite3.Connection, run_id: int
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM qualification_intake_runs WHERE id = ?", (int(run_id),)
    ).fetchone()
    return _qualification_intake_run_dict(row) if row else None


def set_qualification_intake_worker_pid(
    conn: sqlite3.Connection,
    *,
    intake_id: str,
    run_id: int,
    claim_lock: str,
    worker_pid: int,
) -> bool:
    with write_txn(conn):
        updated = conn.execute(
            """
            UPDATE qualification_intake_runs
               SET worker_pid = ?
             WHERE id = ? AND intake_id = ? AND status = 'running'
               AND claim_lock = ?
            """,
            (int(worker_pid), int(run_id), intake_id, claim_lock),
        )
    return updated.rowcount == 1


def heartbeat_qualification_intake(
    conn: sqlite3.Connection,
    *,
    intake_id: str,
    run_id: int,
    claim_lock: str,
    lease_seconds: int = 300,
    now: Optional[int] = None,
) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    heartbeat = int(time.time()) if now is None else int(now)
    expires = heartbeat + int(lease_seconds)
    with write_txn(conn):
        updated = conn.execute(
            """
            UPDATE qualification_intake_runs
               SET claim_expires = ?, last_heartbeat_at = ?
             WHERE id = ? AND intake_id = ? AND status = 'running'
               AND claim_lock = ?
            """,
            (expires, heartbeat, int(run_id), intake_id, claim_lock),
        )
        if updated.rowcount != 1:
            return False
        current = conn.execute(
            """
            UPDATE qualification_intake
               SET claim_expires = ?, updated_at = ?
             WHERE id = ? AND status = 'running'
               AND current_run_id = ? AND claim_lock = ?
            """,
            (expires, heartbeat, intake_id, int(run_id), claim_lock),
        )
        if current.rowcount != 1:
            raise RuntimeError("intake claim changed during heartbeat")
        return True


_QUALIFICATION_FAILURE_OUTCOMES = {
    "spawn_failed",
    "reclaimed",
    "crashed",
    "provider_failed",
    "protocol_violation",
}


def fail_qualification_intake_run(
    conn: sqlite3.Connection,
    *,
    intake_id: str,
    run_id: int,
    claim_lock: str,
    outcome: str,
    error: Optional[str] = None,
    failure_limit: int = 2,
    now: Optional[int] = None,
) -> Optional[str]:
    """Close one failed attempt and return its retry/attention intake state."""

    if outcome not in _QUALIFICATION_FAILURE_OUTCOMES:
        raise ValueError("invalid qualification failure outcome")
    if int(failure_limit) < 1:
        raise ValueError("failure_limit must be positive")
    ended = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        current = conn.execute(
            "SELECT status, current_run_id, claim_lock "
            "FROM qualification_intake WHERE id = ?",
            (intake_id,),
        ).fetchone()
        if (
            current is None
            or current["status"] != "running"
            or current["current_run_id"] != int(run_id)
            or current["claim_lock"] != claim_lock
        ):
            return None
        updated = conn.execute(
            """
            UPDATE qualification_intake_runs
               SET status = 'completed', ended_at = ?, outcome = ?, error = ?,
                   claim_lock = NULL, claim_expires = NULL
             WHERE id = ? AND intake_id = ? AND status = 'running'
               AND claim_lock = ?
            """,
            (ended, outcome, error, int(run_id), intake_id, claim_lock),
        )
        if updated.rowcount != 1:
            return None
        placeholders = ",".join("?" for _ in _QUALIFICATION_FAILURE_OUTCOMES)
        failure_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM qualification_intake_runs "
                f"WHERE intake_id = ? AND outcome IN ({placeholders})",
                (intake_id, *sorted(_QUALIFICATION_FAILURE_OUTCOMES)),
            ).fetchone()[0]
        )
        next_status = (
            "attention_required"
            if failure_count >= int(failure_limit)
            else "pending"
        )
        conn.execute(
            """
            UPDATE qualification_intake
               SET status = ?, current_run_id = NULL, claim_lock = NULL,
                   claim_expires = NULL, updated_at = ?
             WHERE id = ? AND current_run_id = ? AND claim_lock = ?
            """,
            (next_status, ended, intake_id, int(run_id), claim_lock),
        )
        append_qualification_intake_event(
            conn,
            intake_id=intake_id,
            run_id=int(run_id),
            kind=(
                "attention_required"
                if next_status == "attention_required"
                else "claim_released"
            ),
            payload={
                "outcome": outcome,
                "error": error,
                "failure_count": failure_count,
            },
            created_at=ended,
        )
    return next_status


def _qualification_worker_pid_alive(pid: int) -> bool:
    from gateway.status import _pid_exists

    return _pid_exists(int(pid))


def recover_stale_qualification_intakes(
    conn: sqlite3.Connection,
    *,
    failure_limit: int = 2,
    lease_seconds: int = 300,
    max_runtime_seconds: int = 15 * 60,
    now: Optional[int] = None,
    pid_alive: Optional[Callable[[int], bool]] = None,
) -> dict[str, int]:
    """Reclaim expired or dead direct-PO attempts without creating cards."""

    if int(lease_seconds) < 1 or int(max_runtime_seconds) < 1:
        raise ValueError("lease and max runtime must be positive")
    timestamp = int(time.time()) if now is None else int(now)
    alive = pid_alive or _qualification_worker_pid_alive
    rows = conn.execute(
        """
        SELECT q.id AS intake_id, q.current_run_id, q.claim_lock,
               q.claim_expires, r.worker_pid, r.started_at
          FROM qualification_intake q
          JOIN qualification_intake_runs r ON r.id = q.current_run_id
         WHERE q.status = 'running' AND r.status = 'running'
        """
    ).fetchall()
    counts = {"retried": 0, "attention_required": 0}
    for row in rows:
        expired = (
            row["claim_expires"] is not None
            and int(row["claim_expires"]) < timestamp
        )
        worker_pid = row["worker_pid"]
        worker_alive = (
            worker_pid is not None and alive(int(worker_pid))
        )
        runtime_exceeded = (
            timestamp - int(row["started_at"]) > int(max_runtime_seconds)
        )
        if expired and worker_alive and not runtime_exceeded:
            extended_expires = timestamp + int(lease_seconds)
            try:
                with write_txn(conn):
                    run_update = conn.execute(
                        "UPDATE qualification_intake_runs "
                        "SET claim_expires = ? "
                        "WHERE id = ? AND intake_id = ? AND status = 'running' "
                        "AND claim_lock = ?",
                        (
                            extended_expires,
                            int(row["current_run_id"]),
                            str(row["intake_id"]),
                            str(row["claim_lock"]),
                        ),
                    )
                    if run_update.rowcount != 1:
                        raise RuntimeError(
                            "intake run claim changed during lease extension"
                        )
                    intake_update = conn.execute(
                        "UPDATE qualification_intake "
                        "SET claim_expires = ?, updated_at = ? "
                        "WHERE id = ? AND status = 'running' "
                        "AND current_run_id = ? AND claim_lock = ?",
                        (
                            extended_expires,
                            timestamp,
                            str(row["intake_id"]),
                            int(row["current_run_id"]),
                            str(row["claim_lock"]),
                        ),
                    )
                    if intake_update.rowcount != 1:
                        raise RuntimeError(
                            "intake claim changed during lease extension"
                        )
                    append_qualification_intake_event(
                        conn,
                        intake_id=str(row["intake_id"]),
                        run_id=int(row["current_run_id"]),
                        kind="claim_extended",
                        payload={
                            "claim_expires": extended_expires,
                            "worker_pid": int(worker_pid),
                        },
                        created_at=timestamp,
                    )
            except RuntimeError:
                _log.warning(
                    "qualification intake claim changed during lease extension",
                    exc_info=True,
                )
            continue
        dead = worker_pid is not None and not worker_alive
        if not expired and not dead and not runtime_exceeded:
            continue
        error = (
            "worker exceeded maximum intake runtime"
            if runtime_exceeded
            else "worker exited or claim heartbeat expired"
        )
        status = fail_qualification_intake_run(
            conn,
            intake_id=str(row["intake_id"]),
            run_id=int(row["current_run_id"]),
            claim_lock=str(row["claim_lock"]),
            outcome="reclaimed",
            error=error,
            failure_limit=1 if runtime_exceeded else failure_limit,
            now=timestamp,
        )
        if status == "pending":
            counts["retried"] += 1
        elif status == "attention_required":
            counts["attention_required"] += 1
    return counts


def append_qualification_intake_event(
    conn: sqlite3.Connection,
    *,
    intake_id: str,
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    run_id: Optional[int] = None,
    created_at: Optional[int] = None,
) -> int:
    if not kind or not kind.strip():
        raise ValueError("event kind is required")
    timestamp = int(time.time()) if created_at is None else int(created_at)
    payload_json = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if payload is not None
        else None
    )
    with write_txn(conn, allow_nested=True):
        if conn.execute(
            "SELECT 1 FROM qualification_intake WHERE id = ?", (intake_id,)
        ).fetchone() is None:
            raise ValueError(f"unknown qualification intake: {intake_id}")
        if run_id is not None and conn.execute(
            "SELECT 1 FROM qualification_intake_runs "
            "WHERE id = ? AND intake_id = ?",
            (int(run_id), intake_id),
        ).fetchone() is None:
            raise ValueError("intake run does not belong to intake")
        cursor = conn.execute(
            """
            INSERT INTO qualification_intake_events (
                intake_id, run_id, kind, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (intake_id, run_id, kind.strip(), payload_json, timestamp),
        )
    return int(cursor.lastrowid)


def list_qualification_intake_events(
    conn: sqlite3.Connection, intake_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM qualification_intake_events "
        "WHERE intake_id = ? ORDER BY id",
        (intake_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = None
        if row["payload_json"] is not None:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = None
        result.append(
            {
                "id": row["id"],
                "intake_id": row["intake_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return result


def finish_qualification_intake_run(
    conn: sqlite3.Connection,
    *,
    intake_id: str,
    run_id: int,
    claim_lock: str,
    intake_status: str,
    outcome: str,
    error: Optional[str] = None,
    now: Optional[int] = None,
) -> bool:
    if intake_status not in {
        "pending",
        "needs_clarification",
        "attention_required",
        "rejected",
        "qualified",
    }:
        raise ValueError("invalid intake completion status")
    ended = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        intake_row = conn.execute(
            "SELECT status, current_run_id, claim_lock "
            "FROM qualification_intake WHERE id = ?",
            (intake_id,),
        ).fetchone()
        if (
            intake_row is None
            or intake_row["current_run_id"] != int(run_id)
            or intake_row["claim_lock"] != claim_lock
            or intake_row["status"] not in {"running", intake_status}
        ):
            return False
        updated = conn.execute(
            """
            UPDATE qualification_intake_runs
               SET status = 'completed', ended_at = ?, outcome = ?, error = ?,
                   claim_lock = NULL, claim_expires = NULL
             WHERE id = ? AND intake_id = ? AND status = 'running'
               AND claim_lock = ?
            """,
            (ended, outcome, error, int(run_id), intake_id, claim_lock),
        )
        if updated.rowcount != 1:
            return False
        current = conn.execute(
            """
            UPDATE qualification_intake
               SET status = ?, current_run_id = NULL, claim_lock = NULL,
                   claim_expires = NULL, updated_at = ?
             WHERE id = ? AND current_run_id = ? AND claim_lock = ?
            """,
            (intake_status, ended, intake_id, int(run_id), claim_lock),
        )
        if current.rowcount != 1:
            raise RuntimeError("intake claim changed during completion")
    return True


def retry_qualification_intake(
    conn: sqlite3.Connection, intake_id: str, *, now: Optional[int] = None
) -> bool:
    timestamp = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM qualification_intake WHERE id = ?",
            (intake_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown qualification intake: {intake_id}")
        if row["status"] != "attention_required":
            raise ValueError("intake must be attention_required before retry")
        if row["current_run_id"] is not None:
            raise ValueError("cannot retry an intake with an active run")
        retry_state = qualification_retry_state(
            conn,
            intake_id,
            qualification_max_total_attempts(read_board_metadata(_board_slug_for_connection(conn))),
        )
        if not retry_state.allowed:
            raise ValueError("attempt_budget_exhausted")
        updated = conn.execute(
            "UPDATE qualification_intake SET status = 'pending', updated_at = ? "
            "WHERE id = ? AND status = 'attention_required'",
            (timestamp, intake_id),
        )
        if updated.rowcount == 1:
            append_qualification_intake_event(
                conn,
                intake_id=intake_id,
                kind="retry_scheduled",
                payload={"from_status": "attention_required"},
                created_at=timestamp,
            )
    return updated.rowcount == 1


def respond_to_qualification_clarification(
    conn: sqlite3.Connection,
    intake_id: str,
    *,
    source: str,
    response: str,
    session_id: Optional[str] = None,
    attachments: Iterable[dict[str, Any]] = (),
    now: Optional[int] = None,
) -> bool:
    """Append a same-source clarification response and reopen the inert intake."""

    exact_response = str(response or "").strip()
    if not exact_response:
        raise ValueError("clarification response is required")
    timestamp = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        row = conn.execute(
            "SELECT source, status FROM qualification_intake WHERE id = ?",
            (intake_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown qualification intake: {intake_id}")
        if row["source"] != source:
            raise PermissionError("clarification source does not match intake")
        if row["status"] != "needs_clarification":
            raise ValueError("intake is not waiting for clarification")
        append_qualification_intake_event(
            conn,
            intake_id=intake_id,
            kind="clarification_response",
            payload={
                "response": exact_response,
                "session_id": session_id,
                "attachments": list(attachments),
            },
            created_at=timestamp,
        )
        updated = conn.execute(
            "UPDATE qualification_intake SET status = 'pending', updated_at = ? "
            "WHERE id = ? AND status = 'needs_clarification'",
            (timestamp, intake_id),
        )
    return updated.rowcount == 1


def record_qualification_decision(
    conn: sqlite3.Connection,
    *,
    intake_id: str,
    decision: str,
    actor_profile: str,
    reason: Optional[str] = None,
    contract_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> int:
    """Append an audit decision and update the intake's current disposition."""

    if decision not in {"qualified", "rejected", "overridden"}:
        raise ValueError("decision must be qualified, rejected, or overridden")
    if not actor_profile or not actor_profile.strip():
        raise ValueError("actor_profile is required")
    now = int(time.time()) if created_at is None else int(created_at)
    with write_txn(conn):
        exists = conn.execute(
            "SELECT 1 FROM qualification_intake WHERE id = ?", (intake_id,)
        ).fetchone()
        if not exists:
            raise ValueError(f"unknown qualification intake: {intake_id}")
        if decision in {"qualified", "overridden"}:
            if not contract_id:
                raise ValueError(
                    f"{decision} decision requires the matching Work Contract"
                )
            contract_row = conn.execute(
                "SELECT request_id FROM work_contracts WHERE id = ?", (contract_id,)
            ).fetchone()
            if not contract_row:
                raise ValueError(f"unknown Work Contract: {contract_id}")
            if contract_row["request_id"] != intake_id:
                raise ValueError(
                    f"Work Contract {contract_id} does not belong to intake {intake_id}"
                )
        elif contract_id is not None:
            raise ValueError("rejected decisions cannot attach a Work Contract")
        cursor = conn.execute(
            """
            INSERT INTO qualification_intake_decisions (
                intake_id, decision, actor_profile, reason, contract_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                intake_id,
                decision,
                actor_profile.strip(),
                reason,
                contract_id,
                now,
            ),
        )
        conn.execute(
            "UPDATE qualification_intake SET status = ?, updated_at = ? WHERE id = ?",
            (decision, now, intake_id),
        )
        append_qualification_intake_event(
            conn,
            intake_id=intake_id,
            kind={
                "qualified": "qualified_materialized",
                "rejected": "explicitly_rejected",
                "overridden": "override_materialized",
            }[decision],
            payload={
                "actor_profile": actor_profile.strip(),
                "reason": reason,
                "contract_id": contract_id,
            },
            created_at=now,
        )
    return int(cursor.lastrowid)


def list_qualification_decisions(
    conn: sqlite3.Connection, intake_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM qualification_intake_decisions WHERE intake_id = ? ORDER BY id",
        (intake_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def store_work_contract(
    conn: sqlite3.Connection,
    signed_contract: dict[str, Any],
    *,
    secret: Optional[bytes] = None,
    hermes_home: Optional[Path] = None,
    created_at: Optional[int] = None,
) -> str:
    """Verify and append an immutable service-signed Work Contract."""

    from hermes_cli import kanban_intake

    verification = kanban_intake.verify_work_contract(
        signed_contract, secret=secret, hermes_home=hermes_home
    )
    if not verification.valid:
        raise ValueError(
            f"refusing to store an invalid Work Contract: {verification.failure}"
        )
    contract = signed_contract["contract"]
    digest = signed_contract["digest"]
    contract_id = "wc_" + digest[:24]
    issuer = contract["issuer"]
    now = int(time.time()) if created_at is None else int(created_at)
    with authorized_governance_write():
        with write_txn(conn):
            intake_row = conn.execute(
                "SELECT id FROM qualification_intake WHERE id = ?",
                (contract["request_id"],),
            ).fetchone()
            if not intake_row:
                raise ValueError(
                    f"unknown qualification intake: {contract['request_id']}"
                )
            existing = conn.execute(
                "SELECT id, canonical_json, signature FROM work_contracts WHERE digest = ?",
                (digest,),
            ).fetchone()
            if existing:
                if (
                    existing["canonical_json"] != signed_contract["canonical_json"]
                    or existing["signature"] != signed_contract["signature"]
                ):
                    raise ValueError(
                        "stored Work Contract digest conflicts with signed payload"
                    )
                return existing["id"]
            conn.execute(
                """
                INSERT INTO work_contracts (
                    id, request_id, canonical_json, digest, signature,
                    issuer_profile, issuer_run_id, policy_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    contract["request_id"],
                    signed_contract["canonical_json"],
                    digest,
                    signed_contract["signature"],
                    issuer["profile"],
                    issuer["run_id"],
                    contract["policy_version"],
                    now,
                ),
            )
    return contract_id


def get_work_contract(
    conn: sqlite3.Connection, contract_id: str
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM work_contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["contract"] = json.loads(result["canonical_json"])
    return result


def work_contract_view(
    conn: sqlite3.Connection, contract_id: Optional[str]
) -> Optional[dict[str, Any]]:
    """Return the bounded authority fields workers may consume."""
    if not contract_id:
        return None
    stored = get_work_contract(conn, contract_id)
    if stored is None:
        raise ValueError(f"missing Work Contract {contract_id}")
    contract = stored["contract"]
    return {
        "id": stored["id"],
        "digest": stored["digest"],
        "policy_version": contract.get("policy_version"),
        "qualification_path": contract.get("qualification_path"),
        "request_id": contract.get("request_id"),
        "work": contract.get("work"),
        "routing": contract.get("routing"),
        "handover": contract.get("handover"),
        "rules": contract.get("rules"),
        "classification": contract.get("classification"),
        "sizing": contract.get("sizing"),
        "requirement_feasibility": contract.get("requirement_feasibility"),
    }


def add_epic_membership(
    conn: sqlite3.Connection,
    *,
    epic_id: str,
    task_id: str,
    created_at: Optional[int] = None,
) -> None:
    now = int(time.time()) if created_at is None else int(created_at)
    with write_txn(conn):
        rows = conn.execute(
            "SELECT id, work_item_kind FROM tasks WHERE id IN (?, ?)",
            (epic_id, task_id),
        ).fetchall()
        by_id = {row["id"]: row["work_item_kind"] for row in rows}
        missing = [item for item in (epic_id, task_id) if item not in by_id]
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if by_id[epic_id] != "epic":
            raise ValueError(f"epic task {epic_id} is not work_item_kind=epic")
        if by_id[task_id] == "epic":
            raise ValueError("an Epic cannot be a child Epic membership")
        conn.execute(
            "INSERT INTO epic_memberships (epic_id, task_id, created_at) VALUES (?, ?, ?)",
            (epic_id, task_id, now),
        )


def list_epic_members(conn: sqlite3.Connection, epic_id: str) -> list[str]:
    return [
        row["task_id"]
        for row in conn.execute(
            "SELECT task_id FROM epic_memberships WHERE epic_id = ? ORDER BY created_at, task_id",
            (epic_id,),
        ).fetchall()
    ]


def epic_id_for_task(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return the card's explicit Epic membership, if any."""

    row = conn.execute(
        "SELECT epic_id FROM epic_memberships WHERE task_id = ?", (task_id,)
    ).fetchone()
    return str(row["epic_id"]) if row is not None else None


def epic_progress(conn: sqlite3.Connection, epic_id: str) -> dict[str, Any]:
    """Return member-derived progress and the Epic release state."""

    if not _is_epic_task(conn, epic_id):
        raise ValueError(f"task {epic_id} is not an Epic")
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END), 0) AS done
          FROM epic_memberships em
          JOIN tasks t ON t.id = em.task_id
         WHERE em.epic_id = ?
        """,
        (epic_id,),
    ).fetchone()
    epic = get_task(conn, epic_id)
    if epic is not None and epic.status == "done":
        release_state = "released"
    elif any(event.kind == "epic_merged" for event in list_events(conn, epic_id)):
        release_state = "merged"
    else:
        release_state = "pending"
    return {
        "done": int(row["done"]),
        "total": int(row["total"]),
        "release_state": release_state,
    }


def release_scope_for_task(conn: sqlite3.Connection, task_id: str) -> str:
    """Classify release routing without consulting dependency links."""

    if _is_epic_task(conn, task_id):
        return "epic"
    return "epic_member" if epic_id_for_task(conn, task_id) else "standalone"


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _host_prefix() -> str:
    """``"<host>:"`` prefix shared by every claim lock issued from this host."""
    return f"{_claimer_id().split(':', 1)[0]}:"


# --- Task creation / mutation ---

def _validate_model_override(model: Optional[str], provider: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Strip both; a provider without a model is rejected (a bare ``--provider``
    would re-resolve the profile's model against another backend — exactly
    the mismatch the override exists to kill)."""
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if provider and not model:
        raise ValueError("provider_override requires a model_override")
    return model, provider


def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


def _resolve_project_link(
    conn: sqlite3.Connection, project_id: Optional[str], project_source_task_id: Optional[str],
    workspace_kind: str, workspace_path: Optional[str],
) -> tuple[Optional[str], Any, Optional[str], str]:
    """``(project_id, project_obj, project_repo, workspace_kind)`` for ``create_task``.

    A project-linked task is anchored to the project's primary repo as a
    worktree with a deterministic branch (slug + task id). Projects live in the
    creator's per-profile projects.db, but the stored repo path is absolute so
    the cross-profile dispatcher needs no projects.db access. ``project_repo``
    is set when the worktree path must still be derived from the new task id.
    """
    project_id = (str(project_id).strip() or None) if project_id is not None else None
    if not project_id:
        return None, None, None, workspace_kind
    from hermes_cli import projects_db as _pdb

    project_repo: Optional[str] = None
    try:
        with _pdb.connect_closing() as _pconn:
            project_obj = _pdb.get_project(_pconn, project_id)
    except Exception:
        project_obj = None
    if project_obj is None and project_source_task_id:
        project_obj, project_repo = _project_from_source_task(
            conn, _pdb, project_id, str(project_source_task_id),
        )
        if project_obj is not None and workspace_kind == "scratch":
            workspace_kind = "worktree"
    if project_obj is None:
        # Unresolvable id/slug: drop the link (never a dangling reference,
        # never a crash) and create an ordinary scratch task.
        return None, None, None, workspace_kind
    # Canonicalise (a slug may have been passed) and anchor the worktree
    # under the project's primary repo.
    if workspace_kind == "scratch" and project_obj.primary_path:
        workspace_kind = "worktree"
    if workspace_kind == "worktree" and workspace_path is None and project_obj.primary_path:
        # Concrete path is deferred to the insert loop: a fresh
        # ``<repo>/.worktrees/<task-id>`` keyed on the new task id.
        project_repo = str(project_obj.primary_path)
    return project_obj.id, project_obj, project_repo, workspace_kind


def _project_from_source_task(
    conn: sqlite3.Connection, _pdb: Any, project_id: str, source_task_id: str,
) -> tuple[Any, Optional[str]]:
    """Recover a Project (and its repo) from a canonical project-linked
    worktree task on this board. Worker profiles have their own projects.db
    while the Kanban DB is shared, so this carries the repo + branch
    convention forward without opening the creator's store and without
    reusing the source task's literal worktree path. ``(None, None)`` when
    the source task is not a ``<repo>/.worktrees/<id>`` project worktree."""
    source_task = get_task(conn, source_task_id)
    if not (
        source_task is not None
        and source_task.project_id == project_id
        and source_task.workspace_kind == "worktree"
        and source_task.workspace_path
    ):
        return None, None
    source_path = Path(source_task.workspace_path)
    if not (
        source_path.is_absolute()
        and source_path.name == source_task.id
        and source_path.parent.name == ".worktrees"
    ):
        return None, None
    project_slug = None
    if source_task.branch_name:
        prefix, separator, leaf = source_task.branch_name.partition("/")
        if separator and (leaf == source_task.id or leaf.startswith(f"{source_task.id}-")):
            with contextlib.suppress(ValueError):
                project_slug = _pdb.normalize_slug(prefix)
    if project_slug is None:
        with contextlib.suppress(ValueError):
            project_slug = _pdb.normalize_slug(project_id)
    if not project_slug:
        return None, None
    project_repo = str(source_path.parent.parent)
    project_obj = _pdb.Project(
        id=project_id, slug=project_slug, name=project_slug, created_at=0, primary_path=project_repo,
    )
    return project_obj, project_repo


def _normalize_task_skills(skills: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Strip/dedupe a skills list. Commas are refused (a comma-joined string must
    not land in one argv slot); toolset names are rejected all at once because
    agents that confuse the two usually pass several."""
    if skills is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    toolset_typos: list[str] = []
    for s in skills:
        if not s:
            continue
        name = str(s).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(n) for n in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    parents: Iterable[str] = (),
    triage: bool = False,
    idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None,
    skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None,
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    goal_mode: bool = False,
    goal_max_turns: Optional[int] = None,
    initial_status: str = "running",
    session_id: Optional[str] = None,
    board: Optional[str] = None,
    project_id: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
    work_contract_id: Optional[str] = None,
    work_item_kind: str = "card",
    project_source_task_id: Optional[str] = None,
    completion_contract: Optional[str] = None,
    source_commit_required: bool = False,
    source_commit_forbidden: bool = False,
) -> str:
    """Create a new task and optionally link it under parent tasks.

    Returns the new task id.  Status is ``ready`` when there are no
    parents (or all parents already ``done``), otherwise ``todo``.
    If ``triage=True``, status is forced to ``triage`` regardless of
    parents — a specifier/triager is expected to promote the task to
    ``todo`` once the spec is fleshed out.

    If ``idempotency_key`` is provided and a non-archived task with the
    same key already exists, returns the existing task's id instead of
    creating a duplicate. Useful for retried webhooks / automation that
    should not double-write.

    ``max_runtime_seconds`` caps how long a worker may run before the
    dispatcher SIGTERMs (then SIGKILLs after a grace window) and
    re-queues the task. ``None`` means no cap (default).

    ``skills`` is an optional list of skill names to force-load into
    the worker when dispatched. Stored as JSON; the dispatcher passes
    each name to ``hermes --skills ...``. Use this to pin a task to a
    specialist skill (e.g. ``skills=["translation"]`` so the worker loads the
    translation skill regardless of the profile's default config).

    ``model_override`` / ``provider_override`` pin the worker to a specific
    model (and optionally its provider) without touching the profile's
    config — passed to the worker as ``-m <model> [--provider <name>]``.
    ``provider_override`` requires ``model_override``.

    ``reasoning_effort`` pins the worker's thinking depth for this task
    (``minimal``…``ultra``, or ``none`` to disable thinking), passed as
    ``--reasoning <level>``. It is independent of ``model_override``: a task
    can run the profile's own model at a different depth.

    ``project_source_task_id`` is an internal cross-profile fallback for a
    worker-created child. When the active profile cannot resolve ``project_id``
    in its own projects.db, a matching canonical project-linked task in this
    board can supply the repo and branch convention. Its literal worktree is
    never reused; the new task still gets its own task-id-keyed path.
    """
    from hermes_cli.kanban_pr_acceptance import validate_contract

    completion_contract = validate_contract(completion_contract)
    model_override = (model_override or "").strip() or None
    provider_override = (provider_override or "").strip() or None
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    if provider_override and not model_override:
        raise ValueError("provider_override requires a model_override")
    if source_commit_required and source_commit_forbidden:
        raise ValueError(
            "source_commit_required and source_commit_forbidden are mutually exclusive"
        )
    assignee = _canonical_assignee(assignee)
    if assignee == RESOLVER_PROFILE:
        # A card being created has no preflight yet, so this routing can
        # never be valid. Resolver reaches a card only by displacement from
        # an ordinary worker's product preflight, never by authorship.
        raise ValueError(
            f"a new task cannot be assigned to the privileged "
            f"'{RESOLVER_PROFILE}' profile: Resolver only resolves an "
            "unresolved product preflight on an existing card, and holds no "
            "kanban_complete/kanban_block exit of its own. Assign an ordinary "
            "worker profile instead."
        )
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(
            f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}"
        )
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if workspace_path is not None:
        workspace_path = str(workspace_path)
    if project_id is not None:
        project_id = str(project_id).strip() or None
    _validate_resolver_cas_fields(
        {
            "assignee": assignee,
            "project_id": project_id,
            "workflow_template_id": workflow_template_id,
            "current_step_key": current_step_key,
            "workspace_kind": workspace_kind,
            "workspace_path": workspace_path,
            "branch_name": branch_name,
        }
    )
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")
    if work_item_kind not in {"card", "epic"}:
        raise ValueError("work_item_kind must be card or epic")
    if work_item_kind == "epic" and (
        assignee is not None
        or workflow_template_id is not None
        or current_step_key is not None
    ):
        raise ValueError("Epic work items cannot have an assignee or workflow phase")

    # Inherit the board's scoped project when the caller didn't name one, so a
    # project-scoped board anchors every new task to that project's repo
    # (deterministic worktree + branch) without each surface repeating it.
    if project_id is None:
        try:
            _bmeta = read_board_metadata(board if board else get_current_board())
            _board_project = (_bmeta.get("project_id") or "").strip()
            if _board_project:
                project_id = _board_project
        except Exception:
            pass

    # Resolve an optional first-class Project link. A project-linked task is
    # anchored to the project's primary repo as a git worktree, so its branch
    # can be named deterministically (project slug + task id) instead of the
    # random ``wt/<task-id>`` fallback the worker skill applies when no branch
    # is set. Projects live in the creator's per-profile projects.db; the repo
    # path is absolute (profile-independent) and the branch name is pure, so the
    # cross-profile dispatcher needs no projects.db access at dispatch time.
    project_obj = None
    # Primary repo of a project-linked worktree task whose path we still need to
    # derive (a fresh worktree dir under the repo, computed once task_id exists).
    project_repo: Optional[str] = None
    if project_id is not None:
        project_id = str(project_id).strip() or None
    if project_id:
        from hermes_cli import projects_db as _pdb

        try:
            with _pdb.connect_closing() as _pconn:
                project_obj = _pdb.get_project(_pconn, project_id)
        except Exception:
            project_obj = None
        if project_obj is None and project_source_task_id:
            # Worker profiles have their own projects.db, while the Kanban DB is
            # intentionally shared. Recover routing only from a canonical
            # project-linked source task in this same board. This carries the
            # repo + project branch convention forward without copying or
            # opening the creator profile's project store, and without reusing
            # the source task's literal worktree path.
            source_task = get_task(conn, str(project_source_task_id))
            if (
                source_task is not None
                and source_task.project_id == project_id
                and source_task.workspace_kind == "worktree"
                and source_task.workspace_path
            ):
                source_path = Path(source_task.workspace_path)
                if (
                    source_path.is_absolute()
                    and source_path.name == source_task.id
                    and source_path.parent.name == ".worktrees"
                ):
                    project_slug = None
                    if source_task.branch_name:
                        prefix, separator, leaf = source_task.branch_name.partition("/")
                        if separator and (
                            leaf == source_task.id
                            or leaf.startswith(f"{source_task.id}-")
                        ):
                            try:
                                project_slug = _pdb.normalize_slug(prefix)
                            except ValueError:
                                project_slug = None
                    if project_slug is None:
                        try:
                            project_slug = _pdb.normalize_slug(project_id)
                        except ValueError:
                            project_slug = None
                    if project_slug:
                        project_repo = str(source_path.parent.parent)
                        project_obj = _pdb.Project(
                            id=project_id,
                            slug=project_slug,
                            name=project_slug,
                            created_at=0,
                            primary_path=project_repo,
                        )
                        if workspace_kind == "scratch":
                            workspace_kind = "worktree"

        if project_obj is None:
            raise ValueError(f"unknown project: {project_id}")
        else:
            # Canonicalise (a slug may have been passed) and anchor the
            # worktree under the project's primary repo.
            project_id = project_obj.id
            if workspace_kind == "scratch" and project_obj.primary_path:
                workspace_kind = "worktree"
            if (
                workspace_kind == "worktree"
                and workspace_path is None
                and project_obj.primary_path
            ):
                # Defer the concrete path to the insert loop: it's a fresh
                # ``<repo>/.worktrees/<task_id>`` dir keyed on the new task id.
                project_repo = str(project_obj.primary_path)

    if workflow_template_id is not None:
        workflow_template_id = str(workflow_template_id).strip() or None
    if current_step_key is not None:
        current_step_key = str(current_step_key).strip() or None

    workflow_defaulted = False
    if project_obj is not None and project_obj.board_slug:
        bound_board = _normalize_board_slug(project_obj.board_slug)
        effective_board = (
            _normalize_board_slug(board)
            if board is not None
            else _board_slug_for_connection(conn)
        )
        bound_meta = read_board_metadata(bound_board) if bound_board else None
        bound_is_product_v2 = (
            isinstance(bound_meta, dict)
            and str(bound_meta.get("preset") or "").lower() == "product"
            and _handoff_v2_enabled(bound_meta)
        )
        if bound_is_product_v2:
            if bound_board and effective_board != bound_board:
                raise ValueError(
                    f"project {project_obj.slug!r} is bound to product board "
                    f"{bound_board!r}; create project-linked product tasks on "
                    "that board (for CLI: pass --board before create)"
                )
            _ensure_worktrees_gitignore(project_obj.primary_path)
            if workflow_template_id not in (None, "product"):
                # Explicit non-product metadata is a caller decision; do not
                # overwrite it. This preserves custom workflow experiments.
                pass
            else:
                if workflow_template_id is None:
                    workflow_template_id = "product"
                    workflow_defaulted = True
                if current_step_key is None:
                    current_step_key = "backlog"
                    workflow_defaulted = True

    # Product-workflow enforcement (re-applied from f55580879), MASQUERADE FIX:
    # on a product board, infer the step from assignee/title so a plain role
    # card (e.g. assignee=architect) becomes a proper product story rather than
    # a masquerade that stalls after one phase. Conservative: never touches
    # custom-workflow / non-product cards, and handoff_v2 still owns advancement.
    _wf_board_meta = read_board_metadata(
        _normalize_board_slug(board) if board is not None
        else _board_slug_for_connection(conn)
    )
    if (
        work_item_kind == "card"
        and workflow_template_id == PRODUCT_WORKFLOW_TEMPLATE_ID
        and not current_step_key
    ):
        _inferred_step = _infer_product_step(
            title=title,
            assignee=assignee,
            explicit_step=None,
            product_intent=True,
        )
        if _inferred_step:
            current_step_key = _inferred_step
    elif (
        work_item_kind == "card"
        and workflow_template_id is None
        and _is_product_board_metadata(_wf_board_meta)
        and current_step_key is None
    ):
        _inferred_step = _infer_product_step(
            title=title,
            assignee=assignee,
            explicit_step=None,
            product_intent=_looks_like_product_story(title),
        )
        if _inferred_step:
            workflow_template_id = PRODUCT_WORKFLOW_TEMPLATE_ID
            current_step_key = _inferred_step

    if (
        workflow_template_id == "product_epic"
        or current_step_key == "integration_pending"
    ):
        raise ValueError("engine-owned integration state cannot be materialized directly")
    _validate_product_workflow_state(workflow_template_id, current_step_key)

    parents = tuple(p for p in parents if p)

    # Normalise + validate skills: strip whitespace, drop empties, dedupe
    # (preserving order). Refuse commas inside a single name so we don't
    # invisibly splatter a comma-joined string into one argv slot — the
    # `hermes --skills X,Y` comma syntax is handled in the dispatcher,
    # not here.
    skills_list: Optional[list[str]] = None
    if skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        # Collect all toolset-name confusions up front so the user sees the
        # whole list at once. Raising on the first hit is friendly when the
        # input has one mistake, but agents that confuse skills with toolsets
        # usually pass several at once (`skills=["web", "browser", "terminal"]`)
        # and serial-correcting one per failure round-trips wastes tokens.
        toolset_typos: list[str] = []
        for s in skills:
            if not s:
                continue
            name = str(s).strip()
            if not name:
                continue
            if "," in name:
                raise ValueError(
                    f"skill name cannot contain comma: {name!r} "
                    f"(pass a list of separate names instead of a comma-joined string)"
                )
            if name.casefold() in KNOWN_TOOLSET_NAMES:
                toolset_typos.append(name)
                continue
            if name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        if toolset_typos:
            quoted = ", ".join(repr(n) for n in toolset_typos)
            noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
            raise ValueError(
                f"{quoted} {noun}, not skill name(s). "
                "Put toolsets in the assignee profile's `toolsets:` config "
                "instead of per-task skills. Skills are named skill bundles "
                "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
                "capabilities (e.g. `web`, `browser`, `terminal`)."
            )
        skills_list = cleaned

    # Idempotency check — return the existing task instead of creating a
    # duplicate. Done BEFORE entering write_txn to keep the fast path fast
    # and to avoid holding a write lock during the lookup. Race is
    # acceptable: two concurrent creators with the same key might both
    # insert, at which point both rows exist but the next lookup stabilises.
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]

    now = int(time.time())

    # Resolve workspace_path from board-level default_workdir when the
    # caller did not specify one explicitly. Board defaults represent
    # persistent project checkouts, so only persistent workspace kinds may
    # inherit them. Scratch workspaces are auto-deleted on completion and
    # must stay under the per-board scratch root created by
    # ``resolve_workspace``; inheriting ``default_workdir`` for a scratch
    # task would point cleanup at the user's source tree (#28818). The
    # containment guard in ``_cleanup_workspace`` is the safety rail, but
    # we also stop the bad state from being created in the first place.
    if (
        workspace_path is None
        and project_repo is None
        and workspace_kind in {"dir", "worktree"}
    ):
        board_slug = board if board else get_current_board()
        board_meta = read_board_metadata(board_slug)
        board_default = board_meta.get("default_workdir")
        if board_default:
            workspace_path = str(board_default)

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            # ``allow_nested=True``: graph builders (kanban_swarm.create_swarm)
            # compose create_task calls under one outer commit so the
            # dispatcher can never observe a partially constructed graph.
            with write_txn(conn, allow_nested=True):
                # Determine task status from parent status, unless the caller
                # parks it directly in blocked for human-ops review or in
                # triage for a specifier.
                if initial_status == "blocked":
                    task_status = "blocked"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                elif triage:
                    task_status = "triage"
                else:
                    task_status = "ready"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                        # If any parent is not yet done, we're todo.
                        rows = conn.execute(
                            "SELECT status FROM tasks WHERE id IN "
                            "(" + ",".join("?" * len(parents)) + ")",
                            parents,
                        ).fetchall()
                        if any(r["status"] != "done" for r in rows):
                            task_status = "todo"
                # Even in triage mode we still need to validate parent ids
                # so the eventual link rows don't dangle.
                if triage and parents:
                    missing = _find_missing_parents(conn, parents)
                    if missing:
                        raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                if work_item_kind == "epic":
                    task_status = "todo"

                # Project-linked worktree: a fresh worktree dir under the repo
                # plus a deterministic branch (project slug + task id). Together
                # these kill the random ``wt/<task-id>`` worker fallback and the
                # unanchored ``.worktrees/<id>`` under the dispatcher's cwd.
                if project_obj is not None and workspace_kind == "worktree":
                    if project_repo and not workspace_path:
                        workspace_path = os.path.join(
                            project_repo, ".worktrees", task_id
                        )
                    if not branch_name:
                        # _pdb was imported above when project_obj was resolved.
                        try:
                            branch_name = _pdb.branch_name_for(
                                project_obj, task_id, title=title or ""
                            )
                        except Exception:
                            branch_name = None

                _validate_resolver_cas_fields(
                    {
                        "task_id": task_id,
                        "status": task_status,
                        "assignee": assignee,
                        "project_id": project_id,
                        "workflow_template_id": workflow_template_id,
                        "current_step_key": current_step_key,
                        "workspace_kind": workspace_kind,
                        "workspace_path": workspace_path,
                        "branch_name": branch_name,
                    }
                )
                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        max_runtime_seconds,
                        skills, max_retries, model_override, provider_override,
                        reasoning_effort,
                        goal_mode, goal_max_turns, session_id, completion_contract,
                        workflow_template_id, current_step_key,
                        work_contract_id, work_item_kind,
                        source_commit_required, source_commit_forbidden
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title.strip(),
                        body,
                        assignee,
                        task_status,
                        priority,
                        created_by,
                        now,
                        workspace_kind,
                        workspace_path,
                        branch_name,
                        project_id,
                        tenant,
                        idempotency_key,
                        int(max_runtime_seconds) if max_runtime_seconds is not None else None,
                        json.dumps(skills_list) if skills_list is not None else None,
                        int(max_retries) if max_retries is not None else None,
                        model_override,
                        provider_override,
                        reasoning_effort,
                        1 if goal_mode else 0,
                        int(goal_max_turns) if goal_max_turns is not None else None,
                        session_id,
                        completion_contract,
                        workflow_template_id,
                        current_step_key,
                        work_contract_id,
                        work_item_kind,
                        1 if source_commit_required else 0,
                        1 if source_commit_forbidden else 0,
                    ),
                )
                for pid in parents:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                        (pid, task_id),
                    )
                # Notify-sub inheritance (ACK-edge: the originating channel
                # still hears about a child that BLOCKs, not just the final
                # fan-in) is handled by the single-owner helper below —
                # _inherit_notify_subs copies every routing/delivery column.
                _append_event(
                    conn,
                    task_id,
                    "created",
                    {
                        "assignee": assignee,
                        "status": task_status,
                        "parents": list(parents),
                        "tenant": tenant,
                        "workspace_kind": workspace_kind,
                        "workspace_path": workspace_path,
                        "branch_name": branch_name,
                        "project_id": project_id,
                        "skills": list(skills_list) if skills_list else None,
                        "goal_mode": bool(goal_mode) or None,
                        "workflow_template_id": workflow_template_id,
                        "current_step_key": current_step_key,
                        "model_override": model_override,
                        "provider_override": provider_override,
                        "source_commit_required": bool(source_commit_required),
                        "source_commit_forbidden": bool(source_commit_forbidden),
                    },
                )
                if workflow_defaulted:
                    _append_event(
                        conn,
                        task_id,
                        "workflow_defaulted",
                        {
                            "workflow_template_id": workflow_template_id,
                            "current_step_key": current_step_key,
                            "project_id": project_id,
                        },
                    )
                _inherit_notify_subs(conn, task_id, parents, created_at=now)
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
            # Retry with a fresh id.
            continue
    raise RuntimeError("unreachable")



def _board_meta_for(board: Optional[str]) -> dict:
    return read_board_metadata(board if board else get_current_board())


def _project_branch_name(project_obj: Any, task_id: str, title: Optional[str]) -> Optional[str]:
    from hermes_cli import projects_db as _pdb

    try:
        return _pdb.branch_name_for(project_obj, task_id, title=title or "")
    except Exception:
        return None


def _link(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )


def _missing_task_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    """Subset of ``ids`` (order kept) with no ``tasks`` row."""
    ids = list(ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id FROM tasks WHERE id IN ({placeholders})", ids).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in ids if p not in present]


def _inherit_notify_subs(
    conn: sqlite3.Connection, child_id: str, parents: Iterable[str], *,
    created_at: Optional[int] = None,
) -> None:
    """Copy parents' notify subscriptions to a child, cursor caught up to the
    child's current event so a late ``link_tasks`` never replays history.

    Single owner of inheritance (create_task, link_tasks, decompose). It must
    copy EVERY routing/delivery column: dropping ``chat_type`` made DM-originated
    completions wake a fresh group session instead of the originating DM.

    Omitting columns here silently degrades routing: a DM-originated child completion falls back to
    chat_type='group' and wakes a fresh group-scoped session instead of the originating DM (issue #73030).
    """
    parent_ids = tuple(dict.fromkeys(p for p in parents if p))
    if not parent_ids:
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?", (child_id,),
    ).fetchone()
    cursor = int(row["cursor"] if row is not None else 0)
    placeholders = ",".join("?" * len(parent_ids))
    conn.execute(
        f"""
        INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
             chat_type, notifier_profile, delivery_mode, delivery_metadata,
             created_at, last_event_id)
        SELECT ?, platform, chat_id, thread_id, user_id, user_id_alt,
               COALESCE(chat_type, 'dm'), notifier_profile,
               COALESCE(delivery_mode, 'notify'), delivery_metadata, ?, ?
          FROM kanban_notify_subs
         WHERE task_id IN ({placeholders})
        """,
        (child_id, int(created_at if created_at is not None else time.time()), cursor, *parent_ids),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


# Canonical sort-order mappings for ``hermes kanban list --sort``.
# Each value is a raw SQL fragment appended after ``ORDER BY``.
VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
}


def list_tasks(
    conn: sqlite3.Connection, *, assignee: Optional[str] = None, status: Optional[str] = None,
    tenant: Optional[str] = None, session_id: Optional[str] = None, include_archived: bool = False,
    limit: Optional[int] = None, order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None, current_step_key: Optional[str] = None,
) -> list[Task]:
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    for col, val in (
        ("assignee", _canonical_assignee(assignee)), ("status", status), ("tenant", tenant),
        ("session_id", session_id), ("workflow_template_id", workflow_template_id),
        ("current_step_key", current_step_key),
    ):
        if val is not None:
            query += f" AND {col} = ?"
            params.append(val)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in VALID_SORT_ORDERS:
            raise ValueError(f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}")
        query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
    else:
        query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign/reassign; raises RuntimeError while the task is running under a claim."""
    profile = _canonical_assignee(profile)
    _validate_resolver_cas_fields({"assignee": profile})
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        routing_error = resolver_routing_error(conn, task_id, profile)
        if routing_error is not None:
            raise ValueError(routing_error)
        if row["assignee"] != profile:
            # The failure streak is per task/profile; a new profile starts fresh.
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?", (profile, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        _append_event(conn, task_id, "assigned", {"assignee": profile})
    # Observer fires AFTER commit so subscribers see durable state.
    notify_task_updated(conn, task_id, ("assignee",))
    return True


TASK_SNAPSHOT_FIELDS = (
    "status",
    "title",
    "assignee",
    "current_step_key",
    "current_run_id",
)


class TaskSnapshotConflict(RuntimeError):
    """Raised when an operator mutation loses its expected task snapshot."""

    def __init__(self, action: str, current: dict[str, Any]):
        super().__init__(f"task changed; refresh before {action}")
        self.current = current


def task_snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "status": row["status"],
        "title": row["title"],
        "assignee": _canonical_assignee(row["assignee"]),
        "current_step_key": row["current_step_key"],
        "current_run_id": row["current_run_id"],
    }


def assert_task_snapshot(row: sqlite3.Row, expected: dict[str, Any], *, action: str) -> None:
    current = task_snapshot_from_row(row)
    for field, value in expected.items():
        if field not in TASK_SNAPSHOT_FIELDS:
            continue
        if field == "assignee":
            value = _canonical_assignee(value)
        if current[field] != value:
            raise TaskSnapshotConflict(action, current)


@contextlib.contextmanager
def conditional_task_write(conn, task_id, expected, *, action):
    """Hold one write lock across snapshot validation and canonical mutation."""
    with write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None:
            assert_task_snapshot(row, expected, action=action)
        yield row


@contextlib.contextmanager
def conditional_tasks_write(
    conn: sqlite3.Connection,
    expected_by_task_id: dict[str, dict[str, Any]],
    *,
    action: str,
):
    """Hold one write lock while validating and mutating a task batch."""
    with write_txn(conn):
        for task_id, expected in expected_by_task_id.items():
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is not None:
                assert_task_snapshot(row, expected, action=action)
        yield



_SCOPED_EXPECTED_TASK_SNAPSHOT = ContextVar("kanban_expected_task_snapshot", default=None)


@contextlib.contextmanager
def scoped_expected_task_snapshot(task_id, expected, *, action):
    token = _SCOPED_EXPECTED_TASK_SNAPSHOT.set((task_id, expected, action))
    try:
        yield
    finally:
        _SCOPED_EXPECTED_TASK_SNAPSHOT.reset(token)


def _assert_scoped_expected_task_snapshot(row, task_id, *, default_action):
    scoped = _SCOPED_EXPECTED_TASK_SNAPSHOT.get()
    if scoped is None or scoped[0] != task_id:
        return
    _, expected, action = scoped
    assert_task_snapshot(row, expected, action=action or default_action)


CONFIGURE_TASK_SNAPSHOT_FIELDS = TASK_SNAPSHOT_FIELDS + (
    "source_policy",
    "max_retries",
    "max_runtime_seconds",
    "goal_mode",
)
CONFIGURE_TASK_ELIGIBLE_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "blocked", "review"}
)
UNLINK_TASK_ELIGIBLE_STATUSES = CONFIGURE_TASK_ELIGIBLE_STATUSES


def _execution_source_policy(row: Mapping[str, Any]) -> str:
    required = bool(row["source_commit_required"])
    forbidden = bool(row["source_commit_forbidden"])
    if required and forbidden:
        raise RuntimeError(
            "task has conflicting source commit policy flags; repair the task "
            "before changing its execution contract"
        )
    if required:
        return "required"
    if forbidden:
        return "forbidden"
    return "none"


def execution_contract_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_policy": _execution_source_policy(row),
        "max_retries": row["max_retries"],
        "max_runtime_seconds": row["max_runtime_seconds"],
        "goal_mode": bool(row["goal_mode"]),
    }


def task_execution_contract(task: Task) -> dict[str, Any]:
    if task.source_commit_required and task.source_commit_forbidden:
        raise RuntimeError("task has conflicting source commit policy flags")
    source_policy = "none"
    if task.source_commit_required:
        source_policy = "required"
    elif task.source_commit_forbidden:
        source_policy = "forbidden"
    return {
        "source_policy": source_policy,
        "max_retries": task.max_retries,
        "max_runtime_seconds": task.max_runtime_seconds,
        "goal_mode": bool(task.goal_mode),
    }


def configure_task_snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **task_snapshot_from_row(row),
        **execution_contract_from_row(row),
    }


def _validate_optional_positive_int(value: Any, field_name: str) -> None:
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(f"{field_name} must be a positive integer or null")


def _validate_execution_contract_values(
    *,
    source_policy: Any,
    max_retries: Any,
    max_runtime_seconds: Any,
    goal_mode: Any,
) -> None:
    if source_policy not in {"none", "required", "forbidden"}:
        raise ValueError(
            "source_policy must be one of 'none', 'required', or 'forbidden'"
        )
    _validate_optional_positive_int(max_retries, "max_retries")
    _validate_optional_positive_int(max_runtime_seconds, "max_runtime_seconds")
    if type(goal_mode) is not bool:
        raise ValueError("goal_mode must be a boolean")


def _validate_configure_task_expected(expected: Mapping[str, Any]) -> None:
    if not isinstance(expected, Mapping):
        raise ValueError("expected must be the complete task snapshot object")
    fields = set(CONFIGURE_TASK_SNAPSHOT_FIELDS)
    missing = sorted(fields - set(expected))
    extra = sorted(set(expected) - fields)
    if missing:
        raise ValueError(
            f"expected is missing task snapshot field(s): {', '.join(missing)}"
        )
    if extra:
        raise ValueError(
            f"expected has unsupported task snapshot field(s): {', '.join(extra)}"
        )
    if not isinstance(expected["status"], str):
        raise ValueError("expected.status must be a string")
    if not isinstance(expected["title"], str):
        raise ValueError("expected.title must be a string")
    for field_name in ("assignee", "current_step_key"):
        if expected[field_name] is not None and not isinstance(
            expected[field_name], str
        ):
            raise ValueError(f"expected.{field_name} must be a string or null")
    current_run_id = expected["current_run_id"]
    if current_run_id is not None and type(current_run_id) is not int:
        raise ValueError("expected.current_run_id must be an integer or null")
    _validate_execution_contract_values(
        source_policy=expected["source_policy"],
        max_retries=expected["max_retries"],
        max_runtime_seconds=expected["max_runtime_seconds"],
        goal_mode=expected["goal_mode"],
    )


def _validate_unlink_task_expected(expected: Mapping[str, Any]) -> None:
    if not isinstance(expected, Mapping):
        raise ValueError("expected must be the complete child task snapshot object")
    fields = set(TASK_SNAPSHOT_FIELDS)
    missing = sorted(fields - set(expected))
    extra = sorted(set(expected) - fields)
    if missing:
        raise ValueError(
            f"expected is missing child task snapshot field(s): {', '.join(missing)}"
        )
    if extra:
        raise ValueError(
            f"expected has unsupported child task snapshot field(s): {', '.join(extra)}"
        )
    if not isinstance(expected["status"], str):
        raise ValueError("expected.status must be a string")
    if not isinstance(expected["title"], str):
        raise ValueError("expected.title must be a string")
    for field_name in ("assignee", "current_step_key"):
        if expected[field_name] is not None and not isinstance(
            expected[field_name], str
        ):
            raise ValueError(f"expected.{field_name} must be a string or null")
    current_run_id = expected["current_run_id"]
    if current_run_id is not None and type(current_run_id) is not int:
        raise ValueError("expected.current_run_id must be an integer or null")


def _task_has_active_execution(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> bool:
    keys = set(row.keys())
    if "running" in keys and bool(row["running"]):
        return True
    if "current_run_id" in keys and row["current_run_id"] is not None:
        return True
    if "claim_lock" in keys and row["claim_lock"] is not None:
        return True
    return conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND status = 'running' LIMIT 1",
        (row["id"],),
    ).fetchone() is not None


def configure_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected: Mapping[str, Any],
    source_policy: str,
    max_retries: Optional[int],
    max_runtime_seconds: Optional[int],
    goal_mode: bool,
) -> bool:
    """CAS-replace an idle Default-board card's execution contract atomically."""
    _validate_configure_task_expected(expected)
    _validate_execution_contract_values(
        source_policy=source_policy,
        max_retries=max_retries,
        max_runtime_seconds=max_runtime_seconds,
        goal_mode=goal_mode,
    )

    with write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return False
        current = configure_task_snapshot_from_row(row)
        normalized_expected = dict(expected)
        normalized_expected["assignee"] = _canonical_assignee(
            normalized_expected["assignee"]
        )
        if current != normalized_expected:
            raise TaskSnapshotConflict("configure task", current)
        if _task_has_active_execution(conn, row):
            raise RuntimeError(
                f"cannot configure task {task_id}: it has an active/current run"
            )
        if row["status"] not in CONFIGURE_TASK_ELIGIBLE_STATUSES:
            raise RuntimeError(
                f"cannot configure task {task_id}: status {row['status']!r} is not eligible"
            )

        before = execution_contract_from_row(row)
        conn.execute(
            """
            UPDATE tasks
               SET source_commit_required = ?,
                   source_commit_forbidden = ?,
                   max_retries = ?,
                   max_runtime_seconds = ?,
                   goal_mode = ?
             WHERE id = ?
            """,
            (
                1 if source_policy == "required" else 0,
                1 if source_policy == "forbidden" else 0,
                max_retries,
                max_runtime_seconds,
                1 if goal_mode else 0,
                task_id,
            ),
        )
        after_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if after_row is None:
            raise RuntimeError(f"task disappeared while configuring {task_id}")
        _append_event(
            conn,
            task_id,
            "execution_contract_configured",
            {
                "before": before,
                "after": execution_contract_from_row(after_row),
            },
        )
        return True


def set_model_override(
    conn: sqlite3.Connection, task_id: str, model: Optional[str], provider: Optional[str] = None,
) -> bool:
    """Set (empty ``model`` clears BOTH) the per-task model/provider override.
    Allowed while ``running``: it applies on the NEXT dispatch, which is the
    rate-limit-recovery flow (set, then reclaim/retry)."""
    model, provider = _validate_model_override(model, provider)
    return _set_task_override(
        conn, task_id,
        "UPDATE tasks SET model_override = ?, provider_override = ? WHERE id = ?", (model, provider),
        "model_override_set", {"model": model, "provider": provider},
        ("model_override", "provider_override"), archived_msg="cannot set model override",
    )


def _set_task_override(
    conn: sqlite3.Connection, task_id: str, sql: str, params: tuple, event_kind: str, payload: dict,
    changed_fields: tuple[str, ...], *, archived_msg: str,
) -> bool:
    """Per-task override write: refuse archived tasks, record ``event_kind``,
    then fire the task-updated observer AFTER commit (RFC #58548)."""
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None:
            return False
        if status == "archived":
            raise RuntimeError(f"{archived_msg} on archived task {task_id}")
        conn.execute(sql, (*params, task_id))
        _append_event(conn, task_id, event_kind, payload)
    notify_task_updated(conn, task_id, changed_fields)
    return True


def set_reasoning_effort(conn: sqlite3.Connection, task_id: str, effort: Optional[str]) -> bool:
    """Set (empty clears; ``"none"`` pins thinking OFF) the per-task reasoning
    effort. Independent of the model override so clearing one never resets the
    other; applies on the NEXT dispatch, so settable while running."""
    effort = normalize_reasoning_effort(effort)
    return _set_task_override(
        conn, task_id, "UPDATE tasks SET reasoning_effort = ? WHERE id = ?", (effort,),
        "reasoning_effort_set", {"reasoning_effort": effort},
        ("reasoning_effort",), archived_msg="cannot set reasoning effort",
    )


# --- Links ---

def link_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    with write_txn(conn):
        missing = _missing_task_ids(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        _link(conn, parent_id, child_id)
        # If child was ready but parent is not yet done, demote child to todo.
        if _task_status(conn, parent_id) != "done":
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'", (child_id,),
            )
        _append_event(
            conn, child_id, "linked", {"parent": parent_id, "child": child_id},
        )
        _inherit_notify_subs(conn, child_id, (parent_id,))


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """True iff ``parent_id`` is already a descendant of ``child_id``."""
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def unlink_tasks(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    expected: Optional[Mapping[str, Any]] = None,
    failure_limit: Optional[int] = None,
) -> bool:
    """Remove one exact edge and reconsider only its idle child for readiness."""
    if not isinstance(parent_id, str) or not parent_id.strip():
        raise ValueError("parent_id must be a non-empty string")
    if not isinstance(child_id, str) or not child_id.strip():
        raise ValueError("child_id must be a non-empty string")
    if expected is not None:
        _validate_unlink_task_expected(expected)
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    board_meta = product_board_metadata(_board_slug_for_connection(conn))
    release_measure_unblocks = _product_release_measure_unblocks_dependents(board_meta)
    with write_txn(conn):
        child = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (child_id,)
        ).fetchone()
        if child is None:
            return False
        if expected is not None:
            current = task_snapshot_from_row(child)
            normalized_expected = dict(expected)
            normalized_expected["assignee"] = _canonical_assignee(
                normalized_expected["assignee"]
            )
            if current != normalized_expected:
                raise TaskSnapshotConflict("unlink task", current)
        if _task_has_active_execution(conn, child):
            raise RuntimeError(
                f"cannot unlink task {child_id}: it has an active/current run"
            )
        if child["status"] not in UNLINK_TASK_ELIGIBLE_STATUSES:
            raise RuntimeError(
                f"cannot unlink task {child_id}: status {child['status']!r} is not eligible"
            )
        if not conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        ).fetchone():
            return False
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, child_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"task link disappeared while unlinking {parent_id} -> {child_id}"
            )
        _append_event(
            conn, child_id, "unlinked",
            {"parent": parent_id, "child": child_id},
        )
        _promote_ready_task(
            conn,
            child_id,
            failure_limit=int(failure_limit),
            release_measure_unblocks=release_measure_unblocks,
        )
        return True


def _linked_ids(conn: sqlite3.Connection, want: str, where: str, task_id: str) -> list[str]:
    rows = conn.execute(
        f"SELECT {want} FROM task_links WHERE {where} = ? ORDER BY {want}", (task_id,)
    ).fetchall()
    return [r[want] for r in rows]


# Dependency edge removed — re-evaluate promotion eligibility for the child immediately. Matches the
# contract of complete_task and unblock_task; without this the child stays stuck in todo until the next
# dispatcher tick or a manual `hermes kanban recompute` (issue #22459).
def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "parent_id", "child_id", task_id)


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "child_id", "parent_id", task_id)


def task_graph_contexts(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, dict]:
    """Bulk-load compact direct graph state for graph-aware diagnostics."""
    ordered_ids = list(dict.fromkeys(str(task_id) for task_id in task_ids if task_id))
    contexts = {task_id: {"parents": [], "children": []} for task_id in ordered_ids}
    if not ordered_ids:
        return contexts

    placeholders = ",".join("?" for _ in ordered_ids)
    for bucket, own, other in (("parents", "child_id", "parent_id"), ("children", "parent_id", "child_id")):
        for row in conn.execute(
            f"SELECT l.{own} AS owner_id, t.id, t.title, t.status "
            f"FROM task_links l JOIN tasks t ON t.id = l.{other} "
            f"WHERE l.{own} IN ({placeholders}) ORDER BY l.{own}, t.id", tuple(ordered_ids),
        ).fetchall():
            contexts[row["owner_id"]][bucket].append(
                {"id": row["id"], "title": row["title"], "status": row["status"]}
            )
    return contexts


def task_graph_context(conn: sqlite3.Connection, task_id: str) -> dict:
    """Return compact direct parent/child state for one task."""
    return task_graph_contexts(conn, [task_id])[task_id]


# --- Comments & events ---

def add_comment(conn: sqlite3.Connection, task_id: str, author: str, body: str) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    # ``allow_nested=True``: graph builders (kanban_swarm blackboard seeding)
    # compose comment writes under one outer commit.
    with write_txn(conn, allow_nested=True):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)", (task_id, author.strip(), body.strip(), now),
        )
        _append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
        return int(cur.lastrowid or 0)


def _require_task(conn: sqlite3.Connection, task_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ValueError(f"unknown task {task_id}")


def _task_rows(conn: sqlite3.Connection, table: str, task_id: str, order: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE task_id = ? ORDER BY {order}", (task_id,)
    ).fetchall()


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    return [Comment.from_row(r) for r in _task_rows(conn, "task_comments", task_id, "created_at ASC")]


def list_comments_after(
    conn: sqlite3.Connection, task_id: str, *, after_id: int = 0
) -> list[Comment]:
    """Comments with ``id > after_id`` — keyed on rowid, not ``created_at``, so a
    same-second burst is never skipped (live worker comment bridge)."""
    rows = conn.execute(
        "SELECT id, task_id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND id > ? ORDER BY id ASC", (task_id, int(after_id)),
    ).fetchall()
    return [Comment.from_row(r) for r in rows]


# --- Attachments ---

class AttachmentTooLarge(ValueError):
    """Attachment over the size cap. A ``ValueError`` so generic 400 handlers
    still catch it while the tool/CLI can give a 413-style message."""


def _safe_attachment_name(raw: str) -> str:
    """Client filename -> safe basename: strip directories (both separators),
    control chars and leading dots (no dotfiles, no traversal); ValueError when
    nothing usable remains. Only ever joined under the per-task attachments dir."""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00").strip()
    name = name.lstrip(".").strip()
    if not name:
        raise ValueError("invalid attachment filename")
    return name[:200]


def _collision_free_path(dest_dir: Path, safe_name: str) -> Path:
    """``foo.pdf`` -> ``foo.pdf``, ``foo (1).pdf``, ... first one that doesn't exist."""
    stem, dot, ext = safe_name.partition(".")
    candidate = safe_name
    n = 1
    while (dest_dir / candidate).exists():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return dest_dir / candidate


def store_attachment_bytes(
    conn: sqlite3.Connection, task_id: str, filename: str, data: bytes, *,
    content_type: Optional[str] = None, uploaded_by: Optional[str] = None,
    board: Optional[str] = None, max_bytes: Optional[int] = None,
) -> int:
    """Single attachment write path (dashboard, tools, CLI): size cap, safe
    basename, collision-free blob under :func:`task_attachments_dir`, then the
    metadata row. Raises :class:`AttachmentTooLarge` / ``ValueError``; a blob
    whose row insert fails is removed before re-raising. Returns the new id."""
    if max_bytes is None:
        max_bytes = KANBAN_ATTACHMENT_MAX_BYTES
    if len(data) > max_bytes:
        raise AttachmentTooLarge(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
    safe_name = _safe_attachment_name(filename)
    dest_dir = task_attachments_dir(task_id, board=board)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _collision_free_path(dest_dir, safe_name)
    dest_path.write_bytes(data)
    try:
        return add_attachment(
            conn, task_id, filename=dest_path.name, stored_path=str(dest_path.resolve()),
            content_type=content_type, size=len(data), uploaded_by=uploaded_by,
        )
    except Exception:
        # Don't leave an orphan blob if the metadata insert fails (most
        # commonly: the task id doesn't exist).
        with contextlib.suppress(OSError):
            dest_path.unlink(missing_ok=True)
        raise


def add_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str,
    content_type: Optional[str] = None, size: int = 0, uploaded_by: Optional[str] = None,
) -> int:
    """Record the metadata row (+ ``attached`` event) for a blob the caller already wrote."""
    if not filename or not filename.strip():
        raise ValueError("attachment filename is required")
    if not stored_path or not stored_path.strip():
        raise ValueError("attachment stored_path is required")
    now = int(time.time())
    with write_txn(conn):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, filename.strip(), stored_path, content_type, int(size), uploaded_by, now),
        )
        _append_event(
            conn, task_id, "attached",
            {"filename": filename.strip(), "size": int(size), "by": uploaded_by},
        )
        return int(cur.lastrowid or 0)


def list_attachments(conn: sqlite3.Connection, task_id: str) -> list[Attachment]:
    return [Attachment.from_row(r) for r in _task_rows(conn, "task_attachments", task_id, "created_at ASC, id ASC")]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    r = conn.execute("SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)).fetchone()
    return None if r is None else Attachment.from_row(r)


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    """Delete the row (source of truth) and best-effort its blob; None when no row matched."""
    with write_txn(conn):
        att = get_attachment(conn, attachment_id)
        if att is None:
            return None
        conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
        _append_event(conn, att.task_id, "attachment_removed", {"filename": att.filename})
    with contextlib.suppress(OSError):
        p = Path(att.stored_path)
        if p.is_file():
            p.unlink()
    return att


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    return [Event.from_row(r) for r in _task_rows(conn, "task_events", task_id, "created_at ASC, id ASC")]


def _insert_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str, created_at: int,
) -> None:
    """Raw comment INSERT for callers already inside a write txn (``add_comment``
    opens its own txn and emits ``commented``)."""
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)", (task_id, author, body, created_at),
    )


def _append_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict] = None, *,
    run_id: Optional[int] = None,
) -> int:
    """Insert an event row inside the caller's txn; ``run_id`` groups it by attempt (NULL = task-scoped)."""
    cursor = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (task_id, run_id, kind, _json_or_null(payload), int(time.time())),
    )
    return int(cursor.lastrowid)


_REWORK_DIRECTIVE_ORIGIN_KINDS = frozenset(
    {"test", "review", "integration", "refresh"}
)
_REWORK_DIRECTIVE_TARGET_PHASES = frozenset({"architecture", "development"})
_FULL_REWORK_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _rework_directive_from_row(row: sqlite3.Row) -> ReworkDirective:
    try:
        raw_findings = json.loads(row["findings_json"])
    except (TypeError, ValueError):
        raw_findings = []
    findings = (
        tuple(item if isinstance(item, str) else str(item) for item in raw_findings)
        if isinstance(raw_findings, list)
        else ()
    )
    return ReworkDirective(
        id=int(row["id"]),
        task_id=row["task_id"],
        origin_kind=row["origin_kind"],
        origin_run_id=(
            int(row["origin_run_id"])
            if row["origin_run_id"] is not None
            else None
        ),
        origin_intent_key=row["origin_intent_key"],
        origin_phase=row["origin_phase"],
        target_phase=row["target_phase"],
        rejected_branch=row["rejected_branch"],
        rejected_sha=row["rejected_sha"],
        epic_tip_sha=row["epic_tip_sha"],
        findings=findings,
        status=row["status"],
        created_at=int(row["created_at"]),
        resolved_by_run_id=(
            int(row["resolved_by_run_id"])
            if row["resolved_by_run_id"] is not None
            else None
        ),
    )


def create_rework_directive(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    origin_kind: str,
    origin_run_id: Optional[int] = None,
    origin_intent_key: Optional[str] = None,
    origin_phase: str,
    target_phase: str,
    rejected_branch: Optional[str] = None,
    rejected_sha: Optional[str] = None,
    epic_tip_sha: Optional[str] = None,
    findings: Iterable[str],
) -> ReworkDirective:
    """Append a rework directive, superseding the previous active one."""
    origin_kind = str(origin_kind or "").strip()
    origin_phase = str(origin_phase or "").strip()
    target_phase = str(target_phase or "").strip()
    if origin_kind not in _REWORK_DIRECTIVE_ORIGIN_KINDS:
        raise ValueError(f"invalid rework directive origin kind: {origin_kind}")
    if not origin_phase:
        raise ValueError("rework directive origin phase is required")
    if target_phase not in _REWORK_DIRECTIVE_TARGET_PHASES:
        raise ValueError(f"invalid rework directive target phase: {target_phase}")
    normalized_findings = tuple(str(item).strip() for item in findings)
    if not normalized_findings or any(not item for item in normalized_findings):
        raise ValueError("rework directive findings must be non-empty strings")

    def _clean_optional(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    now = int(time.time())
    with write_txn(conn, allow_nested=True):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        conn.execute(
            "UPDATE product_rework_directives SET status = 'superseded' "
            "WHERE task_id = ? AND status = 'active'",
            (task_id,),
        )
        cursor = conn.execute(
            "INSERT INTO product_rework_directives ("
            "task_id, origin_kind, origin_run_id, origin_intent_key, "
            "origin_phase, target_phase, rejected_branch, rejected_sha, "
            "epic_tip_sha, findings_json, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                task_id,
                origin_kind,
                int(origin_run_id) if origin_run_id is not None else None,
                _clean_optional(origin_intent_key),
                origin_phase,
                target_phase,
                _clean_optional(rejected_branch),
                _clean_optional(rejected_sha),
                _clean_optional(epic_tip_sha),
                json.dumps(list(normalized_findings), ensure_ascii=False),
                now,
            ),
        )
        directive_id = cursor.lastrowid
        if directive_id is None:
            raise RuntimeError("rework directive insert did not return an id")
        row = conn.execute(
            "SELECT * FROM product_rework_directives WHERE id = ?",
            (int(directive_id),),
        ).fetchone()
        assert row is not None
        return _rework_directive_from_row(row)



def active_rework_directive(
    conn: sqlite3.Connection, task_id: str
) -> Optional[ReworkDirective]:
    """Return the task's one active directive, if it has one."""
    row = conn.execute(
        "SELECT * FROM product_rework_directives "
        "WHERE task_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return _rework_directive_from_row(row) if row is not None else None



def resolve_rework_directive(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    new_sha: Optional[str],
    resolved_by_run_id: Optional[int],
) -> bool:
    """Resolve an active directive only after a different Development SHA."""
    candidate_sha = str(new_sha or "").strip()
    if not _FULL_REWORK_SHA_RE.fullmatch(candidate_sha):
        return False
    with write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT id, rejected_sha FROM product_rework_directives "
            "WHERE task_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        rejected_sha = str(row["rejected_sha"] or "").strip()
        if candidate_sha == rejected_sha:
            return False
        updated = conn.execute(
            "UPDATE product_rework_directives "
            "SET status = 'resolved', resolved_by_run_id = ? "
            "WHERE id = ? AND status = 'active'",
            (
                int(resolved_by_run_id) if resolved_by_run_id is not None else None,
                int(row["id"]),
            ),
        )
        return updated.rowcount == 1



def _end_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
    status: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> Optional[int]:
    """Close the currently-active run for ``task_id`` and clear the pointer.

    ``outcome`` is the semantic result (completed / blocked / crashed /
    timed_out / spawn_failed / gave_up / reclaimed). ``status`` is the
    run-row status (usually just ``outcome``, but callers can pass it
    explicitly). Returns the closed run_id or ``None`` if no active run
    existed (e.g. a CLI user calling ``hermes kanban complete`` on a
    task that was never claimed).
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row or not row["current_run_id"]:
        return None
    run_id = (
        int(expected_run_id)
        if expected_run_id is not None
        else int(row["current_run_id"])
    )
    if int(row["current_run_id"]) != run_id:
        return None
    active = conn.execute(
        "SELECT id, metadata FROM task_runs "
        "WHERE id = ? AND task_id = ? AND ended_at IS NULL",
        (run_id, task_id),
    ).fetchone()
    if active is None:
        return None
    final_metadata = dict(metadata or {})
    try:
        active_metadata = (
            json.loads(active["metadata"]) if active["metadata"] else {}
        )
    except (TypeError, ValueError):
        active_metadata = {}
    dispatcher_metadata_conflicts: dict[str, dict[str, Any]] = {}
    if isinstance(active_metadata, dict):
        # Canonical resolution can enrich this marker with its model; ordinary
        # worker completion cannot supply it (reserved-metadata guards).
        if "resolver" in active_metadata:
            final_metadata.setdefault("resolver", active_metadata["resolver"])
        for key in (
            "test_branch",
            "test_head_sha",
            "review_branch",
            "review_base_sha",
            "review_head_sha",
            "executor",
            "source_completion_intent",
            "source_completion_receipt",
        ):
            value = active_metadata.get(key)
            if value is not None:
                if key in final_metadata and final_metadata[key] != value:
                    dispatcher_metadata_conflicts[key] = {
                        "dispatcher": value,
                        "worker": final_metadata[key],
                    }
                final_metadata[key] = value
    closed = conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_lock    = NULL,
               claim_expires = NULL,
               worker_pid    = NULL
         WHERE id = ?
           AND task_id = ?
           AND ended_at IS NULL
        """,
        (
            status or outcome,
            outcome,
            summary,
            error,
            (
                json.dumps(final_metadata, ensure_ascii=False)
                if final_metadata
                else None
            ),
            now,
            run_id,
            task_id,
        ),
    )
    cleared = conn.execute(
        "UPDATE tasks SET current_run_id = NULL WHERE id = ? AND current_run_id = ?",
        (task_id, run_id),
    )
    if closed.rowcount != 1 or cleared.rowcount != 1:
        return None
    if dispatcher_metadata_conflicts:
        _append_event(
            conn,
            task_id,
            "dispatcher_metadata_conflict",
            {"conflicts": dispatcher_metadata_conflicts},
            run_id=run_id,
        )
    return run_id



def _first_line(text: Optional[str], limit: int) -> str:
    """First non-blank-stripped line of ``text`` capped at ``limit`` chars; "" when empty."""
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _opt_int(value: Any) -> Optional[int]:
    """``int(value)`` or ``None`` when ``value`` is ``None`` (NULL column passthrough)."""
    return int(value) if value is not None else None


def _json_or_null(obj: Any) -> Optional[str]:
    """JSON text for a payload/metadata column; falsy -> NULL."""
    return json.dumps(obj, ensure_ascii=False) if obj else None


def _task_status(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Current ``tasks.status`` for ``task_id``, or ``None`` when no such row."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else None


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


def _end_or_synthesize_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, status: str,
    summary: Optional[str] = None, metadata: Optional[dict] = None, synthesize: bool,
) -> Optional[int]:
    """:func:`_end_run`; when no run was active and ``synthesize`` holds, record a
    zero-duration run instead so the handoff fields survive in attempt history."""
    run_id = _end_run(conn, task_id, outcome=outcome, status=status, summary=summary, metadata=metadata)
    if run_id is None and synthesize:
        run_id = _synthesize_ended_run(conn, task_id, outcome=outcome, summary=summary, metadata=metadata)
    return run_id


def _synthesize_ended_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None,
    step_key: Optional[str] = None,
) -> int:
    """Zero-duration closed run for a terminal transition on a never-claimed
    task, so the handoff fields aren't silently dropped (``_end_run`` is a
    no-op then). ``started_at == ended_at`` keeps elapsed stats honest. Does
    NOT touch the tasks row."""
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    profile = trow["assignee"] if trow else None
    stored_step_key = step_key if step_key is not None else (trow["current_step_key"] if trow else None)
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, stored_step_key, outcome, outcome, summary, error, _json_or_null(metadata),
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# --- Dependency resolution (todo -> ready) ---

def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the newest ``blocked``/``unblocked`` event is ``blocked`` — an
    explicit ``kanban_block`` that must wait for an operator. A breaker trip
    emits ``gave_up`` (not ``blocked``) and so auto-recovers, as does a task
    with no such event at all (direct DB edit).

    See #28712.
    Returns ``False`` when there is no such event at all (e.g. the task was set to ``status='blocked'`` by
    the circuit breaker or by direct DB manipulation) — preserves the pre-#28712 auto-recover semantics for
    that path.
    """
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return bool(row) and row["kind"] == "blocked"


def _latest_event(
    conn: sqlite3.Connection, task_id: str, kind: str, run_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """Newest ``task_events`` row of ``kind`` (optionally scoped to one run)."""
    sql = "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?"
    params: tuple[Any, ...] = (task_id, kind)
    if run_id is not None:
        sql += " AND run_id = ?"
        params = (*params, int(run_id))
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()


def _resume_status_from_events(conn: sqlite3.Connection, task_id: str) -> str:
    """``review`` when the newest lifecycle event carries a review
    ``resume_status``/``retry_status``/``source_status``, else ``ready`` (legacy)."""
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind IN ("
        "'blocked', 'block_loop_detected', 'dependency_wait', 'gave_up', "
        "'unblocked', 'changes_requested', 'review_reopened', 'status', 'reclaimed', "
        "'stale', 'timed_out', 'crashed', 'spawn_failed', 'rate_limited'"
        ") ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    payload = _json_dict(_row_get(row, "payload"))
    for key in ("resume_status", "retry_status", "source_status"):
        if payload.get(key) == "review":
            return "review"
    return "ready"


def recompute_ready(
    conn: sqlite3.Connection, failure_limit: int = None,
) -> int:
    """Promote ``todo`` tasks when all parents are dependency-satisfied.

    By default a parent is satisfied only when ``done`` or ``archived``. Product
    boards may explicitly opt into autonomous dependency flow, where a parent
    in the visible ``release_measure`` step also satisfies children so coding
    work does not stall at a release/measurement audit lane.

    Returns the number of tasks promoted.  Safe to call inside or outside
    an existing transaction; it opens its own IMMEDIATE txn.

    1. The most recent block event was a worker-initiated ``kanban_block`` — those stay blocked until an
    explicit ``kanban_unblock`` (#28712).
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    board_meta = product_board_metadata(_board_slug_for_connection(conn))
    release_measure_unblocks = _product_release_measure_unblocks_dependents(board_meta)
    promoted = 0
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id "
            "FROM tasks WHERE status IN ('todo', 'blocked')"
        ).fetchall()
        for row in todo_rows:
            if _promote_ready_task(
                conn,
                row["id"],
                failure_limit=int(failure_limit),
                release_measure_unblocks=release_measure_unblocks,
            ):
                promoted += 1
    return promoted


# --- Claim / complete / block ---

def _parents_satisfied(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether every direct parent is terminal for dependency gating."""
    return conn.execute(
        # Check if this task has children that still need the workspace. If any child is not yet
        # done/archived, defer cleanup so the child can read handoff artifacts from the workspace (#33774).
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? "
        "AND p.status NOT IN ('done', 'archived') LIMIT 1", (task_id,),
    ).fetchone() is None


def _claim_and_open_run(
    conn: sqlite3.Connection, task_id: str, source_status: str, lock: str, expires: int, now: int,
    *, event_extra: Optional[dict] = None,
) -> Optional[int]:
    """CAS ``source_status -> running``, open a run row, emit ``claimed``; None
    when the CAS lost. Caller holds the txn."""
    cur = conn.execute(
        f"""
        UPDATE tasks
           SET status        = 'running',
               claim_lock    = ?,
               claim_expires = ?,
               started_at    = COALESCE(started_at, ?)
         WHERE id = ?
           AND status = '{source_status}'
           AND claim_lock IS NULL
        """,
        (lock, expires, now, task_id),
    )
    if cur.rowcount != 1:
        return None
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    run_metadata = _recovery_metadata_for_claim(
        conn,
        task_id,
        trow["assignee"] if trow else None,
        trow["current_step_key"] if trow else None,
    )
    run_cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status,
            claim_lock, claim_expires, max_runtime_seconds,
            started_at, metadata
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
        """,
        (
            task_id, trow["assignee"] if trow else None, trow["current_step_key"] if trow else None,
            lock, expires, trow["max_runtime_seconds"] if trow else None, now,
            json.dumps(run_metadata) if run_metadata else None,
        ),
    )
    run_id = run_cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    _append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {})}, run_id=run_id,
    )
    return run_id


def claim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
    board: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    _board_slug = _normalize_board_slug(board) if board is not None else _board_slug_for_connection(conn)
    board_meta = product_board_metadata(_board_slug)
    release_measure_unblocks = _product_release_measure_unblocks_dependents(board_meta)
    with write_txn(conn):
        # FIRST statement in the transaction, deliberately. A card routed to
        # the privileged Resolver incompatibly must be refused before anything
        # else can touch it — the workflow-metadata repair preflight and the
        # parents demotion below both mutate, and neither should run for a card
        # that can never dispatch. No run row, no `claimed` event, no worker.
        # Blocked rather than retried: a retry cannot change the contract.
        routing_row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ? AND status = 'ready'",
            (task_id,),
        ).fetchone()
        if routing_row is not None:
            routing_error = resolver_routing_error(
                conn, task_id, routing_row["assignee"]
            )
            if routing_error is not None:
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', last_failure_error = ? "
                    "WHERE id = ? AND status = 'ready'",
                    (routing_error, task_id),
                )
                _apply_v2_flags(
                    conn, task_id, board_meta, running=False, blocked=True
                )
                _append_event(
                    conn, task_id, "claim_rejected",
                    {"reason": "resolver_routing", "error": routing_error},
                )
                return None
        candidate = conn.execute(
            "SELECT t.work_item_kind, t.workflow_template_id, t.current_step_key, "
            "       json_extract(w.canonical_json, '$.po_evidence.surface') "
            "           AS qualification_surface "
            "FROM tasks t LEFT JOIN work_contracts w ON w.id = t.work_contract_id "
            "WHERE t.id = ?", (task_id,)
        ).fetchone()
        if (
            candidate is not None
            and (
                candidate["work_item_kind"] == "epic"
                or _is_engine_owned_integration_state(candidate)
            )
        ):
            return None
        # Enforcement preflight: repair a plain/legacy role card missing its
        # product workflow metadata before it claims, so it dispatches on the
        # correct step instead of masquerading (re-applied from f55580879).
        _repair_product_workflow_metadata_if_needed(
            conn, task_id, board=_board_slug, actor="claim_preflight"
        )
        # Structural invariant: never transition ready -> running while any
        # parent is not dependency-satisfied. This is the single enforcement point
        # regardless of which writer (create_task, link_tasks, unblock_task,
        # release_stale_claims, manual SQL) set status='ready'. If a racy
        # writer promoted a task with undone parents, demote it back to
        # 'todo' here — recompute_ready will re-promote when the parents
        # actually finish. See RCA at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        parents = conn.execute(
            "SELECT p.status, p.workflow_template_id, p.current_step_key FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        architecture_assessment = (
            candidate is not None
            and candidate["workflow_template_id"] == "product"
            and candidate["current_step_key"] == "architecture"
            and candidate["qualification_surface"] == "work_inbox_intake"
        )
        if (
            not architecture_assessment
            and any(
                not _dependency_parent_satisfied(
                    p, release_measure_unblocks=release_measure_unblocks
                )
                for p in parents
            )
        ):
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'",
                (task_id,),
            )
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "parents_not_done"},
            )
            return None
        # Defensive: if a prior run somehow leaked (invariant violation from
        # an unknown code path), close it as 'reclaimed' so we don't strand
        # it when the CAS resets the pointer below. No-op when the invariant
        # holds (the common case).
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'ready'",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on re-claim'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'ready'
               AND work_item_kind = 'card'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        # v2 seam: every claim (ready-queue dispatch, _spawn_one_v2, any
        # future caller) maintains the canonical running flag here -- the
        # single point that used to be missed by the live gateway, which
        # calls claim_task directly rather than through _spawn_one_v2.
        # No-op on legacy (non-handoff_v2) boards.
        _apply_v2_flags(conn, task_id, board_meta, running=True, blocked=False)
        # Look up the current task row so we can populate the run with
        # its assignee / step / runtime cap.
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_metadata = _recovery_metadata_for_claim(
            conn, task_id, trow["assignee"], trow["current_step_key"]
        )
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at, metadata
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
                json.dumps(run_metadata) if run_metadata else None,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id},
            run_id=run_id,
        )
        claimed = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook(
        "kanban_task_claimed",
        task_id,
        board=get_current_board(),
        assignee=claimed.assignee if claimed else None,
        run_id=run_id,
    )
    return claimed



def claim_review_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``review -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``review`` status).

    Parent dependencies are re-checked because a previously completed parent
    may have been reopened while this task waited in review.

    Creates a new run entry so the review agent's lifecycle is tracked
    independently from the original worker run.
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        if not _parents_satisfied(conn, task_id):
            demoted = conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'review' AND claim_lock IS NULL",
                (task_id,),
            )
            if demoted.rowcount == 1:
                _append_event(
                    conn,
                    task_id,
                    "dependency_wait",
                    {
                        "reason": "parent_reopened",
                        "source_status": "review",
                    },
                )
            return None
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'review'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_metadata = _recovery_metadata_for_claim(
            conn, task_id, trow["assignee"], trow["current_step_key"]
        )
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at, metadata
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
                json.dumps(run_metadata) if run_metadata else None,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id,
             "source_status": "review"},
            run_id=run_id,
        )
        return get_task(conn, task_id)



def _retry_status_for_run(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int] = None,
) -> str:
    """``review`` when the run's ``claimed`` event says ``source_status=review``,
    else ``ready`` — one place, so crash/timeout/reclaim can't silently turn a
    reviewer run into an implementation run."""
    if run_id is None:
        run_id = _current_run_id(conn, task_id)
    if run_id is None:
        return "ready"
    event = _latest_event(conn, task_id, "claimed", run_id)
    payload = _json_dict(_row_get(event, "payload"))
    return "review" if payload.get("source_status") == "review" else "ready"


# Run outcome -> lifecycle status a goal loop should report for a handed-off run.
_RUN_OUTCOME_TERMINAL_STATUS = {
    "completed": "done",
    "review_requested": "review",
    "changes_requested": "changes_requested",
    "blocked": "blocked",
    "dependency_wait": "blocked",
}


def goal_run_status(
    conn: sqlite3.Connection, task_id: str, expected_run_id: Optional[int] = None,
) -> Optional[str]:
    """Lifecycle status as seen by ONE run: terminal handoffs bind to that run,
    any other ownership loss is ``superseded`` — otherwise an old goal loop
    would read the successor's live ``running`` and mutate it."""
    task = get_task(conn, task_id)
    if task is None:
        return None
    if expected_run_id is not None:
        row = conn.execute(
            "SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",
            (int(expected_run_id), task_id),
        ).fetchone()
        outcome = str(row["outcome"]) if row and row["outcome"] is not None else None
        terminal_status = _RUN_OUTCOME_TERMINAL_STATUS.get(outcome)
        if terminal_status is not None:
            return terminal_status
        if outcome is not None or task.current_run_id != int(expected_run_id):
            return "superseded"
    if task.status in {"ready", "todo"}:
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if event and event["kind"] == "changes_requested":
            return "changes_requested"
    return task.status


def heartbeat_claim(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim; True if we still own it."""
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?", (expires, task_id, lock),
        )
        if cur.rowcount != 1:
            return False
        _extend_run_claim(conn, task_id, expires)
        return True


def _extend_run_claim(conn: sqlite3.Connection, task_id: str, expires: int) -> Optional[int]:
    """Mirror a task claim extension onto its active run row; returns that run id."""
    run_id = _current_run_id(conn, task_id)
    if run_id is not None:
        conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (expires, run_id))
    return run_id


def release_stale_claims(conn: sqlite3.Connection, *, signal_fn=None) -> int:
    """Reclaim ``running`` tasks whose claim expired; returns the count reclaimed.

    A host-local worker that is still alive gets its claim *extended* instead
    (a slow model can sit longer than the TTL inside one tool-free call, so no
    heartbeat) — unless ``last_heartbeat_at`` is older than
    ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (wedged; ``_touch_activity``
    keeps any genuinely active worker fresh). Safe to call often.

    Reclaiming a live worker mid-flight produces the spawn- then-immediately-reclaim loop seen on slow
    models that spend longer than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM call (#23025):
    no tool calls means no ``kanban_heartbeat``, even though the subprocess is healthy.
    Backstop (#29747 gap 3): if the worker's PID is still alive but its ``last_heartbeat_at`` is stale by
    more than ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (1h), the worker has been making no observable
    progress and we reclaim anyway — even if ``_pid_alive`` is still true. This catches the
    wedged-in-a-logic-loop case where the process is technically running but accomplishing nothing.
    ``_touch_activity`` (run_agent.py) bridges chunk-level liveness into ``last_heartbeat_at`` via #31752,
    so any genuinely active worker keeps its heartbeat fresh as a side effect of normal API traffic.
    ``enforce_max_runtime`` and ``detect_crashed_workers`` remain the upper bounds for genuinely wedged or
    dead workers.
    """
    now = int(time.time())
    reclaimed = 0
    host_prefix = _host_prefix()
    stale = conn.execute(
        "SELECT id, claim_lock, worker_pid, claim_expires, last_heartbeat_at, "
        "       assignee "
        "FROM tasks "
        "WHERE status = 'running' AND claim_expires IS NOT NULL "
        "  AND claim_expires < ?", (now,),
    ).fetchall()
    for row in stale:
        host_local = (row["claim_lock"] or "").startswith(host_prefix)
        hb = row["last_heartbeat_at"]
        # Backstop: a heartbeat older than the max-stale threshold means no
        # observable progress — reclaim even if the PID is alive (logic loop).
        heartbeat_stale = hb is not None and (now - int(hb)) > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        if host_local and row["worker_pid"] and _pid_alive(row["worker_pid"]) and not heartbeat_stale:
            _extend_live_stale_claim(conn, row, now)
            continue

        termination = _terminate_reclaimed_worker(
            row["worker_pid"], row["claim_lock"], signal_fn=signal_fn,
        )
        # A live worker of ours must keep its claim (else a duplicate spawns beside it).
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, row["id"], row["claim_lock"], now, termination,
                reason="ttl_expired_worker_alive",
            )
            continue
        with write_txn(conn):
            retry_status = _retry_status_for_run(conn, row["id"])
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                # v2 state-model integrity (R3): worker_pid is cleared here --
                # the worker is gone -- so the canonical ``running`` flag must
                # clear with it, and a card re-idled to ``ready`` isn't
                # ``blocked``. No-op on legacy boards: these columns are
                # never set true there.
                "running = 0, blocked = 0 "
                "WHERE id = ? AND status = 'running' AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ?",
                (retry_status, row["id"], row["claim_lock"], now),
            )
            if cur.rowcount != 1:
                continue
            run_id = _record_reclaim(
                conn, row["id"], termination,
                error=f"stale_lock={row['claim_lock']}",
                payload={
                    "stale_lock": row["claim_lock"],
                    "worker_pid": _opt_int(row["worker_pid"]),
                    "claim_expires": int(row["claim_expires"]),
                    "last_heartbeat_at": _opt_int(row["last_heartbeat_at"]),
                    "now": now,
                    "host_local": host_local,
                    "heartbeat_stale": bool(heartbeat_stale),
                    "retry_status": retry_status,
                },
            )
            reclaimed += 1
        # Post-commit observer; every non-reclaim branch ``continue``d above.
        if _kanban_observer_consumed("on_kanban_worker_stale_claim"):
            _fire_kanban_lifecycle_hook(
                "on_kanban_worker_stale_claim", row["id"], board=get_current_board(),
                assignee=row["assignee"], run_id=run_id, worker_pid=_opt_int(row["worker_pid"]),
                heartbeat_stale=bool(heartbeat_stale), retry_status=retry_status,
            )
    return reclaimed


def _record_reclaim(
    conn: sqlite3.Connection, task_id: str, termination: dict, *, error: str, payload: dict,
) -> Optional[int]:
    """Close the active run as ``reclaimed`` and emit the ``reclaimed`` event
    (payload merged with the termination report). Caller holds the txn."""
    run_id = _end_run(
        conn, task_id, outcome="reclaimed", status="reclaimed", error=error, metadata=termination,
    )
    payload.update(termination)
    _append_event(conn, task_id, "reclaimed", payload, run_id=run_id)
    return run_id


def _extend_live_stale_claim(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> None:
    """TTL-expired claim whose host-local worker is alive: extend instead of
    reclaiming (``claim_extended`` event). CAS on the same expired lock so a
    concurrent reclaimer wins cleanly."""
    new_expires = now + _resolve_claim_ttl_seconds()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' "
            "  AND claim_lock IS ? "
            "  AND claim_expires IS NOT NULL "
            "  AND claim_expires < ?", (new_expires, row["id"], row["claim_lock"], now),
        )
        if cur.rowcount != 1:
            return
        run_id = _extend_run_claim(conn, row["id"], new_expires)
        _append_event(
            conn, row["id"], "claim_extended",
            {
                "reason": "pid_alive",
                "worker_pid": int(row["worker_pid"]),
                "claim_lock": row["claim_lock"],
                "claim_expires_was": int(row["claim_expires"]),
                "claim_expires_now": new_expires,
                "last_heartbeat_at": _opt_int(row["last_heartbeat_at"]),
            },
            run_id=run_id,
        )


def reclaim_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None, signal_fn=None,
) -> bool:
    """Operator reclaim regardless of TTL: release the claim, restore the source
    phase, reset the failure counter. False when not running."""
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    termination = _terminate_reclaimed_worker(row["worker_pid"], prev_lock, signal_fn=signal_fn)
    with write_txn(conn):
        retry_status = _retry_status_for_run(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, "
            # v2 state-model integrity (R3): the worker is gone and the card
            # lands idle in ``ready`` -- clear both canonical flags. No-op on
            # legacy boards (columns never set true there).
            "running = 0, blocked = 0 "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ?", (retry_status, task_id, prev_lock),
        )
        if cur.rowcount != 1:
            return False
        _record_reclaim(
            conn, task_id, termination,
            error=f"manual_reclaim: {reason}" if reason else f"manual_reclaim lock={prev_lock}",
            payload={"manual": True, "reason": reason, "prev_lock": prev_lock, "retry_status": retry_status},
        )
    # Operator intervention = fresh retry budget (own txn, runs after commit).
    _clear_failure_counter(conn, task_id)
    return True


def reassign_task(
    conn: sqlite3.Connection, task_id: str, profile: Optional[str], *, reclaim_first: bool = False,
    reason: Optional[str] = None,
) -> bool:
    """Reassign (None unassigns); a running task is refused unless
    ``reclaim_first`` releases its claim — the "this profile's model is broken" path."""
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        reclaim_task(conn, task_id, reason=reason or "reassign")
    # assign_task handles its own txn + the still-running guard.
    try:
        return assign_task(conn, task_id, profile)
    except RuntimeError:
        # Task is still running and reclaim_first was False; caller
        # needs to decide whether to retry with reclaim.
        return False


def _verify_created_cards(
    conn: sqlite3.Connection, completing_task_id: str, claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom). Verified = the row
    exists AND ``created_by`` is the completing task's assignee or id, OR the
    card is linked as its child (created elsewhere, attached by the worker).
    Never mutates."""
    ordered = list(dict.fromkeys(str(x).strip() for x in (claimed_ids or []) if str(x).strip()))
    if not ordered:
        return [], []

    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,)).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})", tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        trusted = created_by is not None and (
            (completing_assignee and created_by == completing_assignee)
            or created_by == completing_task_id
            or cid in linked_children
        )
        (verified if trusted else phantom).append(cid)
    return verified, phantom


# Matches ``kanban_create`` (12 hex) and ``_new_task_id`` (8 hex) ids; 8+ for forward compat.
_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")


def _scan_prose_for_phantom_ids(conn: sqlite3.Connection, text: str) -> list[str]:
    """``t_<hex>`` references in ``text`` that don't resolve to a task (deduped; advisory)."""
    if not text:
        return []
    return _missing_task_ids(conn, dict.fromkeys(_TASK_ID_PROSE_RE.findall(text)))


class HallucinatedCardsError(ValueError):
    """``complete_task`` refused: ``created_cards`` has ids that don't exist or
    weren't created by this worker (``.phantom``). A ``ValueError`` so tool
    error handlers treat it as recoverable."""

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


class ProductProvenanceError(ValueError):
    """Raised when product-board completion lacks required AI provenance."""

    def __init__(
        self,
        reason: str,
        task_id: str,
        step_key: str,
        *,
        missing: Optional[list[str]] = None,
    ):
        self.task_id = task_id
        self.step_key = step_key
        self.missing = list(missing or [])
        super().__init__(reason)


class ReleaseEvidenceError(ValueError):
    """Raised when a product task lacks evidence required to reach Done."""

    def __init__(self, task_id: str, missing: list[str]):
        self.task_id = task_id
        self.missing = list(dict.fromkeys(missing))
        super().__init__(
            f"release blocked for {task_id}; missing evidence: "
            f"{', '.join(self.missing)}"
        )


_RELEASE_SUCCESS_WORDS = frozenset(
    {"green", "healthy", "ok", "pass", "passed", "success", "succeeded"}
)


def _release_evidence_succeeded(value: Any) -> bool:
    """Return true only for an explicit positive release-evidence result."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _RELEASE_SUCCESS_WORDS
    if not isinstance(value, dict):
        return False
    results = [
        _release_evidence_succeeded(value[key])
        for key in ("success", "passed", "healthy", "status", "result", "health")
        if key in value
    ]
    return bool(results) and all(results)


def _validate_product_workflow_outcome(
    outcome: Any,
    current_step: str,
) -> tuple[str, Optional[str], list[str]]:
    try:
        validated = validate_terminal_outcome(
            task_id="<direct>",
            run_id=0,
            phase=current_step,
            summary=None,
            result=None,
            metadata={"workflow_outcome": outcome},
        )
    except OutcomeValidationError as exc:
        raise ValueError(
            f"invalid workflow_outcome for {current_step}: {exc.code}"
        ) from exc
    return validated.verdict, validated.target_step, list(validated.findings)


def _record_product_outcome_rejection(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: Optional[int],
    phase: str,
    error: OutcomeValidationError,
) -> None:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "phase": phase,
        "code": error.code,
    }
    if error.qualifier is not None:
        payload["qualifier"] = error.qualifier
    with write_txn(conn):
        _append_event(
            conn,
            task_id,
            "completion_rejected_outcome",
            payload,
            run_id=run_id,
        )


def _record_product_outcome_observations(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: Optional[int],
    phase: str,
    outcome: Optional[TerminalOutcome],
) -> None:
    if outcome is None or "serialized_parameter_leak" not in outcome.observations:
        return
    with write_txn(conn):
        _append_event(
            conn,
            task_id,
            "serialized_parameter_leak",
            {"run_id": run_id, "phase": phase},
            run_id=run_id,
        )


def _validate_ordinary_product_outcome(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str],
    result: Optional[str],
    metadata: Optional[dict],
    expected_run_id: Optional[int],
) -> tuple[Optional[TerminalOutcome], Optional[str], Optional[int]]:
    """Validate an active Test/Review envelope before any completion write."""
    row = conn.execute(
        "SELECT current_step_key, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None, None, None
    phase = str(row["current_step_key"] or "").strip()
    _reject_worker_recovery_metadata(conn, task_id, phase, metadata)
    if phase not in PRODUCT_POSITIVE_OUTCOME_STEPS.values():
        return None, None, None
    run_id = (
        int(row["current_run_id"])
        if row["current_run_id"] is not None
        else (int(expected_run_id) if expected_run_id is not None else None)
    )
    try:
        outcome = validate_terminal_outcome(
            task_id=task_id,
            run_id=run_id or 0,
            phase=phase,
            summary=summary,
            result=result,
            metadata=metadata,
        )
    except OutcomeValidationError as exc:
        _record_product_outcome_rejection(
            conn,
            task_id,
            run_id=run_id,
            phase=phase,
            error=exc,
        )
        raise ProductOutcomeError(
            task_id,
            run_id or 0,
            phase,
            exc.code,
            exc.qualifier,
        ) from exc
    return outcome, phase, run_id


def _route_product_rework_if_requested(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str],
    metadata: Optional[dict],
    summary: Optional[str],
    expected_run_id: Optional[int],
    product_role_assignees: Optional[dict[str, str]],
    validated_outcome: Optional[TerminalOutcome] = None,
) -> Optional[bool]:
    outcome = metadata.get("workflow_outcome") if isinstance(metadata, dict) else None
    if outcome is None:
        return None
    meta = product_board_metadata(board) or {}
    policy = meta.get("product_workflow") if isinstance(meta, dict) else {}
    try:
        max_cycles = int((policy or {}).get("max_rework_cycles", 3))
    except (TypeError, ValueError):
        max_cycles = 3
    max_cycles = max(1, max_cycles)
    with authorized_governance_write(), write_txn(conn):
        row = conn.execute(
            "SELECT title, assignee, status, current_step_key, "
            "current_run_id, rework_count, branch_name FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        observed_step = row["current_step_key"]
        current_step = str(observed_step or "").strip()
        if not current_step:
            current_step = str(
                _infer_product_step(
                    title=row["title"] or "",
                    assignee=row["assignee"],
                    explicit_step=None,
                    product_intent=True,
                )
                or ""
            )
        if validated_outcome is None:
            verdict, target_step, findings = _validate_product_workflow_outcome(
                outcome, current_step
            )
        else:
            verdict = validated_outcome.verdict
            target_step = validated_outcome.target_step
            findings = list(validated_outcome.findings)
        if verdict in {"passed", "approved"}:
            return None
        if expected_run_id is None:
            raise ValueError("expected_run_id is required for structured rework")
        if (
            row["status"] != "running"
            or row["current_run_id"] != int(expected_run_id)
        ):
            return False

        run_metadata: dict = {}
        if row["current_run_id"] is not None:
            run_row = conn.execute(
                "SELECT metadata FROM task_runs WHERE id = ?",
                (int(row["current_run_id"]),),
            ).fetchone()
            if run_row is not None and run_row["metadata"]:
                try:
                    parsed_run_metadata = json.loads(run_row["metadata"])
                except (TypeError, ValueError):
                    parsed_run_metadata = None
                if isinstance(parsed_run_metadata, dict):
                    run_metadata = parsed_run_metadata

        def _directive_value(*keys: str) -> Optional[str]:
            for source in (run_metadata, metadata if isinstance(metadata, dict) else {}):
                for key in keys:
                    value = source.get(key)
                    if value is not None and str(value).strip():
                        return str(value).strip()
            return None

        rejected_branch = _directive_value(
            f"{current_step}_branch", "rejected_branch", "branch"
        ) or row["branch_name"]
        rejected_sha = _directive_value(
            f"{current_step}_head_sha",
            "rejected_sha",
            "head_sha",
            "source_sha",
        )
        epic_tip_sha = _directive_value("epic_tip_sha", "epic_head_sha")
        origin_intent_key = _directive_value(
            "origin_intent_key", "integration_intent_key", "intent_key"
        )
        origin_kind = (
            current_step
            if current_step in _REWORK_DIRECTIVE_ORIGIN_KINDS
            else "refresh"
        )

        observed_count = int(row["rework_count"] or 0)
        next_count = observed_count + 1
        limit_reached = next_count > max_cycles
        if limit_reached:
            next_status = "blocked"
            next_assignee = "default"
        else:
            next_status = _column_status_for_step(meta, target_step)
            role = "developer" if target_step == "development" else "architect"
            next_assignee = _product_role_assignee(
                meta, role, product_role_assignees
            )
        sql = (
            "UPDATE tasks SET rework_count = ?, current_step_key = ?, status = ?, "
            "assignee = ?, running = 0, blocked = ?, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, workflow_template_id = 'product' "
            "WHERE id = ? AND status = ? AND current_step_key IS ? "
            "AND rework_count = ? AND current_run_id = ?"
        )
        params: tuple[Any, ...] = (
            next_count,
            target_step,
            next_status,
            next_assignee,
            1 if limit_reached else 0,
            task_id,
            row["status"],
            observed_step,
            observed_count,
            int(expected_run_id),
        )
        updated = conn.execute(sql, params)
        if updated.rowcount != 1:
            return False
        run_id = _end_run(
            conn,
            task_id,
            outcome="rework_requested",
            status="blocked" if limit_reached else "completed",
            summary=summary,
            metadata=metadata,
            expected_run_id=expected_run_id,
        )
        if expected_run_id is not None and run_id is None:
            raise RuntimeError("rework run ownership changed")
        directive = create_rework_directive(
            conn,
            task_id,
            origin_kind=origin_kind,
            origin_run_id=run_id,
            origin_intent_key=origin_intent_key,
            origin_phase=current_step,
            target_phase=str(target_step),
            rejected_branch=rejected_branch,
            rejected_sha=rejected_sha,
            epic_tip_sha=epic_tip_sha,
            findings=findings,
        )
        _append_event(
            conn,
            task_id,
            "rework_limit_reached" if limit_reached else "rework_requested",
            {
                "from_step": current_step,
                "target_step": target_step,
                "verdict": verdict,
                "findings": findings,
                "rework_count": next_count,
                "max_rework_cycles": max_cycles,
                "directive_id": directive.id,
            },
            run_id=run_id,
        )
        if (
            validated_outcome is not None
            and "serialized_parameter_leak" in validated_outcome.observations
        ):
            _append_event(
                conn,
                task_id,
                "serialized_parameter_leak",
                {"run_id": run_id, "phase": current_step},
                run_id=run_id,
            )
        if limit_reached:
            _append_event(
                conn,
                task_id,
                "blocked",
                {
                    "reason": "maximum product rework cycles exceeded",
                    "kind": "rework_limit",
                    "findings": findings,
                    "rework_count": next_count,
                    "max_rework_cycles": max_cycles,
                },
                run_id=run_id,
            )
    return True



class ArtifactPreservationError(RuntimeError):
    """Raised when a declared scratch deliverable cannot be preserved."""


class _SourceCommitError(RuntimeError):
    """Typed failure raised when commit-first completion cannot be proven."""

    def __init__(self, code: str, detail: Optional[str] = None):
        self.code = code
        self.detail = detail
        message = f"source completion failed: {code}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None,
    expected_run_id: Optional[int] = None,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
    product_role_assignees: Optional[dict[str, str]] = None,
    product_workflow_enabled: bool = True,
    fire_lifecycle_hook: bool = True,
    _release_evidence: Optional[dict] = None,
) -> bool:
    """Transition ``running|ready|blocked|review -> done`` and record ``result``.

    ``review`` is accepted so a human (or reviewer) can approve a task parked
    in the review lane by :func:`request_review` — even when it has no active
    run (``current_run_id IS NULL``), the handoff fields are preserved via
    :func:`_synthesize_ended_run`.

    Accepts a task that is merely ``ready`` too, so a manual CLI
    completion (``hermes kanban complete <id>``) works without requiring
    a claim/start/complete sequence.

    ``summary`` and ``metadata`` are stored on the closing run (if any)
    and surfaced to downstream children via :func:`build_worker_context`.
    When ``summary`` is omitted we fall back to ``result`` so single-run
    callers do not have to pass both. ``metadata`` is a free-form dict
    (e.g. ``{"changed_files": [...], "tests_run": [...]}``) — workers
    are encouraged to use it for structured handoff facts.

    ``created_cards`` is an optional list of task ids the completing
    worker claims to have created. Each id is verified against
    ``tasks.created_by``. If any id is phantom (does not exist or was
    not created by this worker's assignee profile), completion is blocked
    with a ``HallucinatedCardsError`` and a
    ``completion_blocked_hallucination`` event is emitted so the rejected
    attempt is auditable. When all ids verify, they are recorded on the
    ``completed`` event payload.

    After a successful completion, ``summary`` and ``result`` are scanned
    for prose references like ``t_deadbeefcafe`` that do not resolve.
    Any suspected phantom references are recorded as a
    ``suspected_hallucinated_references`` event. This pass is advisory
    and never blocks.

    ``board_meta`` is an already-read board metadata snapshot. Release
    orchestration passes the one snapshot it validated policy against, so a
    board edit mid-operation cannot make a later step disagree with the gate
    that admitted it. ``None`` reads fresh, exactly as before.
    """
    board = board or _board_slug_for_connection(conn)
    now = int(time.time())
    lifecycle_scope = conn.execute(
        "SELECT workflow_template_id, current_step_key FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if _is_engine_owned_integration_state(lifecycle_scope):
        return False
    validated_terminal_outcome: Optional[TerminalOutcome] = None
    validated_outcome_phase: Optional[str] = None
    validated_outcome_run_id: Optional[int] = None
    if product_workflow_enabled:
        outcome_meta = (
            board_meta if board_meta is not None else product_board_metadata(board)
        )
        if outcome_meta is not None and _handoff_v2_enabled(outcome_meta):
            (
                validated_terminal_outcome,
                validated_outcome_phase,
                validated_outcome_run_id,
            ) = _validate_ordinary_product_outcome(
                conn,
                task_id,
                summary=summary,
                result=result,
                metadata=metadata,
                expected_run_id=expected_run_id,
            )

    task_kind_row = conn.execute(
        "SELECT work_item_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if (
        task_kind_row is not None
        and task_kind_row["work_item_kind"] == "epic"
        and _release_evidence is None
    ):
        with write_txn(conn):
            _append_event(
                conn,
                task_id,
                "completion_blocked_epic_release",
                {"reason": "Epic completion requires the release contract"},
        )
        return False

    # Gate: verify created_cards BEFORE the main write txn. A rejected
    # completion still needs an auditable event, so we emit it in a
    # tiny dedicated txn, then raise. The caller is responsible for
    # surfacing HallucinatedCardsError to the worker; this function
    # never mutates task state on a phantom-card rejection.
    if created_cards:
        verified_cards, phantom_cards = _verify_created_cards(
            conn, task_id, created_cards
        )
        if phantom_cards:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "completion_blocked_hallucination",
                    {
                        "phantom_cards": phantom_cards,
                        "verified_cards": verified_cards,
                        "summary_preview": (
                            (summary or result or "").strip().splitlines()[0][:200]
                            if (summary or result)
                            else None
                        ),
                    },
                )
            raise HallucinatedCardsError(phantom_cards, task_id)
    else:
        verified_cards = []

    if product_workflow_enabled:
        meta = board_meta if board_meta is not None else product_board_metadata(board)
        if meta is not None and _handoff_v2_enabled(meta):
            validated_positive_phase: Optional[str] = None
            _validate_stored_product_workflow_state(conn, task_id)
            if _latest_unresolved_product_preflight(conn, task_id):
                resolver_result = _complete_product_workflow_step(
                    conn,
                    task_id,
                    board=board,
                    board_meta=board_meta,
                    result=result,
                    summary=summary,
                    metadata=metadata,
                    expected_run_id=expected_run_id,
                    product_role_assignees=product_role_assignees,
                )
                if resolver_result is not None:
                    return resolver_result
            rework_routed = _route_product_rework_if_requested(
                conn,
                task_id,
                board=board,
                metadata=metadata,
                summary=summary if summary is not None else result,
                expected_run_id=expected_run_id,
                product_role_assignees=product_role_assignees,
                validated_outcome=validated_terminal_outcome,
            )
            if rework_routed is not None:
                return rework_routed
            workflow_outcome = (
                metadata.get("workflow_outcome")
                if isinstance(metadata, dict)
                else None
            )
            if isinstance(workflow_outcome, dict):
                validated_positive_phase = PRODUCT_POSITIVE_OUTCOME_STEPS.get(
                    workflow_outcome.get("verdict")
                )
            # Completion preflight: repair a plain/legacy role card's missing
            # product metadata before the transition is evaluated, so handoff_v2
            # advances the correct step (re-applied from f55580879). Repair only
            # -- advancement stays with handoff() below.
            with write_txn(conn):
                _repair_product_workflow_metadata_if_needed(
                    conn, task_id, board=board, actor="completion_preflight"
                )
            row = conn.execute(
                "SELECT current_step_key FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            step_key = row["current_step_key"] if row is not None else None
            transition = PRODUCT_WORKFLOW_TRANSITIONS.get(str(step_key or ""))
            has_unresolved_preflight = bool(
                _latest_unresolved_product_preflight(conn, task_id)
            )
            if (
                transition is not None
                and transition.get("next_step")
                and not has_unresolved_preflight
            ):
                # Non-terminal v2 step, and no obstacle-resolution preflight
                # pending: route through the atomic commit-first handoff()
                # (Phase 2) instead of the legacy advance below. When a
                # preflight IS pending, obstacle-resolution (not real work)
                # just completed -- fall through to the legacy path below,
                # which resumes the card to its original assignee/step
                # (mirrors the ordering in `_complete_product_workflow_step`
                # at line ~1533: preflight check before consulting the
                # transition table).
                advanced = handoff(
                    conn, task_id, board=board, summary=summary, metadata=metadata,
                    expected_run_id=expected_run_id,
                    expected_phase=validated_positive_phase,
                )
                if not advanced:
                    # Required source-commit gate failed (for source-producing
                    # steps), or provenance/board state changed underneath us
                    # -- do NOT fall through to the terminal-done UPDATE,
                    # which would wrongly mark an uncommitted card done.
                    return False
                _record_product_outcome_observations(
                    conn,
                    task_id,
                    run_id=validated_outcome_run_id,
                    phase=validated_outcome_phase or str(step_key or ""),
                    outcome=validated_terminal_outcome,
                )
                return True
            # Terminal / non-advancing v2 step (e.g. release_measure, or no
            # transition at all): fall through to the existing legacy path
            # unchanged -- release_measure -> done stays the human gate.

        product_transition = _complete_product_workflow_step(
            conn,
            task_id,
            board=board,
            board_meta=board_meta,
            result=result,
            summary=summary,
            metadata=metadata,
            expected_run_id=expected_run_id,
            product_role_assignees=product_role_assignees,
        )
        if product_transition is not None:
            return product_transition
        if (
            lifecycle_scope is not None
            and lifecycle_scope["workflow_template_id"]
            == PRODUCT_WORKFLOW_TEMPLATE_ID
            and meta is None
        ):
            connection_board = _known_board_slug_for_connection(conn)
            database_rows = conn.execute("PRAGMA database_list").fetchall()
            main_database = next(
                (row for row in database_rows if row["name"] == "main"),
                None,
            )
            database_path = (
                str(main_database["file"] or "")
                if main_database is not None
                else ""
            )
            reason = "product board metadata is unavailable"
            with write_txn(conn):
                _append_event(
                    conn,
                    task_id,
                    "completion_blocked_product_board_resolution",
                    {
                        "board": board,
                        "connection_board": connection_board,
                        "database_path": database_path,
                    },
                )
            raise ProductWorkflowStateError(
                task_id,
                lifecycle_scope["current_step_key"],
                reason,
            )

    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    source_policy = conn.execute(
        "SELECT source_commit_required, source_commit_forbidden, current_run_id, "
        "workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    source_commit_required = bool(
        source_policy is not None and source_policy["source_commit_required"]
    )
    source_commit_forbidden = bool(
        source_policy is not None and source_policy["source_commit_forbidden"]
    )
    if source_commit_forbidden and source_policy is not None and source_policy["workspace_path"]:
        repo_root = _git_toplevel(Path(str(source_policy["workspace_path"])))
        if repo_root is not None:
            status = subprocess.run(
                ["git", "-C", str(repo_root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if status.returncode == 0 and status.stdout:
                raise _SourceCommitError("source_forbidden_dirty")
            if status.returncode == 0:
                candidate = subprocess.run(
                    ["git", "-C", str(repo_root), "rev-parse", "HEAD^{commit}"],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                candidate_sha = (candidate.stdout or "").strip()
                if candidate.returncode == 0 and len(candidate_sha) == 40:
                    metadata = dict(metadata or {})
                    metadata["candidate_sha"] = candidate_sha
    if source_commit_required:
        source_run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else (
                int(source_policy["current_run_id"])
                if source_policy is not None
                and source_policy["current_run_id"] is not None
                else None
            )
        )
        if source_run_id is None:
            raise _SourceCommitError("missing_run")
        _commit_worker_diff(
            conn,
            task_id,
            message=f"complete: {task_id}",
            expected_run_id=source_run_id,
        )
        expected_run_id = source_run_id
        source_run = get_run(conn, source_run_id)
        if source_run is None or not isinstance(source_run.metadata, dict):
            raise _SourceCommitError("missing_receipt")
        source_metadata = {
            key: source_run.metadata[key]
            for key in ("source_completion_intent", "source_completion_receipt")
            if key in source_run.metadata
        }
        if "source_completion_receipt" not in source_metadata:
            raise _SourceCommitError("missing_receipt")
        metadata = dict(metadata or {})
        metadata.update(source_metadata)
    with authorized_governance_write(), write_txn(conn):
        # Pre-transition status: a task approved straight out of the review
        # lane has no active run, so the handoff must be synthesized below.
        _prior = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        prior_status = _prior["status"] if _prior else None
        if source_commit_required:
            owned = conn.execute(
                "SELECT r.metadata FROM tasks t JOIN task_runs r "
                "ON r.id = t.current_run_id "
                "WHERE t.id = ? AND t.current_run_id = ? AND r.ended_at IS NULL",
                (task_id, int(expected_run_id)),
            ).fetchone()
            if owned is None:
                raise _SourceCommitError("run_changed")
            try:
                owned_metadata = json.loads(owned["metadata"] or "{}")
            except (TypeError, ValueError) as exc:
                raise _SourceCommitError("missing_receipt") from exc
            receipt = owned_metadata.get("source_completion_receipt")
            if not isinstance(receipt, dict) or receipt.get("run_id") != int(
                expected_run_id
            ):
                raise _SourceCommitError("missing_receipt")
        terminal_row = conn.execute(
            "SELECT current_step_key FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        terminal_meta = board_meta if board_meta is not None else product_board_metadata(board)
        if (
            terminal_row is not None
            and terminal_row["current_step_key"] == "release_measure"
            and terminal_meta is not None
            and _handoff_v2_enabled(terminal_meta)
        ):
            _validate_done_evidence(conn, task_id, _release_evidence or {})
        release_status = ", 'todo'" if _release_evidence is not None else ""
        if expected_run_id is None:
            cur = conn.execute(
                f"""
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0,
                       running      = 0,
                       blocked      = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'review'{release_status})
                """,
                (result, now, task_id),
            )
        else:
            cur = conn.execute(
                f"""
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0,
                       running      = 0,
                       blocked      = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'review'{release_status})
                   AND current_run_id = ?
                """,
                (result, now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        # v2 state-model integrity (R3): terminal ``done`` means phase=done
        # too -- set current_step_key='done' so a v2 card's phase agrees with
        # its status (running/blocked above are unconditional no-ops on
        # legacy since those columns are never true there, but
        # current_step_key is a general step-tracking field also used by
        # non-v2 workflow_template boards, so it must stay v2-gated to avoid
        # corrupting their step semantics).
        term_meta = board_meta if board_meta is not None else product_board_metadata(board)
        if term_meta is not None and _handoff_v2_enabled(term_meta):
            conn.execute(
                "UPDATE tasks SET current_step_key = 'done' WHERE id = ?",
                (task_id,),
            )
        if isinstance(metadata, dict):
            _persist_scratch_completion_artifacts(conn, task_id, metadata)
            for stored_path in metadata.pop("_staged_artifacts", []):
                path = Path(stored_path)
                _insert_completion_attachment(
                    conn,
                    task_id,
                    filename=path.name,
                    stored_path=str(path),
                    size=path.stat().st_size,
                    created_at=now,
                )
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="done",
            summary=summary if summary is not None else result,
            metadata=metadata,
            expected_run_id=expected_run_id,
        )
        # If complete_task was called on a never-claimed task (ready or
        # blocked → done with no run in flight), synthesize a
        # zero-duration run so the handoff fields are persisted in
        # attempt history instead of silently lost.
        if run_id is None and (
            summary or metadata or result or prior_status == "review"
        ):
            synth_summary = summary if summary is not None else result
            synth_metadata = metadata
            if prior_status == "review" and not synth_summary and not synth_metadata:
                synth_summary = "Review approved without additional evidence."
                synth_metadata = {
                    "source_status": "review",
                    "approval": "manual",
                }
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=synth_summary,
                metadata=synth_metadata,
            )
        # Carry the handoff summary in the event payload so gateway
        # notifiers and dashboard WS consumers can render it without a
        # second SQL round-trip. First line only, 400 char cap — the
        # full summary stays on the run row.
        event_summary = summary if summary is not None else result
        if prior_status == "review" and not event_summary:
            event_summary = "Review approved without additional evidence."
        _ev_lines = (event_summary or "").strip().splitlines()
        ev_summary = _ev_lines[0][:400] if _ev_lines else ""
        completed_payload: dict = {
            "result_len": len(result) if result else 0,
            "summary": ev_summary or None,
        }
        if _release_evidence is not None:
            completed_payload["release_evidence"] = dict(_release_evidence)
        if verified_cards:
            completed_payload["verified_cards"] = verified_cards
        # Carry artifact paths in the event payload so the gateway
        # notifier can upload them as native attachments alongside the
        # completion message. Workers pass these via
        # ``kanban_complete(artifacts=[...])`` which stashes the list in
        # ``metadata["artifacts"]`` — we promote it onto the event so
        # consumers don't have to fetch the run row to find it.
        if isinstance(metadata, dict):
            md_artifacts = metadata.get("artifacts")
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [
                    str(p).strip() for p in md_artifacts if isinstance(p, str) and str(p).strip()
                ]
                if cleaned_artifacts:
                    completed_payload["artifacts"] = cleaned_artifacts
        _append_event(
            conn, task_id, "completed",
            completed_payload,
            run_id=run_id,
        )
    # Prose-scan the summary + result for t_<hex> references that do
    # not resolve. Advisory — does not block the completion. Runs in
    # its own txn so the completion itself is already durable by the
    # time we emit the warning.
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        # Drop any phantom refs that were already flagged as verified
        # above (shouldn't happen — verified means they exist — but
        # belt-and-suspenders).
        phantom_refs = [p for p in phantom_refs if p not in set(verified_cards)]
        if phantom_refs:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "suspected_hallucinated_references",
                    {
                        "phantom_refs": phantom_refs,
                        "source": "completion_summary",
                    },
                    run_id=run_id,
                )
    # Successful completion — wipe the consecutive-failures counter.
    # Failure history stays on the event log for audit; the counter
    # just tracks "is there a current pathology the breaker should
    # care about", and a success resets that question.
    _clear_failure_counter(conn, task_id)
    # Recompute ready status for dependents (separate txn so children see done).
    recompute_ready(conn)
    # Clean up the scratch workspace and any stale tmux session for the worker.
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    if fire_lifecycle_hook:
        _fire_kanban_lifecycle_hook(
            "kanban_task_completed",
            task_id,
            board=get_current_board(),
            assignee=_done_task.assignee if _done_task else None,
            run_id=run_id,
            summary=(summary if summary is not None else result),
        )
    return True



def clear_terminal_state(
    conn: sqlite3.Connection,
    request: ClearTerminalStateRequest,
) -> bool:
    """Clear a stale generic terminal flag without rewriting workflow history.

    The operator must present the exact terminal snapshot observed before the
    repair. Only ``status`` and ``completed_at`` are changed; the stored phase,
    assignee, evidence, runs, and prior events remain untouched. A successful
    repair appends one auditable event carrying the expected snapshot.
    """
    if not isinstance(request, ClearTerminalStateRequest):
        raise TypeError("request must be ClearTerminalStateRequest")

    task_id = str(request.task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    for field in ("expected_completed_at", "expected_latest_event_id"):
        value = getattr(request, field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} is required")
    expected_phase = str(request.expected_phase or "").strip()
    if not expected_phase:
        raise ValueError("expected_phase is required")
    actor = str(request.actor or "").strip()
    if not actor:
        raise ValueError("actor is required")
    reason = str(request.reason or "").strip()
    if not reason:
        raise ValueError("reason is required")

    expected_snapshot = {
        "status": "done",
        "completed_at": request.expected_completed_at,
        "phase": expected_phase,
        "latest_event_id": request.expected_latest_event_id,
    }
    board = _board_slug_for_connection(conn)
    board_meta = product_board_metadata(board)
    with authorized_governance_write(), write_txn(conn):
        row = conn.execute(
            "SELECT status, completed_at, current_step_key FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["status"] != "done":
            return False
        if (
            row["completed_at"] != request.expected_completed_at
            or row["current_step_key"] != expected_phase
        ):
            return False
        if row["current_step_key"] == "done":
            return False

        latest_event = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if (
            latest_event is None
            or int(latest_event["id"]) != request.expected_latest_event_id
        ):
            return False

        restored_status = _column_status_for_step(board_meta, expected_phase)
        if restored_status in {"done", "archived"}:
            return False
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?, completed_at = NULL
             WHERE id = ?
               AND status = 'done'
               AND completed_at = ?
               AND current_step_key = ?
               AND (
                   SELECT id FROM task_events
                    WHERE task_id = ?
                    ORDER BY id DESC
                    LIMIT 1
               ) = ?
            """,
            (
                restored_status,
                task_id,
                request.expected_completed_at,
                expected_phase,
                task_id,
                request.expected_latest_event_id,
            ),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn,
            task_id,
            "terminal_state_cleared",
            {
                "operation": "clear_terminal_state",
                "actor": actor,
                "reason": reason,
                "expected": expected_snapshot,
            },
        )
    return True


# ---------------------------------------------------------------------------
# Workspace / tmux cleanup
# ---------------------------------------------------------------------------


def _merge_completion_prose_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: Optional[dict], *, summary: Optional[str],
    result: Optional[str],
) -> Optional[dict]:
    """Legacy workers named deliverables only by absolute path in prose; add
    those that exist under the scratch workspace to ``metadata["artifacts"]``
    before cleanup can erase them."""
    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return metadata
    if not _is_managed_scratch_path(workspace):
        return metadata
    text = "\n".join(part for part in (summary, result) if part)
    if not text:
        return metadata
    prefix = re.escape(str(workspace))
    discovered: list[str] = []
    for match in re.finditer(prefix + r"(?:[/\\][^\s`\"'<>]+)", text):
        raw = match.group(0).rstrip(".,;:!?)]}")
        candidate = Path(raw)
        if candidate.is_file():
            discovered.append(str(candidate))
    if not discovered:
        return metadata
    updated = dict(metadata) if isinstance(metadata, dict) else {}
    existing = updated.get("artifacts")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = {str(path) for path in merged}
    for path in discovered:
        if path not in seen:
            merged.append(path)
            seen.add(path)
    updated["artifacts"] = merged
    return updated


def _persist_scratch_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict,
) -> None:
    """Copy scratch-workspace completion artifacts before cleanup removes them."""
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return

    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return
    is_managed, board = _managed_scratch_path_info(workspace)
    if not is_managed:
        return

    try:
        workspace_root = workspace.resolve()
    except OSError:
        return

    attachment_dir = task_attachments_dir(task_id, board=board)
    persisted: list[str] = []
    used_destinations: set[Path] = set()
    changed = False

    def _discard_copies() -> None:
        for copied in used_destinations:
            with contextlib.suppress(OSError):
                copied.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            attachment_dir.rmdir()

    for item in raw_artifacts:
        artifact = str(item).strip() if isinstance(item, str) else ""
        if not artifact:
            continue
        src = Path(artifact).expanduser()
        try:
            resolved_src = src.resolve()
        except OSError:
            persisted.append(artifact)
            continue

        if not resolved_src.is_relative_to(workspace_root):
            persisted.append(artifact)
            continue

        problem = None
        if not src.is_file():
            problem = f"declared scratch artifact is unavailable or not a regular file: {artifact}"
        elif resolved_src.stat().st_size > KANBAN_ATTACHMENT_MAX_BYTES:
            problem = (
                f"declared scratch artifact exceeds the "
                f"{KANBAN_ATTACHMENT_MAX_BYTES}-byte limit: {artifact}"
            )
        if problem:
            _discard_copies()
            raise ArtifactPreservationError(problem)

        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_attachment_path(attachment_dir, resolved_src.name, used_destinations)
            _copy_capped(resolved_src, dest, artifact)
        except Exception as exc:
            if dest is not None:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            _discard_copies()
            if isinstance(exc, ArtifactPreservationError):
                raise
            raise ArtifactPreservationError(
                f"could not preserve declared scratch artifact {artifact}: {exc}"
            ) from exc
        used_destinations.add(dest)
        persisted.append(str(dest.resolve()))
        changed = True

    if changed:
        metadata["artifacts"] = persisted
        metadata["_staged_artifacts"] = [
            path for path in persisted if path.startswith(str(attachment_dir.resolve()))
        ]


def _copy_capped(src: Path, dest: Path, artifact: str) -> None:
    """Chunked copy that aborts if the file grows past the attachment cap mid-copy."""
    with src.open("rb") as source_file, dest.open("xb") as destination_file:
        copied = 0
        while chunk := source_file.read(1024 * 1024):
            copied += len(chunk)
            if copied > KANBAN_ATTACHMENT_MAX_BYTES:
                raise ArtifactPreservationError(
                    f"declared scratch artifact grew beyond the size limit: {artifact}"
                )
            destination_file.write(chunk)


def _insert_completion_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str, size: int,
    created_at: int,
) -> None:
    """Record a worker-produced artifact in the existing attachment table."""
    conn.execute(
        "INSERT INTO task_attachments "
        "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
        "VALUES (?, ?, ?, NULL, ?, 'kanban_complete', ?)",
        (task_id, filename, stored_path, size, created_at),
    )
    _append_event(conn, task_id, "attached", {"filename": filename, "size": size, "by": "kanban_complete"})


def _unique_attachment_path(directory: Path, filename: str, used: set[Path]) -> Path:
    """Return a non-conflicting path under ``directory`` for ``filename``."""
    safe_name = Path(filename).name or "artifact"
    stem, suffix = Path(safe_name).stem or "artifact", Path(safe_name).suffix
    candidate = directory / safe_name
    idx = 1
    while candidate in used or candidate.exists():
        candidate = directory / f"{stem}_{idx}{suffix}"
        idx += 1
    return candidate


def edit_completed_task_result(
    conn: sqlite3.Connection, task_id: str, *, result: str, summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Backfill the user-visible result for an already completed task."""
    handoff_summary = summary if summary is not None else result
    with write_txn(conn):
        if _task_status(conn, task_id) != "done":
            return False
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (result, task_id))
        run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if run is None:
            run_id = _synthesize_ended_run(
                conn, task_id, outcome="completed", summary=handoff_summary, metadata=metadata,
            )
        else:
            run_id = int(run["id"])
            conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (handoff_summary, run_id))
            if metadata is not None:
                conn.execute(
                    "UPDATE task_runs SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), run_id),
                )
        _append_event(
            conn, task_id, "edited",
            {
                "fields": ["result", "summary"] + (["metadata"] if metadata is not None else []),
                "result_len": len(result) if result else 0,
                "summary": _first_line(handoff_summary, 400) or None,
            },
            run_id=run_id,
        )
    return True


def block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    kind: Optional[str] = None,
    attempted_resolutions: Optional[Iterable[str]] = None,
    metadata: Optional[dict] = None,
    expected_run_id: Optional[int] = None,
    board: Optional[str] = None,
    human_escalation_assignee: Optional[str] = None,
    allow_todo: bool = False,
) -> bool:
    """Transition a blockable task to ``blocked`` (or route elsewhere).

    ``allow_todo`` extends the operator path to idle ``todo`` cards without
    changing the worker-facing default.

    ``kind`` (one of :data:`VALID_BLOCK_KINDS`, or ``None`` for a legacy
    un-typed block) drives routing instead of every block landing in one
    undifferentiated ``blocked`` bucket:

    * ``dependency`` — the task is only waiting on another task. It does NOT
      sit in ``blocked`` (where a cron would keep "unblocking" it); it goes to
      ``todo`` so the existing parent-gating / ``recompute_ready`` machinery
      promotes it automatically once its parents finish. No human, no cron, no
      retry storm. This is Dale's "Type 2 — dependency blocked".

    * ``needs_input`` / ``capability`` / ``None`` — "truly blocked" (Dale's
      "Type 1"). Lands in ``blocked`` for a human. BUT: each time such a task
      is re-blocked for the SAME kind after having been unblocked, the
      unblock-loop counter (``block_recurrences``) increments. When it reaches
      :data:`BLOCK_RECURRENCE_LIMIT`, the task is routed to ``triage`` instead
      of ``blocked`` — breaking the cron-unblock ↔ worker-re-block loop and
      forcing a human-in-the-loop triage decision.

    * ``transient`` — treated like a generic block for routing, but a worker
      can use it to signal "this might clear on its own"; it still participates
      in the loop breaker so a forever-flaky task eventually escalates.

    Returns True on any successful transition (to ``blocked``, ``todo``, or
    ``triage``), False when the task wasn't in a blockable state.
    """
    board = board or _board_slug_for_connection(conn)
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(
            f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None"
        )
    product_preflight = _route_product_human_block_to_preflight(
        conn,
        task_id,
        board=board,
        reason=reason,
        kind=kind,
        attempted_resolutions=attempted_resolutions,
        metadata=metadata,
        expected_run_id=expected_run_id,
        human_escalation_assignee=human_escalation_assignee,
    )
    if product_preflight is not None:
        return product_preflight
    meta = product_board_metadata(board)
    routed_to = "blocked"
    recurrences = 0
    attempts = [str(a).strip() for a in (attempted_resolutions or []) if str(a).strip()]

    if kind == "dependency":
        with write_txn(conn):
            # Capture the phase we are leaving BEFORE the UPDATE below rewrites
            # it to 'todo', so the dependency_wait event can record where the
            # card must resume once its parents finish (review vs ready).
            _dep_row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            source_status = (
                _retry_status_for_run(conn, task_id)
                if _dep_row is not None and _dep_row["status"] == "running"
                else (
                    "review"
                    if _dep_row is not None and _dep_row["status"] == "review"
                    else "ready"
                )
            )
            if expected_run_id is None:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = 'todo',
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           block_kind    = ?
                     WHERE id = ?
                       AND (status IN ('running', 'ready', 'review')
                            OR (? AND status = 'todo'))
                    """,
                    (kind, task_id, int(allow_todo)),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = 'todo',
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           block_kind    = ?
                     WHERE id = ?
                       AND (status IN ('running', 'ready', 'review')
                            OR (? AND status = 'todo'))
                       AND current_run_id = ?
                    """,
                    (kind, task_id, int(allow_todo), int(expected_run_id)),
                )
            if cur.rowcount != 1:
                return False
            if _handoff_v2_enabled(meta):
                conn.execute(
                    "UPDATE tasks SET running = 0, blocked = 0 WHERE id = ?",
                    (task_id,),
                )
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
                metadata=metadata,
            )
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="blocked", summary=reason,
                    metadata=metadata,
                )
            _append_event(
                conn, task_id, "dependency_wait",
                {
                    "reason": reason,
                    "kind": kind,
                    "attempted_resolutions": attempts,
                    "source_status": source_status,
                },
                run_id=run_id,
            )
            _blocked_task = get_task(conn, task_id)
            _fire_kanban_lifecycle_hook(
                "kanban_task_blocked",
                task_id,
                board=get_current_board(),
                assignee=_blocked_task.assignee if _blocked_task else None,
                run_id=run_id,
                reason=reason,
            )
        return True

    with write_txn(conn):
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if cur_row is None:
            return False
        source_status = (
            _retry_status_for_run(conn, task_id)
            if cur_row["status"] == "running"
            else "ready"
        )
        prev_kind = cur_row["block_kind"] if "block_kind" in cur_row.keys() else None
        prev_recurrences = (
            int(cur_row["block_recurrences"])
            if "block_recurrences" in cur_row.keys()
            and cur_row["block_recurrences"] is not None
            else 0
        )

        # Truly-blocked kinds. Increment the unblock-loop counter when this is a
        # re-block for the SAME reason after a prior unblock. block_task only
        # fires from running/ready (i.e. AFTER an unblock returned the task to
        # the work pool), so a stored block_kind that matches the incoming kind
        # means: blocked → unblocked → about-to-re-block for the same cause.
        # An un-typed (None) block compares as "same" to a prior un-typed block.
        same_cause = prev_kind == kind
        recurrences = prev_recurrences + 1 if same_cause else 1

        if recurrences >= BLOCK_RECURRENCE_LIMIT:
            # Loop detected — stop letting the unblocker spin this task. Route
            # to triage for a human-in-the-loop decision instead of blocked.
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'triage',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       block_kind    = ?,
                       block_recurrences = ?
                 WHERE id = ?
                   AND (status IN ('running', 'ready', 'review')
                        OR (? AND status = 'todo'))
                """ + ("" if expected_run_id is None else " AND current_run_id = ?"),
                (kind, recurrences, task_id, int(allow_todo)) if expected_run_id is None
                else (kind, recurrences, task_id, int(allow_todo), int(expected_run_id)),
            )
            if cur.rowcount != 1:
                return False
            # v2 flag maintenance: triage (like todo) is neither running nor
            # blocked -- direct UPDATE, no _sync_legacy_status (would
            # clobber the explicit 'triage' status).
            if _handoff_v2_enabled(meta):
                conn.execute(
                    "UPDATE tasks SET running = 0, blocked = 0 WHERE id = ?",
                    (task_id,),
                )
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
                metadata=metadata,
            )
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="blocked", summary=reason,
                    metadata=metadata,
                )
            _append_event(
                conn, task_id, "block_loop_detected",
                {
                    "reason": reason,
                    "kind": kind,
                    "recurrences": recurrences,
                    "limit": BLOCK_RECURRENCE_LIMIT,
                    "attempted_resolutions": attempts,
                    "source_status": source_status,
                },
                run_id=run_id,
            )
        else:
            if expected_run_id is None:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = 'blocked',
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           block_kind    = ?,
                           block_recurrences = ?
                     WHERE id = ?
                       AND (status IN ('running', 'ready', 'review')
                            OR (? AND status = 'todo'))
                    """,
                    (kind, recurrences, task_id, int(allow_todo)),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = 'blocked',
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           block_kind    = ?,
                           block_recurrences = ?
                     WHERE id = ?
                       AND (status IN ('running', 'ready', 'review')
                            OR (? AND status = 'todo'))
                       AND current_run_id = ?
                    """,
                    (
                        kind,
                        recurrences,
                        task_id,
                        int(allow_todo),
                        int(expected_run_id),
                    ),
                )
            if cur.rowcount != 1:
                return False
            # v2 seam: the 'blocked' landing is flag-derivable, so route
            # through _apply_v2_flags (sync gives 'blocked' -- consistent
            # with the status just set above).
            _apply_v2_flags(conn, task_id, meta, running=False, blocked=True)
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=reason,
                metadata=metadata,
            )
            # Synthesize a run when blocking a never-claimed task so the
            # reason is preserved in attempt history.
            if run_id is None and reason:
                run_id = _synthesize_ended_run(
                    conn, task_id,
                    outcome="blocked",
                    summary=reason,
                    metadata=metadata,
                )
            _append_event(
                conn, task_id, "blocked",
                {
                    "reason": reason,
                    "kind": kind,
                    "recurrences": recurrences,
                    "attempted_resolutions": attempts,
                    "source_status": source_status,
                },
                run_id=run_id,
            )
        _blocked_task = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook(
        "kanban_task_blocked",
        task_id,
        board=get_current_board(),
        assignee=_blocked_task.assignee if _blocked_task else None,
        run_id=run_id,
        reason=reason,
    )
    return True



def _route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int,
) -> tuple[str, str, str, tuple, dict]:
    """``(new_status, event_kind, set_sql, params, payload)`` for :func:`block_task`.

    ``dependency`` never enters the human ``blocked`` bucket: it waits in
    ``todo`` for ``recompute_ready``, so a cron never sees a dependency-wait
    as something to "unblock". Every other kind counts unblock-loop
    recurrences: block_task only fires from running/ready (AFTER an unblock
    returned the task to the pool), so a stored ``block_kind`` equal to the
    incoming one means blocked -> unblocked -> re-block for the same cause
    (un-typed None compares equal to a prior un-typed block). At
    ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
    """
    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload


def redact_review_value(value: Any) -> Any:
    """Redact secrets at the domain boundary for durable review handoffs."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: redact_review_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_review_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_review_value(item) for item in value)
    return value


def request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, force: bool = False, with_reason: bool = False,
):
    """``running``/``ready`` -> ``review``; never touches block recurrence accounting.

    Implementer and reviewer are recorded on the event so requested changes
    route back to the right profile; ``reviewer`` reassigns the task, and on
    re-review defaults to the latest ``changes_requested`` provenance. A live
    claim is only cleared with proof of ownership (``expected_run_id``) or
    ``force=True``. Returns ``bool``, or ``(ok, reason)`` with ``with_reason``.
    """

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    summary = redact_review_value(summary)
    metadata = redact_review_value(metadata)
    with write_txn(conn):
        if not _parents_satisfied(conn, task_id):
            return _ret(False, "parent dependencies are not satisfied")
        trow = conn.execute(
            "SELECT assignee, status, claim_lock, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if trow is None:
            return _ret(False, "task not found")
        # Refuse to clear a live worker's claim without proof of ownership
        # (expected_run_id) or an explicit human override (force=True).
        if (
            expected_run_id is None
            and not force
            and trow["status"] == "running"
            and trow["claim_lock"] is not None
        ):
            return _ret(
                False, "task is running under a live claim; pass expected_run_id "
                "(worker ownership) or force=True (explicit operator "
                "override) instead of clearing the live run's claim",
            )
        implementer = trow["assignee"]
        if reviewer is None:
            reviewer = _prior_reviewer(conn, task_id)
            if reviewer is False:
                return _ret(
                    False, "re-review has no durable reviewer provenance (the "
                    "latest changes_requested event is missing or "
                    "malformed); pass reviewer= explicitly",
                )
        reviewer = _canonical_assignee(reviewer)
        assignee_sql = ", assignee = ?" if reviewer is not None else ""
        run_guard = "" if expected_run_id is None else " AND current_run_id = ?"
        params: tuple[Any, ...] = (
            *(() if reviewer is None else (reviewer,)), task_id,
            *(() if expected_run_id is None else (int(expected_run_id),)),
        )
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'review',
                   claim_lock    = NULL,
                   claim_expires = NULL,
                   worker_pid    = NULL
            """ + assignee_sql + """
             WHERE id = ?
               AND status IN ('running', 'ready')
            """ + run_guard,
            params,
        )
        if cur.rowcount != 1:
            return _ret(
                False, "task is not in running/ready (or expected_run_id did not match the current run)",
            )
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="review_requested", status="review",
            summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
        )
        _append_event(
            conn,
            task_id,
            "review_requested",
            {
                "summary": _first_line(summary, 400) or None,
                "implementer": implementer,
                "reviewer": reviewer,
            },
            run_id=run_id,
        )
    return _ret(True)


def _prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """Reviewer recorded by the latest ``changes_requested`` run's event.
    ``None`` = first review (no such run); ``False`` = a run exists but its
    provenance is missing/malformed."""
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = _latest_event(conn, task_id, "changes_requested", changes_run["id"])
    reviewer = _json_dict(_row_get(changes_event, "payload")).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def _nonblank_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def request_changes(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    expected_run_id: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Finish an active review run and route the task back for rework.

    The transition is valid only for a run claimed from ``review``.  It closes
    that reviewer run, restores the implementer recorded by the latest
    ``review_requested`` event, reapplies parent gating, and emits an auditable
    ``changes_requested`` event.  The second tuple item is the implementer on
    success or a diagnostic reason on failure.
    """
    reason = str(redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"

    with write_txn(conn):
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
            "ORDER BY id DESC LIMIT 1",
            (task_id, int(current_run_id)),
        ).fetchone()
        try:
            claimed_payload = (
                json.loads(claimed_event["payload"])
                if claimed_event and claimed_event["payload"]
                else {}
            )
        except (json.JSONDecodeError, TypeError):
            claimed_payload = {}
        if not isinstance(claimed_payload, dict):
            claimed_payload = {}
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"

        requested_event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if requested_event is None:
            return False, "no prior review_requested event"
        try:
            requested_payload = (
                json.loads(requested_event["payload"])
                if requested_event["payload"]
                else {}
            )
        except (json.JSONDecodeError, TypeError):
            requested_payload = {}
        if not isinstance(requested_payload, dict):
            requested_payload = {}
        implementer = requested_payload.get("implementer")
        if not isinstance(implementer, str) or not implementer.strip():
            return False, "review handoff has no valid implementer provenance"
        reviewer = task_row["assignee"]
        if isinstance(reviewer, str) and reviewer.strip():
            reviewer = _canonical_assignee(reviewer)
        else:
            reviewer = None

        new_status = _landing_status_after_parents(conn, task_id)
        # NOTE: consecutive_failures is deliberately PRESERVED (neither
        # reset nor incremented). Review transitions are not evidence the
        # pathology cleared — only complete_task's success path resets the
        # breaker counter (mirrors unblock_task, #35072).
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        )
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        run_id = _end_run(
            conn,
            task_id,
            outcome="changes_requested",
            status=new_status,
            summary=reason,
        )
        _append_event(
            conn,
            task_id,
            "changes_requested",
            {
                "reason": reason,
                "implementer": implementer,
                "reviewer": reviewer,
                "status": new_status,
            },
            run_id=run_id,
        )
    return True, implementer



def promote_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Manually promote a `todo` or `blocked` task to `ready`.

    Mirrors the automatic promotion done by ``recompute_ready`` but
    drives it from a deliberate operator action with an audit-trail
    entry. Refuses to promote if any parent dep is not in a terminal
    state (`done`/`archived`) unless ``force=True``. Does NOT change
    assignee or claim state. Returns ``(True, None)`` on success and
    ``(False, reason)`` if refused. ``dry_run=True`` validates the
    promotion would succeed without mutating state.
    """
    row = conn.execute(
        "SELECT status, workflow_template_id, current_step_key FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False, f"task {task_id} not found"
    if _is_engine_owned_integration_state(row):
        return False, "engine-owned integration state cannot be promoted"

    cur_status = row["status"]
    if cur_status not in ("todo", "blocked"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )

    if cur_status == "blocked":
        scope = conn.execute(
            "SELECT workflow_template_id, current_step_key FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if (
            scope is not None
            and scope["workflow_template_id"] == "product"
            and scope["current_step_key"] == "development"
            and _development_budget_exhaustion_count(conn, task_id) >= 2
        ):
            return False, "human approval is required after repeated budget exhaustion"

    if not force:
        parents = conn.execute(
            "SELECT t.id, t.status FROM tasks t "
            "JOIN task_links l ON l.parent_id = t.id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        unsatisfied = [
            p["id"] for p in parents
            if p["status"] not in ("done", "archived")
        ]
        if unsatisfied:
            return False, (
                f"unsatisfied parent dependencies: "
                f"{', '.join(unsatisfied)} (use --force to override)"
            )

    if dry_run:
        return True, None

    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status IN ('todo', 'blocked')",
            (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _append_event(
            conn,
            task_id,
            "promoted_manual",
            {"actor": actor, "reason": reason, "forced": force},
        )

    return True, None



def _product_unblock_assignee(
    meta: Optional[dict], task_scope: Optional[sqlite3.Row]
) -> tuple[bool, Optional[str]]:
    """Return whether unblock owns the assignee field and its governed value."""
    if (
        task_scope is None
        or task_scope["workflow_template_id"] != "product"
        or not isinstance(meta, dict)
    ):
        return False, None

    step_key = str(task_scope["current_step_key"] or "").strip()
    if not step_key:
        return False, None

    qualification = meta.get("qualification")
    if isinstance(qualification, dict) and qualification.get("required") is True:
        phase_assignees = qualification.get("phase_assignees")
        if not isinstance(phase_assignees, dict) or step_key not in phase_assignees:
            return False, None
        mapped = phase_assignees[step_key]
    else:
        phase_roles = PRODUCT_QUALIFICATION_DEFAULTS.get("phase_assignees", {})
        if step_key not in phase_roles:
            return False, None
        role = phase_roles[step_key]
        if role is None:
            return True, None
        workflow_assignees = _product_workflow_dict(meta).get("assignees")
        if not isinstance(workflow_assignees, dict) or role not in workflow_assignees:
            return False, None
        mapped = workflow_assignees[role]

    if mapped is None:
        return (True, None) if step_key == "release_measure" else (False, None)
    assignee = str(mapped).strip()
    return (True, assignee) if assignee else (False, None)



def _apply_unblock_state(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    new_status: str,
    task_scope: Optional[sqlite3.Row],
    meta: Optional[dict],
    allow_scheduled: bool,
) -> int:
    """Apply canonical unblock state, including the board-owned phase route."""
    restore_assignee, assignee = _product_unblock_assignee(meta, task_scope)
    assignee_sql = ", assignee = ?" if restore_assignee else ""
    status_sql = (
        "status IN ('blocked', 'scheduled')" if allow_scheduled else "status = 'blocked'"
    )
    params: list[object] = [new_status]
    if restore_assignee:
        params.append(assignee)
    params.append(task_id)
    sql = (
        f"UPDATE tasks SET status = ?{assignee_sql}, current_run_id = NULL, "
        "consecutive_failures = 0, last_failure_error = NULL "
        f"WHERE id = ? AND {status_sql}"
    )
    if restore_assignee:
        # Strict-board routing triggers allow only canonical governance writes.
        with authorized_governance_write():
            return conn.execute(sql, params).rowcount
    return conn.execute(sql, params).rowcount


def _reclaim_dangling_run(
    conn: sqlite3.Connection, task_id: str, *, statuses, now: int, note: str,
) -> None:
    """Close a leaked ``current_run_id`` (run row still open) before a status
    flip, preserving the runs invariant (``current_run_id IS NULL`` ⇔ run row
    terminal). No-op in the common path where the prior transition already
    closed the run. Shared by :func:`unblock_task` and
    :func:`reopen_review_task` so the recovery can't drift.
    """
    placeholders = ", ".join("?" for _ in statuses)
    stale = conn.execute(
        f"SELECT current_run_id FROM tasks WHERE id = ? AND status IN ({placeholders})",
        (task_id, *statuses),
    ).fetchone()
    if stale and stale["current_run_id"]:
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, ?),
                   ended_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (note, now, int(stale["current_run_id"])),
        )



def _landing_status_after_parents(conn: sqlite3.Connection, task_id: str) -> str:
    """Return ``'todo'`` if any parent isn't ``done`` yet, else ``'ready'``.

    The parent-completion re-gate shared by :func:`unblock_task` and
    :func:`reopen_review_task`: flipping straight to ``ready`` would bypass the
    parent-completion invariant the dispatcher trusts (it would spawn a child
    whose upstream work isn't finished). If parents are still in progress the
    task waits in ``todo`` until ``recompute_ready`` picks it up. RCA: Bug 2 at
    kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md. Kept in one place
    so the two transitions can't drift.
    """
    undone_parents = conn.execute(
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? "
        "AND p.status NOT IN ('done', 'archived') LIMIT 1",
        (task_id,),
    ).fetchone()
    return "todo" if undone_parents else "ready"



def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Transition ``blocked``/``scheduled`` -> ready or todo.

    Defensively closes any stale ``current_run_id`` pointer before flipping
    status. In the common path (``block_task`` closed the run already) this
    is a no-op. If a future or external write left the pointer dangling,
    the leaked run is closed as ``reclaimed`` inside the same txn so the
    runs invariant (``current_run_id IS NULL`` ⇔ run row in terminal
    state) holds for the rest of this function's lifetime.
    """
    meta = product_board_metadata(_board_slug_for_connection(conn))
    now = int(time.time())
    with write_txn(conn):
        lifecycle_scope = conn.execute(
            "SELECT workflow_template_id, current_step_key FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if _is_engine_owned_integration_state(lifecycle_scope):
            return False
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on unblock'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        # Re-gate on parent completion before flipping 'blocked' back to
        # 'ready'. Unconditionally setting status='ready' here bypasses the
        # parent-completion invariant (the dispatcher trusts that column);
        # if parents are still in progress the task must wait in 'todo'
        # until recompute_ready picks it up. RCA: Bug 2 at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        parents = conn.execute(
            "SELECT p.status, p.workflow_template_id, p.current_step_key "
            "FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        release_measure_unblocks = _product_release_measure_unblocks_dependents(meta)
        has_undone_parent = any(
            not _dependency_parent_satisfied(
                parent,
                release_measure_unblocks=release_measure_unblocks,
            )
            for parent in parents
        )
        task_scope = conn.execute(
            "SELECT t.workflow_template_id, t.current_step_key, "
            "json_extract(w.canonical_json, '$.po_evidence.surface') "
            "AS qualification_surface "
            "FROM tasks t LEFT JOIN work_contracts w ON w.id = t.work_contract_id "
            "WHERE t.id = ?",
            (task_id,),
        ).fetchone()
        if (
            meta is not None
            and not _GOVERNANCE_WRITE_AUTHORIZED.get()
            and task_scope is not None
            and task_scope["workflow_template_id"] == "product"
            and task_scope["current_step_key"] == "development"
            and _development_budget_exhaustion_count(conn, task_id) >= 1
        ):
            return False
        architecture_assessment = (
            task_scope is not None
            and task_scope["current_step_key"] == "architecture"
            and task_scope["qualification_surface"] == "work_inbox_intake"
        )
        new_status = (
            "todo"
            if has_undone_parent and not architecture_assessment
            else "ready"
        )
        # Restore the phase the block came FROM. A card blocked out of the
        # review lane must return to ``review``, not to ``ready`` -- otherwise
        # an escalation silently converts a reviewer handoff back into
        # implementation work. Parent gating still wins: if parents are
        # unfinished the card waits in ``todo`` regardless.
        _current_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        resume_status = (
            _resume_status_from_events(conn, task_id)
            if _current_row is not None and _current_row["status"] == "blocked"
            else "ready"
        )
        if new_status == "ready" and resume_status == "review":
            new_status = "review"
        # NOTE: deliberately does NOT touch ``block_recurrences`` or
        # ``block_kind``. Resetting the recurrence counter on unblock is exactly
        # the amnesia that let a cron unblock → worker re-block loop run
        # unbounded (Dale's report). The counter survives the unblock so that a
        # subsequent same-cause ``block_task`` can detect the loop and route to
        # triage at ``BLOCK_RECURRENCE_LIMIT``. It is reset to 0 only on a
        # successful completion (see ``complete_task``). ``consecutive_failures``
        # (the *dispatcher* spawn/crash/timeout counter — a different signal) is
        # still reset here, which is correct: a deliberate unblock is a fresh
        # start for the dispatcher's retry budget.
        rowcount = _apply_unblock_state(
            conn,
            task_id=task_id,
            new_status=new_status,
            task_scope=task_scope,
            meta=meta,
            allow_scheduled=True,
        )
        if rowcount != 1:
            return False
        # v2 flag maintenance: a just-unblocked card is idle -- (0, 0) --
        # regardless of whether it lands in 'todo' or 'ready'. Direct
        # UPDATE, no _sync_legacy_status (it may land in 'todo', which the
        # sync seam cannot derive).
        if _handoff_v2_enabled(meta):
            conn.execute(
                "UPDATE tasks SET running = 0, blocked = 0 WHERE id = ?",
                (task_id,),
            )
        _append_event(
            conn, task_id, "unblocked",
            (
                {"status": new_status, "resume_status": resume_status}
                if new_status != "ready" or resume_status != "ready"
                else None
            ),
        )
        return True



def reopen_review_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``review`` -> ``ready``/``todo`` so the implementer re-runs on the new
    comments; restores the implementer from the ``review_requested`` event.
    Preserves ``consecutive_failures`` and the block loop counter (review is
    not a block; only :func:`complete_task` clears them)."""
    now = int(time.time())
    with write_txn(conn):
        _reclaim_dangling_run(
            conn, task_id, statuses=("review",), now=now,
            note="invariant recovery on review reopen",
        )
        new_status = _landing_status_after_parents(conn, task_id)
        review_event = _latest_event(conn, task_id, "review_requested")
        handoff = _json_dict(_row_get(review_event, "payload"))
        implementer = _nonblank_str(handoff.get("implementer"))
        params: tuple[Any, ...] = (new_status, *((implementer,) if implementer else ()), task_id)
        cur = conn.execute(
            # consecutive_failures deliberately PRESERVED: review reopen is not
            # a success signal; only complete_task resets the breaker (#35072).
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            + (", assignee = ?" if implementer else "")
            + " WHERE id = ? AND status = 'review'",
            params,
        )
        if cur.rowcount != 1:
            return False
        payload: dict[str, Any] = {"status": new_status}
        if implementer:
            payload["implementer"] = implementer
        _append_event(
            conn, task_id, "review_reopened", payload if payload != {"status": "ready"} else None,
        )
        return True


def invalidate_descendants_for_parent_reopen(
    conn: sqlite3.Connection, task_id: str, *, author: str,
) -> dict[str, Any]:
    """THE done-reopen invalidation: every ``ready``/``review``/``running``/``done``
    descendant of a reopened ancestor is demoted to ``todo`` and re-gated.
    Every surface that reopens a done task (dashboard PATCH/drag) routes here.

    Composes under the caller's txn (``allow_nested=True``) so the flip and the
    retractions commit atomically. Each descendant gets a
    ``descendant_invalidated`` event, the legacy ``status`` event the live feed
    renders, and a comment naming the ancestor. Running descendants are closed
    ``reclaimed`` and their workers killed strictly post-commit (audit trail
    before death) — when composed, the CALLER must drain ``terminations``
    after its own commit. ``consecutive_failures`` resets (deliberate operator
    action), the opposite of :func:`reopen_review_task`.

    Returns ``{"invalidated": [{id, prior_status, new_status, resume_status}],
    "terminations": [(worker_pid, claim_lock)]}``.
    """
    caller_owns_txn = bool(conn.in_transaction)
    now = int(time.time())
    invalidated: list[dict[str, Any]] = []
    terminations: list[tuple[Optional[int], Optional[str]]] = []
    with write_txn(conn, allow_nested=True):
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT child_id FROM task_links WHERE parent_id = ?
                UNION
                SELECT l.child_id
                FROM task_links l
                JOIN descendants d ON d.id = l.parent_id
            )
            SELECT t.id, t.status, t.current_run_id, t.worker_pid, t.claim_lock
            FROM descendants d
            JOIN tasks t ON t.id = d.id
            ORDER BY t.id
            """,
            (task_id,),
        ).fetchall()
        for row in rows:
            previous_status = row["status"]
            if previous_status not in {"ready", "review", "running", "done"}:
                continue
            resume_status = "ready"
            run_id = None
            if previous_status == "review":
                resume_status = "review"
            elif previous_status == "running":
                resume_status = _retry_status_for_run(conn, row["id"], row["current_run_id"])
                terminations.append((row["worker_pid"], row["claim_lock"]))
                run_id = _end_run(
                    conn, row["id"], outcome="reclaimed", status="todo",
                    summary=f"ancestor {task_id} reopened",
                )
            # consecutive_failures = 0: deliberate operator reset — see
            # docstring for why this diverges from reopen_review_task.
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "current_run_id = NULL, consecutive_failures = 0 WHERE id = ?", (row["id"],),
            )
            entry = {
                "id": row["id"], "prior_status": previous_status,
                "new_status": "todo", "resume_status": resume_status,
            }
            _append_event(
                conn, row["id"], "descendant_invalidated",
                {"ancestor": task_id, **{k: v for k, v in entry.items() if k != "id"}},
                run_id=run_id,
            )
            # Legacy 'status' event so existing live-feed consumers still see
            # the move without learning the new event kind.
            _append_event(
                conn, row["id"], "status",
                {
                    "status": "todo", "reason": "ancestor_reopened", "parent": task_id,
                    "previous_status": previous_status, "resume_status": resume_status,
                },
                run_id=run_id,
            )
            _insert_comment(
                conn, row["id"], author, f"Invalidated: ancestor {task_id} was reopened; "
                f"retracted from '{previous_status}' to 'todo' "
                f"(will resume via '{resume_status}').", now,
            )
            invalidated.append(entry)
    if not caller_owns_txn:
        # Standalone: committed above, audit trail durable, safe to kill now.
        # Composed calls leave this to the caller post-commit.
        for pid, claim_lock in terminations:
            _terminate_reclaimed_worker(pid, claim_lock)
    return {"invalidated": invalidated, "terminations": terminations}


def reopen_review_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Transition ``review`` -> ready (or todo) so the implementer re-runs.

    The "changes requested" counterpart of :func:`request_review`: sends the
    task back out of the review lane so the dispatcher re-runs the implementer
    on the new comments. Mirrors :func:`unblock_task` (parent re-gating,
    defensive stale-run close, ``consecutive_failures`` preserved) and emits a
    ``review_reopened`` event.

    Deliberately does NOT touch ``block_recurrences``/``block_kind``: review is
    not a block, so there is no loop counter to reset. (A stale counter from a
    genuine block *before* review is left intact — only :func:`complete_task`
    clears it.) Returns False when the task is missing or not in ``review``.
    """
    now = int(time.time())
    with write_txn(conn):
        _reclaim_dangling_run(
            conn, task_id, statuses=("review",), now=now,
            note="invariant recovery on review reopen",
        )
        new_status = _landing_status_after_parents(conn, task_id)
        review_event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        try:
            handoff = (
                json.loads(review_event["payload"])
                if review_event and review_event["payload"]
                else {}
            )
        except (json.JSONDecodeError, TypeError):
            handoff = {}
        implementer = handoff.get("implementer")
        if not isinstance(implementer, str) or not implementer.strip():
            implementer = None
        assignee_sql = ", assignee = ?" if implementer else ""
        params: tuple[Any, ...] = (
            (new_status, implementer, task_id)
            if implementer
            else (new_status, task_id)
        )
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            # consecutive_failures deliberately PRESERVED: review reopen is
            # not a success signal; only complete_task resets the breaker
            # counter (mirrors unblock_task, #35072).
            + assignee_sql
            + " WHERE id = ? AND status = 'review'",
            params,
        )
        if cur.rowcount != 1:
            return False
        payload: dict[str, Any] = {"status": new_status}
        if implementer:
            payload["implementer"] = implementer
        _append_event(
            conn,
            task_id,
            "review_reopened",
            payload if payload != {"status": "ready"} else None,
        )
        return True


def approve_unblock_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_status: Optional[str],
    expected_title: Optional[str],
    comment_author: str,
    comment_source: str = "Agentic OS Cockpit approve/unblock control",
) -> Optional[Task]:
    """Atomically approve and unblock one blocked task with an audit comment.

    This is the server-side counterpart to a UI confirmation prompt: the
    confirmed snapshot is re-read and validated inside the same write
    transaction that performs canonical unblock bookkeeping and records the
    traceability comment. A stale snapshot raises ``RuntimeError`` and leaves
    both status and comments untouched.
    """
    if expected_status != "blocked":
        raise ValueError("expected status snapshot must be blocked")
    if expected_title is None:
        raise ValueError("expected title snapshot is required")
    author = (comment_author or "dashboard").strip() or "dashboard"
    default_source = "Agentic OS Cockpit approve/unblock control"
    source = (comment_source or default_source).strip() or default_source
    board = _board_slug_for_connection(conn)
    meta = product_board_metadata(board)
    now = int(time.time())

    with write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["status"] == "archived":
            return None
        current_status = row["status"] or ""
        current_title = row["title"] or ""
        if current_status != "blocked" or current_title != expected_title:
            raise RuntimeError("card changed; refresh before approving unblock")

        stale_run_id = row["current_run_id"] if "current_run_id" in row.keys() else None
        if stale_run_id:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on unblock'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale_run_id)),
            )

        undone_parents = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
            (task_id,),
        ).fetchone()
        new_status = "todo" if undone_parents else "ready"
        rowcount = _apply_unblock_state(
            conn,
            task_id=task_id,
            new_status=new_status,
            task_scope=row,
            meta=meta,
            allow_scheduled=False,
        )
        if rowcount != 1:
            raise RuntimeError("card changed; refresh before approving unblock")
        if _handoff_v2_enabled(meta):
            conn.execute(
                "UPDATE tasks SET running = 0, blocked = 0 WHERE id = ?",
                (task_id,),
            )
        _append_event(
            conn,
            task_id,
            "unblocked",
            {"status": new_status} if new_status != "ready" else None,
        )

        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        trace_body = (
            "Traceability log — approval/unblock confirmed. "
            f"Actor: {author}. Board: {board}. Task: {task_id}. Title: {current_title}. "
            "Decision: approved_unblock. "
            f"Resulting status: {new_status}. "
            f"Timestamp: {timestamp}. Source: {source}."
        )
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, author, trace_body, now),
        )
        _append_event(conn, task_id, "commented", {"author": author, "len": len(trace_body)})
        refreshed = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(refreshed) if refreshed else None


def specify_triage_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, assignee: Optional[str] = None, author: Optional[str] = None,
) -> bool:
    """Update title/body/assignee (when given) and move ``triage -> todo`` in one
    txn; False when not in triage. Lands in ``todo`` (not ``ready``) so parent
    gating still applies; the audit comment is written only when a field changed.
    """
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    assignee = _canonical_assignee(assignee)
    with write_txn(conn):
        existing = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        _assert_scoped_expected_task_snapshot(
            existing,
            task_id,
            default_action="specifying the task",
        )
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'", tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Not add_comment (own txn + 'commented' event); 'specified' below records it.
            _insert_comment(
                conn, task_id, author.strip(),
                "Specified — updated " + ", ".join(changed_fields) + " and promoted to todo.",
                int(time.time()),
            )
        _append_event(
            conn, task_id, "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Own IMMEDIATE txn (outside the one above): a parent-free specified task
    # flips to 'ready' now instead of idling until the next tick.
    recompute_ready(conn)
    return True


def decompose_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_assignee: Optional[str],
    children: list[dict],
    author: Optional[str] = None,
    auto_promote: bool = True,
) -> Optional[list[str]]:
    """Fan a triage task out into child tasks and promote the root to ``todo``.

    The root task stays alive and becomes the parent of every child —
    when all children reach ``done``, the root promotes to ``ready`` and
    its assignee (typically the orchestrator profile) wakes back up to
    judge completion or spawn more work.

    ``children`` is a list of dicts, each shaped like::

        {
            "title": "...",
            "body": "...",                     # optional
            "assignee": "profile-name",        # optional, None -> default fallback
            "parents": [0, 2],                 # indices into this same children list
        }

    Returns the list of created child task ids (in input order) on
    success. Returns ``None`` when:
      - The root task does not exist
      - The root task is not in ``triage``
      - A cycle would result (caller built a bad graph)

    Validation of titles/assignees happens inside the same write_txn as
    the inserts so a malformed entry aborts the whole decomposition
    cleanly (no orphan children).
    """
    if not children:
        return None
    if root_assignee is not None:
        root_assignee = _canonical_assignee(root_assignee)

    # Pre-validate the children list shape outside the txn. Cheap checks
    # that don't need DB access. Bad input aborts before we touch the DB.
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError(f"child[{idx}] is not a dict")
        title = child.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"child[{idx}].title is required")
        parents_idx = child.get("parents") or []
        if not isinstance(parents_idx, list):
            raise ValueError(f"child[{idx}].parents must be a list")
        for p in parents_idx:
            if not isinstance(p, int) or p < 0 or p >= len(children):
                raise ValueError(
                    f"child[{idx}].parents[{p}] is not a valid index into children"
                )
            if p == idx:
                raise ValueError(f"child[{idx}] cannot list itself as a parent")

    # Detect cycles in the sibling parent graph (Kahn's topological sort).
    # link_tasks() calls _would_cycle() for every new edge; here we check
    # the entire sibling graph before touching the DB.  A cycle silently
    # deadlocks every involved child in 'todo' because recompute_ready()
    # can never promote them.
    _in_deg = [0] * len(children)
    _adj: list[list[int]] = [[] for _ in range(len(children))]
    for _i, _c in enumerate(children):
        for _p in (_c.get("parents") or []):
            _adj[_p].append(_i)
            _in_deg[_i] += 1
    _queue = [_i for _i in range(len(children)) if _in_deg[_i] == 0]
    _seen = 0
    while _queue:
        _node = _queue.pop()
        _seen += 1
        for _nb in _adj[_node]:
            _in_deg[_nb] -= 1
            if _in_deg[_nb] == 0:
                _queue.append(_nb)
    if _seen != len(children):
        raise ValueError("cyclic dependency detected in decomposed children list")

    # We do the full decomposition in a SINGLE write_txn so it's
    # atomic: either every child is created AND the root flips to
    # ``todo``, or nothing changes. We deliberately do NOT call any
    # kb helper that opens its own write_txn (create_task, link_tasks,
    # add_comment) from inside this block — see architecture.md
    # write_txn pitfalls. Instead we inline the INSERTs and
    # _append_event calls.
    now = int(time.time())
    child_ids: list[str] = []
    with write_txn(conn):
        root_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if root_row is None:
            return None
        _assert_scoped_expected_task_snapshot(
            root_row,
            task_id,
            default_action="decomposing the task",
        )
        if root_row["status"] != "triage":
            return None
        tenant = root_row["tenant"]
        # Children inherit the root's workspace by default so a fan-out
        # of a code-gen task lands in the parent's project dir/worktree
        # rather than throwaway scratch tmp dirs. A child dict can still
        # override with its own 'workspace_kind' / 'workspace_path'.
        root_ws_kind = root_row["workspace_kind"] or "scratch"
        root_ws_path = root_row["workspace_path"]

        # Create children. Status is 'todo' regardless of parents — we
        # link them under the root AFTER creation so the dispatcher
        # sees a coherent state, and recompute_ready() at the end
        # promotes parent-free children to 'ready'.
        for idx, child in enumerate(children):
            new_id = _new_task_id()
            title = child["title"].strip()
            body = child.get("body")
            assignee = _canonical_assignee(child.get("assignee"))
            # Per-child override wins; otherwise inherit the root's
            # workspace. A child that sets workspace_kind without a path
            # falls back to the root path only when kinds match (so a
            # child can't accidentally point a 'dir' at the root's
            # worktree path or vice versa).
            child_ws_kind = child.get("workspace_kind") or root_ws_kind
            if child.get("workspace_path"):
                child_ws_path = child.get("workspace_path")
            elif child_ws_kind == "worktree":
                # Never share one worktree checkout between siblings: the
                # root's literal path would put every child in the same
                # directory on the first-dispatched sibling's branch, with
                # no lock — siblings can be promoted and dispatched
                # concurrently. Leave the path unset so dispatch
                # materializes a fresh <repo>/.worktrees/<child-id> per
                # child from the board anchor.
                child_ws_path = None
            elif child_ws_kind == root_ws_kind:
                child_ws_path = root_ws_path
            else:
                child_ws_path = None
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, body, assignee, status, workspace_kind, "
                " workspace_path, branch_name, project_id, tenant, created_at, "
                " created_by, max_runtime_seconds, skills, max_retries, "
                " model_override, provider_override, reasoning_effort, goal_mode, "
                " goal_max_turns, workflow_template_id, current_step_key, "
                " work_contract_id, work_item_kind, source_commit_required, "
                " source_commit_forbidden) "
                "VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    title,
                    body if isinstance(body, str) else None,
                    assignee,
                    child_ws_kind,
                    child_ws_path,
                    None,
                    root_row["project_id"],
                    tenant,
                    now,
                    (author or "decomposer"),
                    root_row["max_runtime_seconds"],
                    root_row["skills"],
                    root_row["max_retries"],
                    root_row["model_override"],
                    root_row["provider_override"],
                    root_row["reasoning_effort"],
                    root_row["goal_mode"],
                    root_row["goal_max_turns"],
                    root_row["workflow_template_id"],
                    root_row["current_step_key"],
                    None,
                    root_row["work_item_kind"],
                    root_row["source_commit_required"],
                    root_row["source_commit_forbidden"],
                ),
            )
            _append_event(
                conn, new_id, "created",
                {"by": author or "decomposer", "from_decompose_of": task_id},
            )
            _inherit_notify_subs(conn, new_id, (task_id,), created_at=now)
            child_ids.append(new_id)

        # Link children to their sibling parents (within the decomposed graph).
        for idx, child in enumerate(children):
            for p_idx in child.get("parents") or []:
                parent_id = child_ids[p_idx]
                child_id = child_ids[idx]
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                    "VALUES (?, ?)",
                    (parent_id, child_id),
                )
                _append_event(
                    conn, child_id, "linked",
                    {"parent": parent_id, "child": child_id},
                )

        # Link the ROOT task as a child of every leaf child — i.e. the
        # root waits for the whole graph. Simpler than computing leaves:
        # link root under every child. Cycle-free because the root is
        # only ever a child here, never a parent of children.
        for cid in child_ids:
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                "VALUES (?, ?)",
                (cid, task_id),
            )

        # Flip the root: triage -> todo, set assignee to the orchestrator.
        sets = ["status = 'todo'"]
        params: list[Any] = []
        if root_assignee is not None:
            sets.append("assignee = ?")
            params.append(root_assignee)
        params.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

        # Audit comment + event on the root so the timeline shows the fan-out.
        if author and author.strip():
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Decomposed into "
                    + ", ".join(child_ids)
                    + ". Root will wake when all children complete.",
                    now,
                ),
            )
        _append_event(
            conn, task_id, "decomposed",
            {
                "child_ids": child_ids,
                "root_assignee": root_assignee,
            },
        )

    # Outside the write_txn: promote parent-free children to 'ready'
    # so the dispatcher picks them up on its next tick. Same pattern
    # specify_triage_task uses.  When auto_promote is False children
    # stay in 'todo' until the user manually promotes them — useful
    # for manual-review-first workflows.
    if auto_promote:
        recompute_ready(conn)
    return child_ids


def archive_task(conn: sqlite3.Connection, task_id: str) -> bool:
    if _strict_destructive_write_forbidden(conn):
        raise PermissionError("strict-board archival requires Hermes service authority")
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status != 'archived'", (task_id,),
        )
        if cur.rowcount != 1:
            return False
        _apply_v2_flags_for_status(conn, task_id, 'archived')
        # Archived mid-run (dashboard): close the run so history isn't orphaned.
        run_id = _end_run(
            conn, task_id, outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
    # ``archived`` parents no longer block children; promote them now.
    recompute_ready(conn)
    # Reap the workspace on archive too (never-completed tasks kept it forever).
    _cleanup_workspace(conn, task_id)
    return True


def _delete_task_relations(conn: sqlite3.Connection, task_id: str) -> None:
    """Delete every row referencing ``task_id`` (schema has no ON DELETE CASCADE)."""
    conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
    for table in ("task_comments", "task_events", "task_runs", "kanban_notify_subs"):
        conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Permanently remove an archived task after the fork's safety checks."""
    if _strict_destructive_write_forbidden(conn):
        raise PermissionError("strict-board task deletion requires Hermes service authority")
    with write_txn(conn):
        if _task_status(conn, task_id) != "archived":
            return False
        _delete_task_relations(conn, task_id)
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount == 1


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete a task and its related rows in one txn; False when not found."""
    if _strict_destructive_write_forbidden(conn):
        raise PermissionError("strict-board task deletion requires Hermes service authority")
    with write_txn(conn):
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount != 1:
            return False
        _delete_task_relations(conn, task_id)
    recompute_ready(conn)
    return True


def _strict_destructive_write_forbidden(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT qualification_required FROM board_governance WHERE id = 1"
    ).fetchone()
    return bool(
        row is not None
        and int(row["qualification_required"]) == 1
        and not _GOVERNANCE_WRITE_AUTHORIZED.get()
    )


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def _git_toplevel(
    path: Path,
    *,
    git_executable: Optional[str] = None,
) -> Optional[Path]:
    """Return the git toplevel containing ``path``, or ``None`` if not in a repo."""
    executable = git_executable or shutil.which("git")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    try:
        return Path(out).expanduser().resolve()
    except Exception:
        return Path(out).expanduser()


def _git_branch_exists(repo_root: Path, branch_name: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{branch_name}"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def _git_ref_sha(repo_root: Path, branch_name: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"refs/heads/{branch_name}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def _git_common_dir(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    return Path(out).expanduser().resolve(strict=False)


def _git_dir(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-dir"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    return Path(out).expanduser().resolve(strict=False)


def _git_current_branch(path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    branch = (result.stdout or "").strip()
    return branch or None


def _is_linked_worktree_checkout(path: Path) -> bool:
    git_dir = _git_dir(path)
    common_dir = _git_common_dir(path)
    if git_dir is None or common_dir is None:
        return False
    return git_dir != common_dir


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for_worktree_target(path: Path) -> Optional[Path]:
    current = _nearest_existing_path(path).resolve(strict=False)
    while True:
        repo_root = _git_toplevel(current)
        if repo_root is not None:
            return repo_root
        if current == current.parent:
            return None
        current = current.parent


def epic_branch_for(epic_id: str) -> str:
    """Deterministic integration branch name for an epic's story worktrees."""
    return f"epic/{epic_id}"


def _git_head_sha(repo_root: Path) -> Optional[str]:
    """Resolve ``HEAD`` to a full 40-character SHA, or ``None``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception:
        return None
    sha = (result.stdout or "").strip()
    return sha if result.returncode == 0 and len(sha) == 40 else None


def _ensure_epic_branch(
    repo_root: Path, epic_branch: str, *, start_point: Optional[str]
) -> bool:
    """Ensure an epic base exists; create it only at an explicit full SHA.

    Returns whether this call created the ref. ``start_point`` is the exact
    commit the base must be created at — ``None`` means the caller could not
    establish one, and this raises rather than inventing history.

    A missing base is ambiguous once the epic has materialized anything: the
    current ``HEAD`` has probably moved, and recreating the ref there silently
    shifts every story's review baseline. The epic base is a precondition of
    the story worktree that branches off it and of the Review target
    preparation that later runs ``git merge-base`` against it — a story ref
    materialized without it looks healthy and fails much later, off-site
    (2026-07-30 epic ``t_c29de776``: Review runs 531/532 died before reviewer
    spawn and an operator had to create the ref by hand).
    """
    if _git_branch_exists(repo_root, epic_branch):
        return False
    if not start_point:
        raise RuntimeError(
            f"epic base branch {epic_branch} is missing and its historical base "
            "cannot be established from the event ledger; it will not be "
            "recreated from current HEAD. Recreate the ref at the verified "
            "historical commit, then resume dispatch."
        )
    error = ""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "branch", epic_branch, start_point],
            capture_output=True, text=True, timeout=30, check=False,
        )
        error = (result.stderr or result.stdout or "").strip()
    except Exception as exc:  # timeout / OS-level failure
        error = str(exc)
    created_sha = _git_ref_sha(repo_root, epic_branch)
    if created_sha != start_point:
        raise RuntimeError(
            f"could not create epic base branch {epic_branch} in {repo_root} at "
            f"{start_point}" + (f": {error}" if error else "")
        )
    return True


def _story_base_branch(
    conn: sqlite3.Connection, task_id: str, *, board: Optional[str] = None
) -> Optional[str]:
    """The base branch a v2 story's worktree should branch off, or ``None``.

    ``None`` (legacy behavior, base defaults to ``HEAD``) unless the board has
    opted into ``handoff_v2`` AND the task has explicit Epic membership.
    """
    meta = product_board_metadata(board)
    if not _handoff_v2_enabled(meta):
        return None
    epic_id = epic_id_for_task(conn, task_id)
    if epic_id is None:
        return None
    return epic_branch_for(epic_id)


def _dependency_source_base(
    conn: sqlite3.Connection, task: Task, repo_root: Path
) -> Optional[str]:
    """Return the common source-receipt commit for a Default-board child."""
    required: list[tuple[str, str]] = []
    candidates_from_forbidden: list[tuple[str, str]] = []
    for parent_id in parent_ids(conn, task.id):
        row = conn.execute(
            "SELECT t.status, t.source_commit_required, t.source_commit_forbidden, r.metadata "
            "FROM tasks t LEFT JOIN task_runs r ON r.id = ("
            "SELECT id FROM task_runs WHERE task_id = t.id AND ended_at IS NOT NULL "
            "AND outcome = 'completed' "
            "ORDER BY id DESC LIMIT 1) "
            "WHERE t.id = ?",
            (parent_id,),
        ).fetchone()
        if row is None or not (row["source_commit_required"] or row["source_commit_forbidden"]):
            continue
        if row["status"] != "done":
            raise RuntimeError(f"required source parent {parent_id} is not done")
        try:
            receipt = (json.loads(row["metadata"] or "{}").get(
                "source_completion_receipt"
            ))
        except (TypeError, ValueError, AttributeError):
            receipt = None
        if row["source_commit_forbidden"]:
            try:
                sha = (json.loads(row["metadata"] or "{}").get("candidate_sha"))
            except (TypeError, ValueError, AttributeError):
                sha = None
            if isinstance(sha, str) and sha.strip():
                candidates_from_forbidden.append((parent_id, sha.strip()))
            continue
        sha = receipt.get("commit_sha") if isinstance(receipt, dict) else None
        if not isinstance(sha, str) or not sha.strip():
            raise RuntimeError(f"required source parent {parent_id} has no completion receipt")
        required.append((parent_id, sha.strip()))
    evidence = required + candidates_from_forbidden
    if not evidence:
        return None

    resolved: list[tuple[str, str]] = []
    for parent_id, sha in evidence:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"{sha}^{{commit}}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        resolved_sha = (result.stdout or "").strip()
        if result.returncode != 0 or len(resolved_sha) != 40:
            raise RuntimeError(f"source parent {parent_id} evidence is foreign or invalid")
        resolved.append((parent_id, resolved_sha))
    resolved_shas = list(dict.fromkeys(sha for _, sha in resolved))
    candidates = []
    for candidate in resolved_shas:
        if all(
            subprocess.run(
                ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", other, candidate],
                capture_output=True, timeout=30, check=False,
            ).returncode == 0
            for other in resolved_shas
        ):
            candidates.append(candidate)
    if len(candidates) != 1:
        raise RuntimeError("required source parent receipts diverge")
    return candidates[0]


#: Durable record of the exact commit an epic base branch was created at.
#: Written to the epic's own event stream, so the base survives branch
#: cleanup and re-cloning — local refs are not evidence of history.
EPIC_BASE_PINNED_EVENT = "epic_base_pinned"


def _record_epic_base_pin(
    conn: sqlite3.Connection, epic_id: str, branch: str, base_sha: str
) -> None:
    """Persist the epic base SHA, unless an identical pin is already recorded."""
    existing = _epic_base_pinned_sha(conn, epic_id)
    if existing == base_sha:
        return
    with write_txn(conn):
        _append_event(
            conn, epic_id, EPIC_BASE_PINNED_EVENT,
            {"branch": branch, "base_sha": base_sha},
        )


def _epic_base_pinned_sha(
    conn: sqlite3.Connection, epic_id: str
) -> Optional[str]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (epic_id, EPIC_BASE_PINNED_EVENT),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        sha = str((json.loads(row["payload"]) or {}).get("base_sha") or "").strip()
    except Exception:
        return None
    return sha or None


def _latest_epic_integration_sha(
    conn: sqlite3.Connection, epic_id: str
) -> Optional[str]:
    """Newest successful story integration tip for this epic, if any.

    A story integrated into the epic advances its branch, so the last
    integration is the epic base every later sibling must branch from —
    a strictly better answer than the original pin once one exists.
    """
    members = list_epic_members(conn, epic_id)
    if not members:
        return None
    placeholders = ",".join("?" for _ in members)
    rows = conn.execute(
        f"SELECT payload FROM task_events WHERE kind = 'story_integrated_to_epic' "  # noqa: S608 — placeholders only
        f"AND task_id IN ({placeholders}) ORDER BY id DESC",
        tuple(members),
    ).fetchall()
    target = epic_branch_for(epic_id)
    for row in rows:
        if not row["payload"]:
            continue
        try:
            payload = json.loads(row["payload"]) or {}
        except Exception:
            continue
        if payload.get("target_branch") != target:
            continue
        sha = str(payload.get("candidate_sha") or "").strip()
        if sha:
            return sha
    return None


def _epic_has_materialization_history(
    conn: sqlite3.Connection, epic_id: str, *, excluding: Optional[str] = None
) -> bool:
    """Whether this epic materialized anything before ``excluding``, from the DB.

    Deliberately not derived from local Git refs: a re-clone or a branch
    cleanup removes those, and a mature epic would then look brand new and
    pin its base to whatever ``HEAD`` happens to be.

    ``excluding`` is the member currently being materialized. The dispatcher
    claims a card — creating its run row — before resolving its workspace, so
    without this the very first story of a fresh epic would read its own run
    as prior history and fail closed on every new epic.
    """
    if _epic_base_pinned_sha(conn, epic_id) is not None:
        return True
    members = [m for m in list_epic_members(conn, epic_id) if m != excluding]
    if not members:
        return False
    placeholders = ",".join("?" for _ in members)
    row = conn.execute(
        f"SELECT 1 FROM task_runs WHERE task_id IN ({placeholders}) LIMIT 1",  # noqa: S608 — placeholders only
        tuple(members),
    ).fetchone()
    return row is not None


def _epic_base_start_point(
    conn: Optional[sqlite3.Connection], task: Task, repo_root: Path
) -> tuple[Optional[str], Optional[str]]:
    """Resolve ``(epic_id, start_point)`` for ``task``'s missing epic base.

    ``start_point`` is ``None`` when no commit can be established honestly —
    the caller must then fail closed rather than guess.
    """
    if conn is None:
        return None, None
    epic_id = epic_id_for_task(conn, task.id)
    if epic_id is None:
        return None, None
    contract = repository_contract_for_board(
        _board_slug_for_connection(conn), repo_root=repo_root
    )
    recovered = (
        _latest_epic_integration_sha(conn, epic_id)
        or _epic_base_pinned_sha(conn, epic_id)
    )
    if recovered:
        return epic_id, recovered
    if _epic_has_materialization_history(conn, epic_id, excluding=task.id):
        return epic_id, None
    if contract is not None:
        return epic_id, resolve_commit(repo_root, contract.base_ref)
    # Legacy boards without a repository policy retain their historical
    # behavior. Configured boards never consult ambient checkout state.
    return epic_id, _git_head_sha(repo_root)


def _is_epic_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Identify an Epic only from its explicit persisted kind."""

    row = conn.execute(
        "SELECT work_item_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return bool(row is not None and row["work_item_kind"] == "epic")


def _ensure_git_worktree(
    repo_root: Path, target: Path, branch_name: str, *, base: str = "HEAD"
) -> bool:
    """Materialize ``target`` as a linked git worktree under ``repo_root``.

    ``base`` is the ref a brand-new ``branch_name`` is created from (default
    ``HEAD``, i.e. legacy behavior). Callers threading an epic branch as
    ``base`` are responsible for ensuring it exists first (see
    :func:`_ensure_epic_branch`).
    """
    target = target.expanduser()
    repo_common = _git_common_dir(repo_root)
    if target.exists() and repo_common is not None:
        target_common = _git_common_dir(target)
        if target_common == repo_common:
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if _git_branch_exists(repo_root, branch_name):
        cmd = ["git", "-C", str(repo_root), "worktree", "add", str(target), branch_name]
    else:
        cmd = [
            "git", "-C", str(repo_root), "worktree", "add", "-b", branch_name,
            str(target), base,
        ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git worktree add failed for {target} on branch {branch_name}: {stderr}"
        )
    return True


def _primary_checkout_root(path: Path) -> Path:
    """Resolve the primary checkout shared by a linked worktree."""
    common_dir = _git_common_dir(path)
    if common_dir is not None and common_dir.name == ".git":
        return common_dir.parent.resolve(strict=False)
    return (_git_toplevel(path) or path).expanduser().resolve(strict=False)


def _provision_node_dependencies(primary_root: Path, worktree_root: Path) -> None:
    """Provision Node dependencies before a worker can claim the worktree."""
    from hermes_cli.worktree_dependencies import provision_node_dependencies

    provision_node_dependencies(primary_root, worktree_root)


def _cleanup_provisioned_node_dependencies(worktree_root: Path) -> None:
    """Remove dispatcher-provisioned Node dependencies from a worktree."""
    from hermes_cli.worktree_dependencies import (
        cleanup_provisioned_node_dependencies,
    )

    cleanup_provisioned_node_dependencies(worktree_root)


def _worktree_has_other_running_consumer(
    conn: sqlite3.Connection, task_id: str, worktree_root: Path
) -> bool:
    """Return whether another claimed task currently uses this worktree."""
    target = worktree_root.expanduser().resolve(strict=False)
    rows = conn.execute(
        "SELECT id, workspace_path FROM tasks "
        "WHERE id != ? AND workspace_kind = 'worktree' AND status = 'running' "
        "AND workspace_path IS NOT NULL",
        (task_id,),
    ).fetchall()
    for row in rows:
        try:
            other = Path(row["workspace_path"]).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError, TypeError):
            continue
        if other == target:
            return True
    return False


def _assert_worktree_has_no_other_running_consumer(
    conn: Optional[sqlite3.Connection], task_id: Optional[str], worktree_root: Path
) -> None:
    if conn is None or task_id is None:
        return
    if _worktree_has_other_running_consumer(conn, task_id, worktree_root):
        raise RuntimeError(
            f"worktree dependency provisioning refused for {worktree_root}: "
            "another running task is using this checkout"
        )


def _materialize_worktree_with_dependencies(
    repo_root: Path,
    target: Path,
    branch_name: str,
    *,
    base: str,
    conn: Optional[sqlite3.Connection] = None,
    task_id: Optional[str] = None,
) -> None:
    """Create/reuse a linked worktree and provision it before spawning."""
    created = _ensure_git_worktree(repo_root, target, branch_name, base=base)
    try:
        _assert_worktree_has_no_other_running_consumer(conn, task_id, target)
        _provision_node_dependencies(_primary_checkout_root(repo_root), target)
    except Exception as provision_exc:
        if created:
            try:
                removed = subprocess.run(
                    [
                        "git", "-C", str(repo_root), "worktree", "remove",
                        "--force", str(target),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as cleanup_exc:
                raise RuntimeError(
                    f"{provision_exc}; failed to remove new worktree {target}: "
                    f"{cleanup_exc}"
                ) from provision_exc
            if removed.returncode != 0:
                detail = (removed.stderr or removed.stdout or "").strip()
                raise RuntimeError(
                    f"{provision_exc}; failed to remove new worktree {target}: "
                    f"{detail or f'exit {removed.returncode}'}"
                ) from provision_exc
        raise


def _resolve_worktree_workspace(
    task: Task,
    *,
    board: Optional[str] = None,
    base_branch: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[Path, str]:
    """Resolve + materialize a linked git worktree for ``task``.

    When ``task.workspace_path`` is unset, the anchor is the board's
    ``default_workdir`` (a persistent project checkout). This keeps every
    worktree task under a meaningful, board-owned repo — ``<repo>/.worktrees/
    <task-id>`` — instead of silently landing under the dispatcher's current
    working directory (which is whatever directory the gateway happened to be
    launched from, e.g. the Hermes checkout). If no anchor is configured
    anywhere, we fail loudly rather than guess.

    ``base_branch`` is the ref a brand-new worktree branch should be created
    from -- e.g. an epic's integration branch, so a story's worktree contains a
    previously-integrated sibling story's code. When set, the epic branch is
    created first (off the repo's current ``HEAD``) if it doesn't exist yet.

    Callers do not have to supply it: when ``conn`` is available and
    ``base_branch`` is omitted it is derived here via
    :func:`_story_base_branch`, which returns ``None`` (legacy behavior, branch
    off ``HEAD``) for anything that is not a handoff_v2 story with explicit
    Epic membership. Deriving it at this single shared seam is deliberate --
    only ``_spawn_one_v2`` used to pass it, so a story dispatched by the ready
    loop, the review loop, or any other ``resolve_workspace`` caller
    materialized with its epic base missing and Review target preparation then
    failed on an unresolvable ``git merge-base`` (2026-07-30 epic
    ``t_c29de776``).
    """
    branch_name = (task.branch_name or "").strip() or f"wt/{task.id}"
    if base_branch is None and conn is not None:
        base_branch = _story_base_branch(conn, task.id, board=board)
    base = base_branch or "HEAD"

    def resolve_default_base(repo_root: Path) -> None:
        nonlocal base_branch, base
        if (
            base_branch is None
            and conn is not None
            and not _handoff_v2_enabled(product_board_metadata(board))
        ):
            base_branch = _dependency_source_base(conn, task, repo_root)
            base = base_branch or "HEAD"

    def ensure_epic_base(repo_root: Path) -> None:
        if (
            base_branch is None
            or not _handoff_v2_enabled(product_board_metadata(board))
            or _git_branch_exists(repo_root, base_branch)
        ):
            return
        epic_id, start_point = _epic_base_start_point(conn, task, repo_root)
        if _ensure_epic_branch(repo_root, base_branch, start_point=start_point):
            # Persist the base the moment it exists, so a later branch
            # cleanup or fresh clone can recover it from the ledger.
            if conn is not None and epic_id and start_point:
                _record_epic_base_pin(conn, epic_id, base_branch, start_point)

    if not task.workspace_path:
        # Anchor on the board's configured default_workdir, not Path.cwd().
        # The dispatcher's CWD is incidental (gateway launch dir) and using it
        # scatters worktrees under whatever repo the gateway started in.
        board_slug = board if board else get_current_board()
        board_default = (read_board_metadata(board_slug).get("default_workdir") or "").strip()
        if not board_default:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but no workspace_path, "
                f"and board {board_slug!r} has no default_workdir set. Set a board "
                "default workdir (a git repo) or create the task with "
                "--workspace worktree:<absolute-repo-path>."
            )
        anchor = Path(board_default).expanduser()
        if not anchor.is_absolute():
            raise ValueError(
                f"board {board_slug!r} default_workdir {board_default!r} is not "
                "absolute; use an absolute path to a git repo"
            )
        repo_root = _git_toplevel(anchor)
        if repo_root is None:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but board "
                f"{board_slug!r} default_workdir {board_default!r} is not inside a git repo"
            )
        target = repo_root / ".worktrees" / task.id
        resolve_default_base(repo_root)
        ensure_epic_base(repo_root)
        _materialize_worktree_with_dependencies(
            repo_root,
            target,
            branch_name,
            base=base,
            conn=conn,
            task_id=task.id,
        )
        return target, branch_name

    requested = Path(task.workspace_path).expanduser()
    if not requested.is_absolute():
        raise ValueError(
            f"task {task.id} has non-absolute worktree path "
            f"{task.workspace_path!r}; use an absolute path"
        )
    requested_resolved = requested.resolve(strict=False)

    if requested.exists() and _is_linked_worktree_checkout(requested):
        actual_branch = _git_current_branch(requested)
        if actual_branch == branch_name:
            _assert_worktree_has_no_other_running_consumer(
                conn, task.id, requested
            )
            primary_root = _primary_checkout_root(requested)
            ensure_epic_base(primary_root)
            _provision_node_dependencies(primary_root, requested)
            return requested_resolved, actual_branch
        # The requested path is an existing checkout of a DIFFERENT
        # task's branch. Decompose children inherit the root's
        # workspace_path verbatim, so siblings all point here; reusing
        # the checkout as-is would run this task on the other task's
        # branch — silent cross-task provenance corruption, and unsafe
        # when siblings run concurrently. Fall back to a fresh worktree
        # of our own under the same repo.
        fallback_root = _repo_root_for_worktree_target(requested.parent)
        if fallback_root is not None:
            fallback = fallback_root / ".worktrees" / task.id
            if fallback.resolve(strict=False) != requested_resolved:
                resolve_default_base(fallback_root)
                ensure_epic_base(fallback_root)
                _materialize_worktree_with_dependencies(
                    fallback_root,
                    fallback,
                    branch_name,
                    base=base,
                    conn=conn,
                    task_id=task.id,
                )
                return fallback.resolve(strict=False), branch_name
        # No repo to anchor a fallback on (or the occupied path IS this
        # task's own canonical worktree): keep the legacy reuse rather
        # than failing dispatch.
        return requested_resolved, actual_branch or branch_name

    repo_root = _git_toplevel(requested)
    if repo_root is not None and requested_resolved == repo_root:
        target = repo_root / ".worktrees" / task.id
        resolve_default_base(repo_root)
        ensure_epic_base(repo_root)
        _materialize_worktree_with_dependencies(
            repo_root,
            target,
            branch_name,
            base=base,
            conn=conn,
            task_id=task.id,
        )
        return target, branch_name

    repo_root = _repo_root_for_worktree_target(requested.parent)
    if repo_root is None:
        raise ValueError(
            f"task {task.id} worktree path {task.workspace_path!r} is not inside a git repo "
            "and does not point at a git repo root"
        )
    resolve_default_base(repo_root)
    ensure_epic_base(repo_root)
    _materialize_worktree_with_dependencies(
        repo_root,
        requested,
        branch_name,
        base=base,
        conn=conn,
        task_id=task.id,
    )
    return requested, branch_name


@dataclass(frozen=True)
class IntegrationCandidate:
    pre_sha: str
    candidate_sha: str
    source_branch: str
    source_sha: str
    target_branch: str
    target_worktree: Optional[Path]
    scratch_worktree: Path
    repo_root: Path
    candidate_ref: str
    verification_result: Optional[VerificationResult] = None


_RECONCILE_INTEGRATION_VERIFY_UNSET = object()


@dataclass(frozen=True)
class ReleaseResult:
    released: bool
    status: str
    integration_event_id: Optional[int] = None
    integration_sha: Optional[str] = None
    deployment_policy_event_id: Optional[int] = None
    deployment_record_event_id: Optional[int] = None


class IntegrationCandidateError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "integration_error",
        scratch_worktree: Optional[Path] = None,
        verification_result: Optional[VerificationResult] = None,
    ):
        super().__init__(message)
        self.code = str(code or "integration_error")
        self.scratch_worktree = scratch_worktree
        self.verification_result = verification_result


def _verification_result_payload(
    result: VerificationResult, *, scope: str, subject_id: str
) -> dict[str, Any]:
    """Serialize bounded repository verification evidence for a task event."""
    try:
        return verification_result_payload(
            result, scope=scope, subject_id=subject_id
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("passed verification result cannot produce receipt") from exc


def _verification_needs_attention(result: Optional[VerificationResult]) -> bool:
    return result is not None and result.status in {
        "configuration_error",
        "infrastructure_error",
    }


def _run_or_reuse_configured_verification(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    candidate_path: Path,
    source_sha: str,
    candidate_sha: str,
    contract: RepositoryContract,
    profile_name: str,
    gate_kind: str,
) -> VerificationResult:
    profile = contract.verification.get(profile_name)
    expected_key = build_verification_receipt_key(
        profile, candidate_path, candidate_sha=candidate_sha,
        contract_digest=contract.digest,
        generated_policy_digest=contract.generated_policy_digest,
        gate_kind=gate_kind, profile_name=profile_name,
    )
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC",
        (task_id, "repository_verification"),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else None
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            continue
        receipt = verification_receipt_from_payload(payload)
        if (receipt is None or receipt.key.digest != expected_key.digest
                or payload.get("scope") != gate_kind
                or payload.get("subject_id") != task_id
                or payload.get("status") != "passed"):
            continue
        steps = tuple(
            VerificationStepResult(
                argv=tuple(step["argv"]), workdir=PurePosixPath(step["workdir"]),
                status=step["status"], returncode=step["returncode"],
                duration_seconds=step["duration_seconds"],
                stdout_tail=step["stdout_tail"], stderr_tail=step["stderr_tail"],
                error=step["error"],
            ) for step in payload.get("steps", []) if isinstance(step, dict)
        )
        return VerificationResult(
            status="passed", source_sha=source_sha, candidate_sha=candidate_sha,
            contract_digest=contract.digest, profile=profile_name, steps=steps,
            key=expected_key, error=None, reused=True,
        )
    result = run_verification(
        profile, candidate_path, source_sha=source_sha, candidate_sha=candidate_sha,
        contract_digest=contract.digest, scope=gate_kind, subject_id=task_id,
        profile_name=profile_name, generated_policy_digest=contract.generated_policy_digest,
    )
    with write_txn(conn):
        _append_event(conn, task_id, "repository_verification",
                      _verification_result_payload(result, scope=gate_kind, subject_id=task_id))
    return result


def _integration_git(
    cwd: Path, args: list[str], *, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise IntegrationCandidateError(
            f"git command timed out: {args[0]}", code="timeout"
        ) from exc
    except OSError as exc:
        raise IntegrationCandidateError(
            f"git command failed: {args[0]}", code="io_error"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise IntegrationCandidateError(
            f"git command failed: {args[0]}", code="command_failed"
        ) from exc


def _checked_out_branch_worktree(repo_root: Path, branch: str) -> Optional[Path]:
    listed = _integration_git(repo_root, ["worktree", "list", "--porcelain"])
    if listed.returncode != 0:
        raise IntegrationCandidateError(
            "could not list repository worktrees", code="command_failed"
        )
    wanted = f"refs/heads/{branch}"
    for block in (listed.stdout or "").strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if fields.get("branch") == wanted and fields.get("worktree"):
            return Path(fields["worktree"]).resolve()
    return None


def _worktree_is_clean(path: Path) -> bool:
    status = _integration_git(path, ["status", "--porcelain", "--untracked-files=all"])
    return status.returncode == 0 and not (status.stdout or "").strip()


def _remove_clean_integration_worktree(repo_root: Path, scratch: Path) -> None:
    if not _worktree_is_clean(scratch):
        raise IntegrationCandidateError(
            f"scratch worktree is dirty; preserved at {scratch}",
            code="ownership_changed",
            scratch_worktree=scratch,
        )
    removed = _integration_git(repo_root, ["worktree", "remove", str(scratch)])
    if removed.returncode != 0:
        raise IntegrationCandidateError(
            f"could not remove scratch worktree; preserved at {scratch}",
            code="command_failed",
            scratch_worktree=scratch,
        )


def _build_verified_merge_candidate(
    repo_root: Path,
    target_branch: str,
    source_branch: str,
    message: str,
    candidate_verify_fn: Optional[Callable[[Path], bool]] = None,
    *,
    expected_source_sha: Optional[str] = None,
    allow_empty_contribution: bool = False,
    verification_profile: Optional[VerificationProfile] = None,
    verification_contract_digest: Optional[str] = None,
    verification_scope: str = "story_integration",
    verification_subject_id: str = "",
    verification_profile_name: Optional[str] = None,
    verification_generated_policy_digest: str = "",
    configured_verification_fn: Optional[Callable[[Path, str, str], VerificationResult]] = None,
    candidate_ref_prefix: str = "refs/hermes/integration-candidates/",
) -> IntegrationCandidate:
    repo_root = repo_root.resolve()
    target_worktree = _checked_out_branch_worktree(repo_root, target_branch)
    if target_worktree is not None and not _worktree_is_clean(target_worktree):
        raise IntegrationCandidateError(
            f"target worktree is dirty: {target_worktree}",
            code="ownership_changed",
        )

    source_result = _integration_git(
        repo_root, ["rev-parse", f"refs/heads/{source_branch}"]
    )
    source_sha = (source_result.stdout or "").strip()
    if source_result.returncode != 0 or not source_sha:
        raise IntegrationCandidateError(
            f"could not resolve {source_branch}", code="ref_missing"
        )
    if expected_source_sha is not None and source_sha != expected_source_sha:
        raise IntegrationCandidateError(
            f"source branch moved: {source_branch} no longer matches reviewed SHA",
            code="source_moved",
        )
    approved_source_sha = expected_source_sha or source_sha

    pre_result = _integration_git(repo_root, ["rev-parse", f"refs/heads/{target_branch}"])
    pre_sha = (pre_result.stdout or "").strip()
    if pre_result.returncode != 0 or not pre_sha:
        raise IntegrationCandidateError(
            f"could not resolve {target_branch}", code="ref_missing"
        )

    source_ancestor = _integration_git(
        repo_root,
        ["merge-base", "--is-ancestor", approved_source_sha, pre_sha],
    )
    empty_contribution = source_ancestor.returncode == 0
    if empty_contribution and not allow_empty_contribution:
        raise IntegrationCandidateError("empty contribution", code="source_moved")
    if source_ancestor.returncode not in {0, 1}:
        raise IntegrationCandidateError(
            "could not verify candidate contribution", code="command_failed"
        )

    nonce = secrets.token_hex(6)
    scratch = repo_root / ".worktrees" / f"integration-{nonce}"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    added = _integration_git(
        repo_root, ["worktree", "add", "--detach", str(scratch), pre_sha]
    )
    if added.returncode != 0:
        raise IntegrationCandidateError(
            "could not create integration worktree", code="provisioning_failed"
        )

    if not empty_contribution:
        merged = _integration_git(
            scratch,
            ["merge", "--no-ff", approved_source_sha, "-m", message],
            timeout=900,
        )
        if merged.returncode != 0:
            _integration_git(scratch, ["merge", "--abort"])
            _remove_clean_integration_worktree(repo_root, scratch)
            raise IntegrationCandidateError("merge conflict", code="merge_conflict")

    try:
        _provision_node_dependencies(_primary_checkout_root(repo_root), scratch)
    except Exception as exc:
        _cleanup_provisioned_node_dependencies(scratch)
        _remove_clean_integration_worktree(repo_root, scratch)
        raise IntegrationCandidateError(
            f"candidate dependency provisioning failed: {exc}",
            code="provisioning_failed",
        ) from exc

    candidate_result = _integration_git(scratch, ["rev-parse", "HEAD"])
    candidate_sha = (candidate_result.stdout or "").strip()
    if candidate_result.returncode != 0 or not candidate_sha:
        _cleanup_provisioned_node_dependencies(scratch)
        raise IntegrationCandidateError(
            "could not resolve integration candidate",
            code="ref_missing",
            scratch_worktree=scratch,
        )

    verification_result: Optional[VerificationResult] = None
    if configured_verification_fn is not None:
        configured_result = configured_verification_fn(scratch, approved_source_sha, candidate_sha)
        verification_result = configured_result
        verified = configured_result.status == "passed"
    elif verification_contract_digest is not None and candidate_verify_fn is None:
        configured_result = run_verification(
            verification_profile,
            scratch,
            source_sha=approved_source_sha,
            candidate_sha=candidate_sha,
            contract_digest=verification_contract_digest,
            scope=verification_scope,
            subject_id=verification_subject_id,
            profile_name=verification_profile_name,
            generated_policy_digest=verification_generated_policy_digest,
        )
        verification_result = configured_result
        verified = configured_result.status == "passed"
    elif candidate_verify_fn is None:
        script = scratch / "scripts" / "run_tests.sh"
        if not script.is_file():
            verified = False
        else:
            try:
                result = subprocess.run(
                    ["bash", "scripts/run_tests.sh"],
                    cwd=scratch,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )
                verified = result.returncode == 0
            except Exception:
                verified = False
    else:
        try:
            verified = bool(candidate_verify_fn(scratch))
        except Exception:
            verified = False
    if not verified:
        _cleanup_provisioned_node_dependencies(scratch)
        _remove_clean_integration_worktree(repo_root, scratch)
        failure_code = "verification_failed"
        if verification_result is not None:
            if verification_result.status == "configuration_error":
                if verification_result.error in {"missing_profile", "empty_profile"}:
                    failure_code = "profile_missing"
                elif verification_result.error in {
                    "invalid_command",
                } or str(verification_result.error or "").startswith(
                    "missing_executable:"
                ):
                    failure_code = "command_missing"
                else:
                    failure_code = "profile_invalid"
            elif verification_result.status == "infrastructure_error":
                failure_code = (
                    "timeout"
                    if verification_result.error == "timeout"
                    else "io_error"
                )
        raise IntegrationCandidateError(
            "candidate verification failed"
            if verification_result is None
            else f"candidate verification {verification_result.status}",
            code=failure_code,
            verification_result=verification_result,
        )

    _cleanup_provisioned_node_dependencies(scratch)

    if expected_source_sha is not None:
        current_source = _integration_git(
            repo_root, ["rev-parse", f"refs/heads/{source_branch}"]
        )
        if (
            current_source.returncode != 0
            or (current_source.stdout or "").strip() != expected_source_sha
        ):
            _remove_clean_integration_worktree(repo_root, scratch)
            raise IntegrationCandidateError(
                f"source branch moved: {source_branch} no longer matches reviewed SHA",
                code="source_moved",
            )

    if not _worktree_is_clean(scratch):
        raise IntegrationCandidateError(
            f"scratch worktree is dirty; preserved at {scratch}",
            code="ownership_changed",
            scratch_worktree=scratch,
        )

    if candidate_ref_prefix not in {
        "refs/hermes/integration-candidates/",
        RELEASE_CANDIDATE_REF_PREFIX,
    }:
        raise IntegrationCandidateError(
            "unsupported candidate ref namespace", code="malformed_candidate_ref"
        )
    candidate_ref = f"{candidate_ref_prefix}{nonce}"
    retained = _integration_git(repo_root, ["update-ref", candidate_ref, candidate_sha])
    if retained.returncode != 0:
        raise IntegrationCandidateError(
            "could not retain integration candidate",
            code="command_failed",
            scratch_worktree=scratch,
        )
    _remove_clean_integration_worktree(repo_root, scratch)
    return IntegrationCandidate(
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
        source_branch=source_branch,
        source_sha=approved_source_sha,
        target_branch=target_branch,
        target_worktree=target_worktree,
        scratch_worktree=scratch,
        repo_root=repo_root,
        candidate_ref=candidate_ref,
        verification_result=verification_result,
    )


def _fast_forward_target(candidate: IntegrationCandidate) -> bool:
    target_ref = f"refs/heads/{candidate.target_branch}"
    current = _integration_git(candidate.repo_root, ["rev-parse", target_ref])
    if current.returncode != 0 or (current.stdout or "").strip() != candidate.pre_sha:
        return False

    current_target_worktree = _checked_out_branch_worktree(
        candidate.repo_root, candidate.target_branch
    )
    if current_target_worktree != candidate.target_worktree:
        return False

    if current_target_worktree is not None:
        if not _worktree_is_clean(current_target_worktree):
            return False
        branch = _integration_git(current_target_worktree, ["branch", "--show-current"])
        if branch.returncode != 0 or (branch.stdout or "").strip() != candidate.target_branch:
            return False
        current = _integration_git(candidate.repo_root, ["rev-parse", target_ref])
        if current.returncode != 0 or (current.stdout or "").strip() != candidate.pre_sha:
            return False
        applied = _integration_git(
            current_target_worktree, ["merge", "--ff-only", candidate.candidate_sha]
        )
    else:
        applied = _integration_git(
            candidate.repo_root,
            ["update-ref", target_ref, candidate.candidate_sha, candidate.pre_sha],
        )
    if applied.returncode != 0:
        return False
    applied_ref = _integration_git(candidate.repo_root, ["rev-parse", target_ref])
    if (
        applied_ref.returncode != 0
        or (applied_ref.stdout or "").strip() != candidate.candidate_sha
    ):
        return False
    _integration_git(
        candidate.repo_root,
        ["update-ref", "-d", candidate.candidate_ref, candidate.candidate_sha],
    )
    return True


def _default_epic_verify(
    epic_branch: str, *, board: Optional[str] = None
) -> bool:
    """Run the project's test suite against ``epic_branch`` and report green.

    The legacy (slow) verify path for :func:`epic_ready`. Resolves the active
    board's repo root the same way :func:`_resolve_worktree_workspace` does
    (board ``default_workdir`` -> :func:`_git_toplevel`), materializes/locates
    a worktree checked out to ``epic_branch``, then shells out to
    ``scripts/run_tests.sh`` in that worktree. Boards with a repository
    contract refuse this legacy fallback until the configured verification
    service is used.

    Defensive: any exception, or a missing ``run_tests.sh``, means "not
    green" -- this never raises. Exercised by the dogfood checkpoint, not by
    unit tests (which inject ``verify_fn`` instead).
    """
    try:
        board_default = (
            read_board_metadata(board or get_current_board()).get("default_workdir") or ""
        ).strip()
        if not board_default:
            return False
        repo_root = _git_toplevel(Path(board_default).expanduser())
        if repo_root is None:
            return False
        contract = repository_contract_for_board(board, repo_root=repo_root)
        _ensure_epic_branch(repo_root, epic_branch, start_point=None)
        target = repo_root / ".worktrees" / f"epic-verify-{epic_branch.replace('/', '-')}"
        from hermes_cli.worktree_dependencies import _acquire_project_lock

        # Provisioning's per-project locks protect mutation only. This
        # separate lease serializes the entire deterministic verification
        # worktree lifetime so concurrent verifiers cannot replace or clean
        # dependencies while another suite is running.
        verify_lease = _acquire_project_lock(target / ".hermes-epic-verification")
        try:
            _materialize_worktree_with_dependencies(
                repo_root, target, epic_branch, base="HEAD"
            )
            try:
                if contract is not None:
                    source_sha = _git_ref_sha(repo_root, epic_branch)
                    candidate_result = _integration_git(target, ["rev-parse", "HEAD"])
                    candidate_sha = (candidate_result.stdout or "").strip()
                    if not source_sha or candidate_result.returncode != 0 or not candidate_sha:
                        return False
                    result = run_verification(
                        contract.verification.get("epic_release"),
                        target,
                        source_sha=source_sha,
                        candidate_sha=candidate_sha,
                        contract_digest=contract.digest,
                        scope="epic_release",
                        subject_id=epic_branch,
                        profile_name="epic_release",
                    )
                    return result.status == "passed"
                script = target / "scripts" / "run_tests.sh"
                if not script.exists():
                    return False
                result = subprocess.run(
                    ["bash", "scripts/run_tests.sh"],
                    cwd=target,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )
                return result.returncode == 0
            finally:
                _cleanup_provisioned_node_dependencies(target)
        finally:
            verify_lease.release()
    except Exception:
        return False


def epic_readiness(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
) -> EpicReadiness:
    """Return the strict, read-only fact derivation for one governed Epic."""

    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta) or not _is_epic_task(conn, epic_id):
        return EpicReadiness(epic_id, None, (), ("not_governed_epic",))
    try:
        contract = repository_contract_for_metadata(meta)
        if contract is None:
            return EpicReadiness(epic_id, None, (), ("missing_repository_contract",))
        epic_branch = epic_branch_for(epic_id)
        epic_tip_sha = resolve_commit(
            contract.repo_root, f"refs/heads/{epic_branch}"
        )

        def current_terminal_source(story_id: str) -> Optional[EpicTerminalSource]:
            terminal_runs = _terminal_run_records(conn, story_id)
            approved = latest_review_authority(terminal_runs)
            if approved is None:
                return None
            passed = latest_test_authority(terminal_runs, approved.source_sha)
            if passed is None:
                return None
            try:
                eligibility = candidate_eligibility(
                    contract.repo_root,
                    approved,
                    passed,
                )
            except CandidateEligibilityError:
                return EpicTerminalSource(approved.source_sha, False)
            return EpicTerminalSource(
                eligibility.source_sha,
                eligibility.non_empty,
            )

        return derive_epic_readiness(
            conn,
            epic_id,
            epic_tip_sha=epic_tip_sha,
            current_terminal_source=current_terminal_source,
            commit_contains=lambda descendant, ancestor: commit_contains(
                contract.repo_root,
                descendant_sha=descendant,
                ancestor_sha=ancestor,
            ),
        )
    except (RepositoryConfigurationError, OSError, ValueError):
        return EpicReadiness(epic_id, None, (), ("repository_unavailable",))


def epic_ready(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    verify_fn: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Whether ``epic_id`` has exact current facts and a green local suite.

    Repository-governed boards derive the cheap gate from current membership,
    terminal Review authority, integration intents/facts, and commit ancestry.
    Older handoff-v2 boards without repository policy retain their legacy
    all-members-done gate.
    """

    meta = product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return False
    if not _is_epic_task(conn, epic_id):
        return False
    children = list_epic_members(conn, epic_id)
    if not children:
        return False
    if "repository" in meta:
        if not epic_readiness(conn, epic_id, board=board, board_meta=meta).ready:
            return False
    else:
        for child_id in children:
            child = get_task(conn, child_id)
            if child is None or child.status != "done":
                return False
    verify = verify_fn or (
        lambda branch: _default_epic_verify(branch, board=board)
    )
    return bool(verify(epic_branch_for(epic_id)))


_EPIC_RELEASE_ACTIVE_STATUSES = ("awaiting_push", "ci_pending", "ci_failed")
_EPIC_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class _EpicReleaseInputs:
    epic_tip_sha: str
    target_branch: str
    target_pre_sha: str
    contract_digest: str
    members: tuple[tuple[str, str, str, int], ...]


def _epic_release_input_evidence(inputs: _EpicReleaseInputs) -> dict[str, Any]:
    return {
        "epic_tip_sha": inputs.epic_tip_sha,
        "target_branch": inputs.target_branch,
        "target_pre_sha": inputs.target_pre_sha,
        "contract_digest": inputs.contract_digest,
        "members": [
            {
                "story_id": story_id,
                "source_sha": source_sha,
                "candidate_sha": candidate_sha,
                "integrated_at": integrated_at,
            }
            for story_id, source_sha, candidate_sha, integrated_at in inputs.members
        ],
    }


def _epic_release_inputs(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str],
    board_meta: Optional[dict],
) -> tuple[_EpicReleaseInputs, RepositoryContract, str]:
    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta) or not _is_epic_task(conn, epic_id):
        raise EpicReleasePreparationError(
            "not_governed_epic", {"epic_id": epic_id}
        )
    try:
        contract = repository_contract_for_metadata(meta)
    except (RepositoryConfigurationError, OSError, ValueError) as exc:
        raise EpicReleasePreparationError(
            "repository_unavailable", {"epic_id": epic_id, "error": str(exc)}
        ) from exc
    if contract is None:
        raise EpicReleasePreparationError(
            "missing_repository_contract", {"epic_id": epic_id}
        )
    if "epic_release" not in contract.verification:
        raise EpicReleasePreparationError(
            "missing_epic_release_profile", {"epic_id": epic_id}
        )

    readiness = epic_readiness(
        conn,
        epic_id,
        board=board,
        board_meta=meta,
    )
    if not readiness.ready or readiness.epic_tip_sha is None:
        raise EpicReleasePreparationError(
            "not_ready",
            {"epic_id": epic_id, "blockers": list(readiness.blockers)},
        )

    epic_branch = epic_branch_for(epic_id)
    try:
        epic_tip_sha = resolve_commit(
            contract.repo_root, f"refs/heads/{epic_branch}"
        )
        target_pre_sha = resolve_commit(
            contract.repo_root, f"refs/heads/{contract.target_branch}"
        )
    except (RepositoryConfigurationError, OSError, ValueError) as exc:
        raise EpicReleasePreparationError(
            "repository_unavailable", {"epic_id": epic_id, "error": str(exc)}
        ) from exc
    if readiness.epic_tip_sha != epic_tip_sha:
        raise EpicReleasePreparationError(
            "readiness_tip_mismatch",
            {
                "epic_id": epic_id,
                "readiness_epic_tip_sha": readiness.epic_tip_sha,
                "epic_tip_sha": epic_tip_sha,
            },
        )

    members = tuple(
        sorted(
            (
                member.story_id,
                member.source_sha,
                member.candidate_sha,
                int(member.integrated_at),
            )
            for member in readiness.members
        )
    )
    if not members:
        raise EpicReleasePreparationError(
            "not_ready", {"epic_id": epic_id, "blockers": ["no_members"]}
        )
    return (
        _EpicReleaseInputs(
            epic_tip_sha=epic_tip_sha,
            target_branch=contract.target_branch,
            target_pre_sha=target_pre_sha,
            contract_digest=contract.digest,
            members=members,
        ),
        contract,
        epic_branch,
    )


def _epic_release_active_row(
    conn: sqlite3.Connection, epic_id: str
) -> Optional[sqlite3.Row]:
    placeholders = ",".join("?" for _ in _EPIC_RELEASE_ACTIVE_STATUSES)
    return conn.execute(
        f"SELECT * FROM epic_release_snapshots WHERE epic_id=? "
        f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (epic_id, *_EPIC_RELEASE_ACTIVE_STATUSES),
    ).fetchone()


def _epic_release_snapshot_members(
    conn: sqlite3.Connection, snapshot_id: int
) -> tuple[EpicReleaseMember, ...]:
    rows = conn.execute(
        "SELECT * FROM epic_release_members WHERE snapshot_id=? ORDER BY story_id",
        (snapshot_id,),
    ).fetchall()
    return tuple(epic_release_member_from_row(row) for row in rows)


def _epic_release_snapshot_mismatch_evidence(
    conn: sqlite3.Connection,
    snapshot: EpicReleaseSnapshot,
    *,
    epic_id: str,
    inputs: _EpicReleaseInputs,
) -> dict[str, Any]:
    """Accumulate typed drift evidence for one active snapshot.

    An empty result means the snapshot still matches every current input
    exactly.  Each key records the per-field snapshot value alongside the
    current authority it no longer matches.
    """

    evidence: dict[str, Any] = {}
    if snapshot.epic_id != epic_id:
        evidence["epic_id"] = {
            "snapshot": snapshot.epic_id,
            "current": epic_id,
        }
    if snapshot.epic_tip_sha != inputs.epic_tip_sha:
        evidence["epic_tip_sha"] = {
            "snapshot": snapshot.epic_tip_sha,
            "current": inputs.epic_tip_sha,
        }
    if snapshot.target_branch != inputs.target_branch:
        evidence["target_branch"] = {
            "snapshot": snapshot.target_branch,
            "current": inputs.target_branch,
        }
    if snapshot.target_pre_sha != inputs.target_pre_sha:
        evidence["target_pre_sha"] = {
            "snapshot": snapshot.target_pre_sha,
            "current": inputs.target_pre_sha,
        }
    if snapshot.repository_contract_digest != inputs.contract_digest:
        evidence["repository_contract_digest"] = {
            "snapshot": snapshot.repository_contract_digest,
            "current": inputs.contract_digest,
        }
    if snapshot.status not in _EPIC_RELEASE_ACTIVE_STATUSES:
        evidence["status"] = {"snapshot": snapshot.status}
    try:
        validate_release_candidate_ref(snapshot.candidate_ref)
    except RepositoryConfigurationError:
        evidence["candidate_ref"] = {"snapshot": snapshot.candidate_ref}
    expected_members = tuple(
        EpicReleaseMember(
            snapshot_id=snapshot.id,
            epic_id=epic_id,
            story_id=story_id,
            source_sha=source_sha,
            candidate_sha=candidate_sha,
            integrated_at=integrated_at,
        )
        for story_id, source_sha, candidate_sha, integrated_at in inputs.members
    )
    try:
        current_members = _epic_release_snapshot_members(conn, snapshot.id)
    except ValueError as exc:
        evidence["members"] = {"error": str(exc)}
    else:
        if current_members != expected_members:
            evidence["members"] = {
                "snapshot": [
                    {
                        "story_id": member.story_id,
                        "source_sha": member.source_sha,
                        "candidate_sha": member.candidate_sha,
                        "integrated_at": member.integrated_at,
                    }
                    for member in current_members
                ],
                "current": [
                    {
                        "story_id": member.story_id,
                        "source_sha": member.source_sha,
                        "candidate_sha": member.candidate_sha,
                        "integrated_at": member.integrated_at,
                    }
                    for member in expected_members
                ],
            }
    event = conn.execute(
        "SELECT task_id, kind, payload FROM task_events WHERE id=?",
        (snapshot.aggregate_verification_event_id,),
    ).fetchone()
    receipt_evidence: dict[str, Any] = {}
    if (
        event is None
        or event["task_id"] != epic_id
        or event["kind"] != "repository_verification"
    ):
        receipt_evidence["event"] = {
            "task_id": event["task_id"] if event is not None else None,
            "kind": event["kind"] if event is not None else None,
        }
    else:
        try:
            payload = json.loads(event["payload"]) if event["payload"] else None
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, Mapping) or not verification_receipt_matches(
            payload,
            source_sha=inputs.epic_tip_sha,
            candidate_sha=snapshot.release_candidate_sha,
            contract_digest=inputs.contract_digest,
            gate_kind="epic_release",
            subject_id=epic_id,
            profile_name="epic_release",
        ):
            receipt_evidence["receipt"] = {"matches": False}
    if receipt_evidence:
        evidence["aggregate_verification_event"] = receipt_evidence
    return evidence


def _epic_release_snapshot_matches(
    conn: sqlite3.Connection,
    snapshot: EpicReleaseSnapshot,
    *,
    epic_id: str,
    inputs: _EpicReleaseInputs,
) -> bool:
    return not _epic_release_snapshot_mismatch_evidence(
        conn, snapshot, epic_id=epic_id, inputs=inputs
    )


def _epic_release_active_or_refuse(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    inputs: _EpicReleaseInputs,
) -> Optional[EpicReleaseSnapshot]:
    row = _epic_release_active_row(conn, epic_id)
    if row is None:
        return None
    try:
        snapshot = epic_release_snapshot_from_row(row)
    except ValueError as exc:
        raise EpicReleasePreparationError(
            "active_snapshot_mismatch",
            {"epic_id": epic_id, "snapshot_id": row["id"], "error": str(exc)},
        ) from exc
    if not _epic_release_snapshot_matches(
        conn, snapshot, epic_id=epic_id, inputs=inputs
    ):
        raise EpicReleasePreparationError(
            "active_snapshot_mismatch",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "expected": _epic_release_input_evidence(inputs),
                "actual": {
                    "epic_tip_sha": snapshot.epic_tip_sha,
                    "target_branch": snapshot.target_branch,
                    "target_pre_sha": snapshot.target_pre_sha,
                    "release_candidate_sha": snapshot.release_candidate_sha,
                    "candidate_ref": snapshot.candidate_ref,
                    "repository_contract_digest": snapshot.repository_contract_digest,
                    "status": snapshot.status,
                },
            },
        )
    return snapshot


def _validate_epic_release_candidate(
    candidate: object,
    *,
    contract: RepositoryContract,
    inputs: _EpicReleaseInputs,
    epic_branch: str,
    epic_id: str,
) -> tuple[IntegrationCandidate, dict[str, Any]]:
    if not isinstance(candidate, IntegrationCandidate):
        raise EpicReleasePreparationError(
            "candidate_mismatch", {"epic_id": epic_id, "reason": "invalid_candidate"}
        )
    try:
        validate_release_candidate_ref(candidate.candidate_ref)
    except RepositoryConfigurationError as exc:
        raise EpicReleasePreparationError(
            "candidate_ref_mismatch",
            {"epic_id": epic_id, "candidate_ref": candidate.candidate_ref},
        ) from exc
    verification = candidate.verification_result
    if not isinstance(verification, VerificationResult):
        raise EpicReleasePreparationError(
            "candidate_mismatch",
            {"epic_id": epic_id, "reason": "missing_verification"},
        )
    if (
        candidate.repo_root.resolve() != contract.repo_root
        or candidate.target_branch != inputs.target_branch
        or candidate.source_branch != epic_branch
        or candidate.source_sha != inputs.epic_tip_sha
        or candidate.pre_sha != inputs.target_pre_sha
        or _EPIC_RELEASE_SHA_RE.fullmatch(candidate.candidate_sha) is None
        or verification.status != "passed"
        or verification.source_sha != inputs.epic_tip_sha
        or verification.candidate_sha != candidate.candidate_sha
        or verification.contract_digest != contract.digest
        or verification.profile != "epic_release"
    ):
        raise EpicReleasePreparationError(
            "candidate_mismatch",
            {
                "epic_id": epic_id,
                "candidate_sha": candidate.candidate_sha,
                "candidate_ref": candidate.candidate_ref,
            },
        )
    try:
        payload = verification_result_payload(
            verification, scope="epic_release", subject_id=epic_id
        )
    except (TypeError, ValueError) as exc:
        raise EpicReleasePreparationError(
            "verification_mismatch", {"epic_id": epic_id, "error": str(exc)}
        ) from exc
    if not verification_receipt_matches(
        payload,
        source_sha=inputs.epic_tip_sha,
        candidate_sha=candidate.candidate_sha,
        contract_digest=contract.digest,
        gate_kind="epic_release",
        subject_id=epic_id,
        profile_name="epic_release",
    ):
        raise EpicReleasePreparationError(
            "verification_mismatch", {"epic_id": epic_id}
        )
    return candidate, payload


def _cleanup_epic_release_candidate(candidate: IntegrationCandidate) -> bool:
    try:
        return delete_release_candidate_ref(
            candidate.repo_root,
            candidate_ref=candidate.candidate_ref,
            candidate_sha=candidate.candidate_sha,
        )
    except (RepositoryConfigurationError, OSError, subprocess.SubprocessError):
        return False


def _epic_release_invalidate_durably(
    conn: sqlite3.Connection,
    *,
    epic_id: str,
    snapshot: EpicReleaseSnapshot,
    evidence: dict[str, Any],
) -> None:
    """Atomically mark only the exact active snapshot invalidated.

    Re-checks that the active row is still the same snapshot inside the
    IMMEDIATE transaction (a concurrent preparation may have won), then
    records the status flip together with the typed drift evidence.
    """

    now = int(time.time())
    with authorized_governance_write(), write_txn(conn):
        locked_row = _epic_release_active_row(conn, epic_id)
        if locked_row is None or int(locked_row["id"]) != snapshot.id:
            raise EpicReleaseInvalidationError(
                "active_snapshot_changed",
                {"epic_id": epic_id, "snapshot_id": snapshot.id},
            )
        conn.execute(
            "UPDATE epic_release_snapshots SET status='invalidated', updated_at=? "
            "WHERE id=? AND status IN ('awaiting_push', 'ci_pending', 'ci_failed')",
            (now, snapshot.id),
        )
        _append_event(
            conn,
            epic_id,
            "epic_release_invalidated",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "drift": evidence,
                "candidate_ref": snapshot.candidate_ref,
                "release_candidate_sha": snapshot.release_candidate_sha,
                "invalidated_at": now,
            },
        )


def invalidate_epic_release_snapshot(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
) -> EpicReleaseInvalidation:
    """Invalidate the exact active Epic release snapshot when authority drifted.

    Proven drift — a changed Epic tip, target pre-SHA, repository contract,
    member set/pins, readiness blockers, or aggregate verification
    event/receipt, or an invalid candidate ref — atomically marks only that
    epic's active snapshot ``invalidated`` with typed audit evidence.  After
    the durable invalidation the snapshot's ``candidate_ref`` is deleted only
    when it still pins the recorded ``release_candidate_sha``; an absent,
    mismatched, or repointed ref is preserved and reported.  An exact
    snapshot is returned untouched so E05B1 preparation replay can keep it,
    and unverifiable states never invalidate.
    """

    if conn.in_transaction:
        raise EpicReleaseInvalidationError(
            "active_transaction", {"epic_id": epic_id}
        )

    row = _epic_release_active_row(conn, epic_id)
    if row is None:
        return EpicReleaseInvalidation("missing", None, {}, False)
    try:
        snapshot = epic_release_snapshot_from_row(row)
    except ValueError as exc:
        raise EpicReleaseInvalidationError(
            "invalid_active_snapshot",
            {"epic_id": epic_id, "snapshot_id": row["id"], "error": str(exc)},
        ) from exc

    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta) or not _is_epic_task(conn, epic_id):
        return EpicReleaseInvalidation(
            "unverifiable", snapshot, {"code": "not_governed_epic"}, False
        )
    try:
        contract = repository_contract_for_metadata(meta)
    except (RepositoryConfigurationError, OSError, ValueError) as exc:
        return EpicReleaseInvalidation(
            "unverifiable",
            snapshot,
            {"code": "repository_unavailable", "error": str(exc)},
            False,
        )
    if contract is None:
        return EpicReleaseInvalidation(
            "unverifiable", snapshot, {"code": "missing_repository_contract"}, False
        )
    if "epic_release" not in contract.verification:
        return EpicReleaseInvalidation(
            "unverifiable",
            snapshot,
            {"code": "missing_epic_release_profile"},
            False,
        )

    evidence: dict[str, Any]
    try:
        inputs, _contract, _branch = _epic_release_inputs(
            conn, epic_id, board=board, board_meta=meta
        )
    except EpicReleasePreparationError as exc:
        if exc.code in ("not_ready", "readiness_tip_mismatch"):
            evidence = {"inputs_error": exc.code, **exc.evidence}
        else:
            return EpicReleaseInvalidation(
                "unverifiable", snapshot, {"code": exc.code, **exc.evidence}, False
            )
    else:
        evidence = _epic_release_snapshot_mismatch_evidence(
            conn, snapshot, epic_id=epic_id, inputs=inputs
        )
        if not evidence:
            return EpicReleaseInvalidation("exact", snapshot, {}, False)

    _epic_release_invalidate_durably(
        conn, epic_id=epic_id, snapshot=snapshot, evidence=evidence
    )

    deleted = _epic_release_delete_candidate_and_record(
        conn, epic_id=epic_id, snapshot=snapshot, contract=contract
    )

    invalidated = replace(
        snapshot,
        status="invalidated",
        updated_at=int(time.time()),
    )
    return EpicReleaseInvalidation(
        "invalidated", invalidated, evidence, bool(deleted)
    )


def _epic_release_delete_candidate_and_record(
    conn: sqlite3.Connection,
    *,
    epic_id: str,
    snapshot: EpicReleaseSnapshot,
    contract: RepositoryContract,
) -> bool:
    """Exact-SHA candidate-ref cleanup plus the typed ref-outcome audit event.

    Shared by invalidation and by the release handoff's proven-drift
    refusal: the retained release-candidate ref is deleted only when it
    still pins the recorded SHA, and the outcome is recorded either way.
    An absent or repointed ref is preserved and reported, never recreated.
    """

    deleted = False
    try:
        deleted = delete_release_candidate_ref(
            contract.repo_root,
            candidate_ref=snapshot.candidate_ref,
            candidate_sha=snapshot.release_candidate_sha,
        )
    except (RepositoryConfigurationError, OSError, subprocess.SubprocessError):
        deleted = False
    with authorized_governance_write(), write_txn(conn):
        _append_event(
            conn,
            epic_id,
            "epic_release_invalidated",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "candidate_ref": snapshot.candidate_ref,
                "release_candidate_sha": snapshot.release_candidate_sha,
                "candidate_ref_deleted": bool(deleted),
            },
        )
    return deleted


def invalidate_stale_epic_release_snapshots(
    conn: sqlite3.Connection,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
) -> tuple[EpicReleaseInvalidation, ...]:
    """Invalidate every stale active Epic release snapshot on the board.

    Bounded sweep over the currently active snapshots: only proven drift
    invalidates and only the drifted epic's exact release ref is touched.
    Exact and unverifiable snapshots are returned untouched, so no unrelated
    snapshot or ref changes.
    """

    if conn.in_transaction:
        raise EpicReleaseInvalidationError("active_transaction", {})
    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return ()
    placeholders = ",".join("?" for _ in _EPIC_RELEASE_ACTIVE_STATUSES)
    rows = conn.execute(
        f"SELECT DISTINCT epic_id FROM epic_release_snapshots "  # noqa: S608 -- placeholders only
        f"WHERE status IN ({placeholders}) ORDER BY epic_id",
        _EPIC_RELEASE_ACTIVE_STATUSES,
    ).fetchall()
    results: list[EpicReleaseInvalidation] = []
    for (epic_id,) in rows:
        results.append(
            invalidate_epic_release_snapshot(
                conn, str(epic_id), board=board, board_meta=meta
            )
        )
    return tuple(results)


def prepare_epic_release_snapshot(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
    candidate_builder: Optional[Callable[..., object]] = None,
) -> EpicReleaseSnapshot:
    """Prepare and persist one immutable, verified aggregate Epic snapshot.

    Repository work happens outside the database transaction.  The final
    transaction rechecks every input and the one-active-snapshot boundary,
    then records the aggregate verification event, snapshot, and member pins
    together.  Preparation never moves the target branch and never invalidates
    an existing snapshot.
    """
    if conn.in_transaction:
        raise EpicReleasePreparationError(
            "active_transaction", {"epic_id": epic_id}
        )

    inputs, contract, epic_branch = _epic_release_inputs(
        conn, epic_id, board=board, board_meta=board_meta
    )
    replay = _epic_release_active_or_refuse(conn, epic_id, inputs=inputs)
    if replay is not None:
        return replay

    builder = candidate_builder or _build_verified_merge_candidate
    candidate: Optional[IntegrationCandidate] = None
    persisted = False
    winner: Optional[EpicReleaseSnapshot] = None
    try:
        candidate_value = builder(
            contract.repo_root,
            inputs.target_branch,
            epic_branch,
            f"merge epic {epic_id}",
            expected_source_sha=inputs.epic_tip_sha,
            verification_profile=contract.verification["epic_release"],
            verification_contract_digest=contract.digest,
            verification_scope="epic_release",
            verification_subject_id=epic_id,
            verification_profile_name="epic_release",
            verification_generated_policy_digest=contract.generated_policy_digest,
            configured_verification_fn=(
                lambda path, source, candidate_sha: _run_or_reuse_configured_verification(
                    conn,
                    task_id=epic_id,
                    candidate_path=path,
                    source_sha=source,
                    candidate_sha=candidate_sha,
                    contract=contract,
                    profile_name="epic_release",
                    gate_kind="epic_release",
                )
            )
            if candidate_builder is None
            else None,
            candidate_ref_prefix=RELEASE_CANDIDATE_REF_PREFIX,
        )
        if conn.in_transaction:
            raise EpicReleasePreparationError(
                "active_transaction", {"epic_id": epic_id}
            )
        candidate, verification_payload = _validate_epic_release_candidate(
            candidate_value,
            contract=contract,
            inputs=inputs,
            epic_branch=epic_branch,
            epic_id=epic_id,
        )
        try:
            latest_inputs, latest_contract, latest_branch = _epic_release_inputs(
                conn, epic_id, board=board, board_meta=board_meta
            )
        except EpicReleasePreparationError as exc:
            raise EpicReleasePreparationError(
                "inputs_changed",
                {
                    "epic_id": epic_id,
                    "before": _epic_release_input_evidence(inputs),
                    "after_error": exc.code,
                },
            ) from exc
        if (
            latest_inputs != inputs
            or latest_contract.digest != contract.digest
            or latest_branch != epic_branch
        ):
            raise EpicReleasePreparationError(
                "inputs_changed",
                {
                    "epic_id": epic_id,
                    "before": _epic_release_input_evidence(inputs),
                    "after": _epic_release_input_evidence(latest_inputs),
                },
            )

        with authorized_governance_write(), write_txn(conn):
            locked_inputs, locked_contract, locked_branch = _epic_release_inputs(
                conn, epic_id, board=board, board_meta=board_meta
            )
            if (
                locked_inputs != inputs
                or locked_contract.digest != contract.digest
                or locked_branch != epic_branch
            ):
                raise EpicReleasePreparationError(
                    "inputs_changed",
                    {
                        "epic_id": epic_id,
                        "before": _epic_release_input_evidence(inputs),
                        "after": _epic_release_input_evidence(locked_inputs),
                    },
                )
            active_row = _epic_release_active_row(conn, epic_id)
            if active_row is not None:
                active = epic_release_snapshot_from_row(active_row)
                if not _epic_release_snapshot_matches(
                    conn, active, epic_id=epic_id, inputs=locked_inputs
                ):
                    raise EpicReleasePreparationError(
                        "active_snapshot_mismatch",
                        {
                            "epic_id": epic_id,
                            "snapshot_id": active.id,
                            "expected": _epic_release_input_evidence(locked_inputs),
                        },
                    )
                winner = active
            else:
                now = int(time.time())
                event_id = _append_event(
                    conn, epic_id, "repository_verification", verification_payload
                )
                snapshot_cursor = conn.execute(
                    "INSERT INTO epic_release_snapshots ("
                    "epic_id, epic_tip_sha, target_branch, target_pre_sha, "
                    "release_candidate_sha, candidate_ref, "
                    "aggregate_verification_event_id, repository_contract_digest, "
                    "status, pushed_sha, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_push', NULL, ?, ?)",
                    (
                        epic_id,
                        inputs.epic_tip_sha,
                        inputs.target_branch,
                        inputs.target_pre_sha,
                        candidate.candidate_sha,
                        candidate.candidate_ref,
                        event_id,
                        contract.digest,
                        now,
                        now,
                    ),
                )
                snapshot_id = snapshot_cursor.lastrowid
                if snapshot_id is None:
                    raise RuntimeError("Epic release snapshot insert did not return an id")
                for story_id, source_sha, candidate_sha, integrated_at in inputs.members:
                    conn.execute(
                        "INSERT INTO epic_release_members ("
                        "snapshot_id, epic_id, story_id, source_sha, candidate_sha, integrated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            int(snapshot_id),
                            epic_id,
                            story_id,
                            source_sha,
                            candidate_sha,
                            integrated_at,
                        ),
                    )
                row = conn.execute(
                    "SELECT * FROM epic_release_snapshots WHERE id=?",
                    (int(snapshot_id),),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Epic release snapshot was not durable")
                winner = epic_release_snapshot_from_row(row)
                if not _epic_release_snapshot_matches(
                    conn, winner, epic_id=epic_id, inputs=locked_inputs
                ):
                    raise RuntimeError("Epic release snapshot was not exact")
                persisted = True
    except Exception:
        if candidate is not None and not persisted:
            _cleanup_epic_release_candidate(candidate)
        raise

    if winner is None:
        raise RuntimeError("Epic release preparation produced no snapshot")
    if candidate is not None and not persisted:
        _cleanup_epic_release_candidate(candidate)
    return winner


def build_epic_release_handoff(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
) -> EpicReleaseHandoff:
    """Build truthful immutable release evidence for a human operator.

    The handoff is assembled only after two immediate rechecks, and it is
    refused — never partially returned — when either recheck fails:

    1. The active snapshot is re-derived against current durable authority
       (Epic tip, target pre-SHA, contract, member pins, aggregate
       verification event/receipt).  Any proven drift invalidates the
       snapshot durably (exact-SHA candidate-ref cleanup included) and
       refuses the handoff.
    2. The local and read-only remote target heads are observed right now
       via :func:`observe_target_heads` and compared to the snapshot's
       ``target_pre_sha``.  A mismatch invalidates and refuses; an
       unavailable local or remote target refuses without invalidating
       (drift cannot be proven, so the snapshot is preserved).

    The returned :class:`EpicReleaseHandoff` carries only plain data —
    IDs, full SHAs, member keys, contract digest, aggregate verification
    event, required CI workflows, candidate ref, observed heads, and one
    plain-language external action.  It deliberately contains no merge or
    push command and exposes no capability to perform either.
    """

    if conn.in_transaction:
        raise EpicReleaseHandoffError("active_transaction", {"epic_id": epic_id})

    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta) or not _is_epic_task(conn, epic_id):
        raise EpicReleaseHandoffError("not_governed_epic", {"epic_id": epic_id})
    try:
        contract = repository_contract_for_metadata(meta)
    except (RepositoryConfigurationError, OSError, ValueError) as exc:
        raise EpicReleaseHandoffError(
            "repository_unavailable", {"epic_id": epic_id, "error": str(exc)}
        ) from exc
    if contract is None:
        raise EpicReleaseHandoffError(
            "missing_repository_contract", {"epic_id": epic_id}
        )
    if "epic_release" not in contract.verification:
        raise EpicReleaseHandoffError(
            "missing_epic_release_profile", {"epic_id": epic_id}
        )

    row = _epic_release_active_row(conn, epic_id)
    if row is None:
        raise EpicReleaseHandoffError("no_active_snapshot", {"epic_id": epic_id})
    try:
        snapshot = epic_release_snapshot_from_row(row)
    except ValueError as exc:
        raise EpicReleaseHandoffError(
            "invalid_active_snapshot",
            {"epic_id": epic_id, "snapshot_id": row["id"], "error": str(exc)},
        ) from exc

    # --- Recheck 1: current durable authority. ------------------------------
    drift: dict[str, Any] = {}
    try:
        inputs, _contract, _branch = _epic_release_inputs(
            conn, epic_id, board=board, board_meta=meta
        )
    except EpicReleasePreparationError as exc:
        if exc.code in ("not_ready", "readiness_tip_mismatch"):
            drift = {"inputs_error": exc.code, **exc.evidence}
        else:
            raise EpicReleaseHandoffError(
                exc.code, {"epic_id": epic_id, **exc.evidence}
            ) from exc
    else:
        drift = _epic_release_snapshot_mismatch_evidence(
            conn, snapshot, epic_id=epic_id, inputs=inputs
        )
    if drift:
        _epic_release_invalidate_durably(
            conn, epic_id=epic_id, snapshot=snapshot, evidence=drift
        )
        deleted = _epic_release_delete_candidate_and_record(
            conn, epic_id=epic_id, snapshot=snapshot, contract=contract
        )
        raise EpicReleaseHandoffError(
            "snapshot_drifted",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "drift": drift,
                "candidate_ref_deleted": bool(deleted),
            },
        )

    # --- Recheck 2: immediate local and read-only remote target heads. ------
    try:
        observation = observe_target_heads(
            contract.repo_root,
            target_branch=contract.target_branch,
            base_ref=contract.base_ref,
        )
    except RepositoryConfigurationError as exc:
        raise EpicReleaseHandoffError(
            "repository_unavailable", {"epic_id": epic_id, "error": exc.code}
        ) from exc
    if observation.local_head is None:
        raise EpicReleaseHandoffError(
            "local_target_unavailable",
            {"epic_id": epic_id, "target_branch": contract.target_branch},
        )
    if observation.local_head != snapshot.target_pre_sha:
        _epic_release_invalidate_durably(
            conn,
            epic_id=epic_id,
            snapshot=snapshot,
            evidence={
                "target_pre_sha": {
                    "snapshot": snapshot.target_pre_sha,
                    "local_head": observation.local_head,
                },
                "handoff": "local_target_moved",
            },
        )
        deleted = _epic_release_delete_candidate_and_record(
            conn, epic_id=epic_id, snapshot=snapshot, contract=contract
        )
        raise EpicReleaseHandoffError(
            "local_target_moved",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "snapshot_pre_sha": snapshot.target_pre_sha,
                "local_head": observation.local_head,
                "candidate_ref_deleted": bool(deleted),
            },
        )
    if not observation.remote_available or observation.remote_head is None:
        raise EpicReleaseHandoffError(
            "remote_unavailable",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "remote_name": observation.remote_name,
                "target_branch": contract.target_branch,
            },
        )
    if observation.remote_head != snapshot.target_pre_sha:
        _epic_release_invalidate_durably(
            conn,
            epic_id=epic_id,
            snapshot=snapshot,
            evidence={
                "target_pre_sha": {
                    "snapshot": snapshot.target_pre_sha,
                    "remote_head": observation.remote_head,
                    "remote_name": observation.remote_name,
                },
                "handoff": "remote_target_moved",
            },
        )
        deleted = _epic_release_delete_candidate_and_record(
            conn, epic_id=epic_id, snapshot=snapshot, contract=contract
        )
        raise EpicReleaseHandoffError(
            "remote_target_moved",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "snapshot_pre_sha": snapshot.target_pre_sha,
                "remote_head": observation.remote_head,
                "remote_name": observation.remote_name,
                "candidate_ref_deleted": bool(deleted),
            },
        )

    # --- Assemble the immutable, human-facing evidence payload. -------------
    event = conn.execute(
        "SELECT task_id, kind, payload FROM task_events WHERE id=?",
        (snapshot.aggregate_verification_event_id,),
    ).fetchone()
    if (
        event is None
        or event["task_id"] != epic_id
        or event["kind"] != "repository_verification"
    ):
        raise EpicReleaseHandoffError(
            "aggregate_event_unavailable",
            {"epic_id": epic_id, "snapshot_id": snapshot.id},
        )
    try:
        receipt = json.loads(event["payload"]) if event["payload"] else None
    except (TypeError, ValueError) as exc:
        raise EpicReleaseHandoffError(
            "aggregate_event_unavailable",
            {"epic_id": epic_id, "snapshot_id": snapshot.id, "error": str(exc)},
        ) from exc
    if not isinstance(receipt, Mapping):
        raise EpicReleaseHandoffError(
            "aggregate_event_unavailable",
            {"epic_id": epic_id, "snapshot_id": snapshot.id},
        )

    members = _epic_release_snapshot_members(conn, snapshot.id)
    action = (
        f"Epic release snapshot {snapshot.id} for epic {epic_id} is pinned at "
        f"{snapshot.candidate_ref} ({snapshot.release_candidate_sha}) against "
        f"target pre-image {snapshot.target_pre_sha} on branch "
        f"{snapshot.target_branch} of remote {observation.remote_name}. "
        "A human release operator must review this pinned evidence and perform "
        "the release out-of-band."
    )
    return EpicReleaseHandoff(
        epic_id=epic_id,
        snapshot=snapshot,
        members=members,
        workflows=tuple(contract.ci_workflows),
        aggregate_event_kind=str(event["kind"]),
        aggregate_event_receipt=dict(receipt),
        local_target_head=observation.local_head,
        remote_target_head=observation.remote_head,
        remote_name=observation.remote_name,
        action=action,
        checked_at=int(time.time()),
    )


def _epic_release_record_pushed(
    conn: sqlite3.Connection,
    *,
    epic_id: str,
    snapshot: EpicReleaseSnapshot,
    pushed_sha: str,
) -> None:
    """Atomically pin ``pushed_sha`` for the exact active snapshot.

    Re-checks that the active row is still the same snapshot inside the
    IMMEDIATE transaction (a concurrent observation may have won), then
    records the push with a typed event.  A status transition from
    ``awaiting_push`` to ``ci_pending`` happens here as well.
    """

    now = int(time.time())
    with authorized_governance_write(), write_txn(conn):
        locked_row = _epic_release_active_row(conn, epic_id)
        if locked_row is None or int(locked_row["id"]) != snapshot.id:
            raise EpicReleaseCIObservationError(
                "active_snapshot_changed",
                {"epic_id": epic_id, "snapshot_id": snapshot.id},
            )
        conn.execute(
            "UPDATE epic_release_snapshots SET pushed_sha=?, status='ci_pending', "
            "updated_at=? WHERE id=? AND status IN ('awaiting_push', 'ci_pending')",
            (pushed_sha, now, snapshot.id),
        )
        _append_event(
            conn,
            epic_id,
            "epic_release_ci_pending",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "pushed_sha": pushed_sha,
                "release_candidate_sha": snapshot.release_candidate_sha,
                "observed_at": now,
            },
        )


def _epic_release_record_ci_failed(
    conn: sqlite3.Connection,
    *,
    epic_id: str,
    snapshot: EpicReleaseSnapshot,
    conclusions: Mapping[str, str | None],
) -> None:
    """Atomically mark the exact active snapshot ``ci_failed``.

    Manual recovery is retained: the snapshot stays active, and a later
    same-SHA observation where every workflow passes still releases it.
    """

    now = int(time.time())
    with authorized_governance_write(), write_txn(conn):
        locked_row = _epic_release_active_row(conn, epic_id)
        if locked_row is None or int(locked_row["id"]) != snapshot.id:
            raise EpicReleaseCIObservationError(
                "active_snapshot_changed",
                {"epic_id": epic_id, "snapshot_id": snapshot.id},
            )
        conn.execute(
            "UPDATE epic_release_snapshots SET status='ci_failed', updated_at=? "
            "WHERE id=? AND status IN ('awaiting_push', 'ci_pending', 'ci_failed')",
            (now, snapshot.id),
        )
        _append_event(
            conn,
            epic_id,
            "epic_release_ci_failed",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "pushed_sha": snapshot.release_candidate_sha,
                "release_candidate_sha": snapshot.release_candidate_sha,
                "conclusions": dict(conclusions),
                "observed_at": now,
            },
        )


def _epic_release_record_released(
    conn: sqlite3.Connection,
    *,
    epic_id: str,
    snapshot: EpicReleaseSnapshot,
    conclusions: Mapping[str, str | None],
) -> None:
    """Atomically flip the exact active snapshot to ``released``."""

    now = int(time.time())
    with authorized_governance_write(), write_txn(conn):
        locked_row = _epic_release_active_row(conn, epic_id)
        if locked_row is None or int(locked_row["id"]) != snapshot.id:
            raise EpicReleaseCIObservationError(
                "active_snapshot_changed",
                {"epic_id": epic_id, "snapshot_id": snapshot.id},
            )
        conn.execute(
            "UPDATE epic_release_snapshots SET status='released', updated_at=? "
            "WHERE id=? AND status IN ('awaiting_push', 'ci_pending', 'ci_failed')",
            (now, snapshot.id),
        )
        _append_event(
            conn,
            epic_id,
            "epic_release_released",
            {
                "epic_id": epic_id,
                "snapshot_id": snapshot.id,
                "pushed_sha": snapshot.release_candidate_sha,
                "release_candidate_sha": snapshot.release_candidate_sha,
                "candidate_ref": snapshot.candidate_ref,
                "conclusions": dict(conclusions),
                "released_at": now,
            },
        )


def observe_epic_release_ci(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
) -> EpicReleaseCIObservation:
    """Observe the exact active Epic release snapshot's CI state, read-only.

    The observation is strictly read-only against the CI provider (HTTP
    GET only) and against Git (``rev-parse``/``ls-remote`` only): no
    rerun, cancel, merge, push, or update-remote primitive is ever issued.
    Outcomes:

    * Proven durable-authority drift, or a remote target head that moved
      away from the recorded candidate after it was pinned pushed, marks
      only the exact snapshot ``invalidated`` and exact-deletes the
      candidate ref (when it still pins the recorded SHA).
    * A remote head equal to ``target_pre_sha`` (not yet pushed) leaves
      the snapshot ``ci_pending``.
    * Only ``pushed_sha == release_candidate_sha`` plus every required
      workflow ``success`` releases; a failure/cancel/timeout preserves
      the snapshot as ``ci_failed`` (manual recovery retained), running or
      queued stays ``ci_pending``, and a later same-SHA all-pass releases.
    * An unobservable remote or CI provider preserves the snapshot
      ``unavailable`` — drift cannot be proven, so nothing changes.
    """

    if conn.in_transaction:
        raise EpicReleaseCIObservationError("active_transaction", {"epic_id": epic_id})

    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta) or not _is_epic_task(conn, epic_id):
        raise EpicReleaseCIObservationError("not_governed_epic", {"epic_id": epic_id})
    try:
        contract = repository_contract_for_metadata(meta)
    except (RepositoryConfigurationError, OSError, ValueError) as exc:
        raise EpicReleaseCIObservationError(
            "repository_unavailable", {"epic_id": epic_id, "error": str(exc)}
        ) from exc
    if contract is None:
        raise EpicReleaseCIObservationError(
            "missing_repository_contract", {"epic_id": epic_id}
        )
    if "epic_release" not in contract.verification:
        raise EpicReleaseCIObservationError(
            "missing_epic_release_profile", {"epic_id": epic_id}
        )

    row = _epic_release_active_row(conn, epic_id)
    if row is None:
        return EpicReleaseCIObservation("missing", None, {}, False, None)
    try:
        snapshot = epic_release_snapshot_from_row(row)
    except ValueError as exc:
        raise EpicReleaseCIObservationError(
            "invalid_active_snapshot",
            {"epic_id": epic_id, "snapshot_id": row["id"], "error": str(exc)},
        ) from exc

    # --- Recheck current durable authority. ---------------------------------
    try:
        inputs, _contract, _branch = _epic_release_inputs(
            conn, epic_id, board=board, board_meta=meta
        )
    except EpicReleasePreparationError as exc:
        if exc.code in ("not_ready", "readiness_tip_mismatch"):
            drift = {"inputs_error": exc.code, **exc.evidence}
        else:
            raise EpicReleaseCIObservationError(
                exc.code, {"epic_id": epic_id, **exc.evidence}
            ) from exc
    else:
        drift = _epic_release_snapshot_mismatch_evidence(
            conn, snapshot, epic_id=epic_id, inputs=inputs
        )
    if drift:
        _epic_release_invalidate_durably(
            conn, epic_id=epic_id, snapshot=snapshot, evidence=drift
        )
        deleted = _epic_release_delete_candidate_and_record(
            conn, epic_id=epic_id, snapshot=snapshot, contract=contract
        )
        invalidated = replace(
            snapshot, status="invalidated", updated_at=int(time.time())
        )
        return EpicReleaseCIObservation(
            "invalidated", invalidated, drift, bool(deleted), snapshot.pushed_sha
        )

    # --- Observe the remote target head (read-only). ------------------------
    try:
        observation = observe_target_heads(
            contract.repo_root,
            target_branch=contract.target_branch,
            base_ref=contract.base_ref,
        )
    except RepositoryConfigurationError as exc:
        raise EpicReleaseCIObservationError(
            "repository_unavailable", {"epic_id": epic_id, "error": exc.code}
        ) from exc
    if not observation.remote_available or observation.remote_head is None:
        return EpicReleaseCIObservation(
            "unavailable",
            snapshot,
            {"remote_name": observation.remote_name},
            False,
            snapshot.pushed_sha,
        )

    if observation.remote_head != snapshot.release_candidate_sha:
        if snapshot.pushed_sha == snapshot.release_candidate_sha:
            # The candidate was pinned pushed but the remote moved on to a
            # different SHA: durable invalidation with exact-SHA cleanup.
            evidence = {
                "target_pre_sha": {
                    "snapshot": snapshot.target_pre_sha,
                    "remote_head": observation.remote_head,
                },
                "remote_head": observation.remote_head,
                "release_candidate_sha": snapshot.release_candidate_sha,
            }
            _epic_release_invalidate_durably(
                conn, epic_id=epic_id, snapshot=snapshot, evidence=evidence
            )
            deleted = _epic_release_delete_candidate_and_record(
                conn, epic_id=epic_id, snapshot=snapshot, contract=contract
            )
            invalidated = replace(
                snapshot, status="invalidated", updated_at=int(time.time())
            )
            return EpicReleaseCIObservation(
                "invalidated", invalidated, evidence, bool(deleted), snapshot.pushed_sha
            )
        # Not yet pushed: the remote is still at (or not at) the target
        # pre-image and we have never pinned a push.  Preserve as pending.
        return EpicReleaseCIObservation(
            "ci_pending",
            snapshot,
            {
                "remote_head": observation.remote_head,
                "release_candidate_sha": snapshot.release_candidate_sha,
                "not_yet_pushed": True,
            },
            False,
            snapshot.pushed_sha,
        )

    # The exact candidate is confirmed on the remote target head.
    if snapshot.pushed_sha != snapshot.release_candidate_sha:
        _epic_release_record_pushed(
            conn,
            epic_id=epic_id,
            snapshot=snapshot,
            pushed_sha=snapshot.release_candidate_sha,
        )

    # --- Observe CI (HTTP GET only) for the exact candidate SHA. -----------
    try:
        conclusions = observe_ci_workflow_runs(
            contract.repo_root,
            base_ref=contract.base_ref,
            workflows=tuple(contract.ci_workflows),
            head_sha=snapshot.release_candidate_sha,
        )
    except RepositoryConfigurationError as exc:
        raise EpicReleaseCIObservationError(
            "repository_unavailable", {"epic_id": epic_id, "error": exc.code}
        ) from exc
    if conclusions is None:
        return EpicReleaseCIObservation(
            "unavailable",
            snapshot,
            {"ci_provider": "unavailable"},
            False,
            snapshot.release_candidate_sha,
        )

    statuses = [conclusions.get(wf) for wf in contract.ci_workflows]
    if all(status == "success" for status in statuses):
        _epic_release_record_released(
            conn,
            epic_id=epic_id,
            snapshot=snapshot,
            conclusions=conclusions,
        )
        deleted = delete_release_candidate_ref(
            contract.repo_root,
            candidate_ref=snapshot.candidate_ref,
            candidate_sha=snapshot.release_candidate_sha,
        )
        released = replace(
            snapshot, status="released", updated_at=int(time.time())
        )
        return EpicReleaseCIObservation(
            "released",
            released,
            {"conclusions": dict(conclusions)},
            bool(deleted),
            snapshot.release_candidate_sha,
        )
    if any(status in ("failure", "cancelled", "timed_out") for status in statuses):
        _epic_release_record_ci_failed(
            conn,
            epic_id=epic_id,
            snapshot=snapshot,
            conclusions=conclusions,
        )
        failed = replace(
            snapshot, status="ci_failed", updated_at=int(time.time())
        )
        return EpicReleaseCIObservation(
            "ci_failed",
            failed,
            {"conclusions": dict(conclusions)},
            False,
            snapshot.release_candidate_sha,
        )
    # Running / queued / no run yet.
    return EpicReleaseCIObservation(
        "ci_pending",
        snapshot,
        {"conclusions": dict(conclusions)},
        False,
        snapshot.release_candidate_sha,
    )


def _merge_epic_fail_safe(
    conn: sqlite3.Connection,
    epic_id: str,
    reason: str,
    *,
    board: Optional[str],
    notify_fn: Optional[Callable[[str, str, Optional[str]], None]],
) -> None:
    """Block the epic + emit an event + notify. Never raises.

    Shared by every failure exit of :func:`merge_epic_to_main` (conflict,
    post-merge verify failure, unexpected error) so a failed merge always
    leaves the epic in a visible, blocked state instead of silently retrying
    forever.
    """
    try:
        set_running(conn, epic_id, False, board=board)
        set_blocked(conn, epic_id, True, board=board, reason=reason)
        with write_txn(conn):
            _append_event(
                conn, epic_id, "blocked", {"reason": reason, "kind": "epic_merge_failed"},
            )
    except Exception:
        pass
    if notify_fn is not None:
        try:
            notify_fn(epic_id, reason, board)
        except Exception:
            pass


def merge_epic_to_main(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
    main_branch: str = "main",
    verify_fn: Optional[Callable[[str], bool]] = None,
    candidate_verify_fn: Optional[Callable[[Path], bool]] = None,
    expected_source_sha: Optional[str] = None,
    before_apply_fn: Optional[Callable[[], bool]] = None,
    notify_fn: Optional[Callable[[str, str, Optional[str]], None]] = None,
) -> Optional[str]:
    """Hermes-run merge of ``epic_id``'s integration branch into LOCAL main.

    Returns ``None`` on a non-``handoff_v2`` board (nothing to do here --
    callers fall back to legacy flows); ``"not_ready"`` when :func:`epic_ready`
    says the epic isn't mergeable yet, or the repo/branch can't be resolved
    (no git mutation in either case); ``"conflict"`` when the merge itself
    fails (aborted, main left exactly as it was); ``"verify_failed"`` when the
    candidate merge succeeds but its isolated verification fails;
    ``"merged"`` only after an atomic fast-forward of the unchanged target.

    On both failure outcomes the epic is cleared of ``running``, marked
    ``blocked``, and ``notify_fn`` (the Slack hook) is invoked -- see
    :func:`_merge_epic_fail_safe`. Every subprocess call uses the file's
    defensive idiom (``capture_output=True, text=True, timeout=...,
    check=False``); no exception escapes this function.

    ``board_meta`` is an already-read board metadata snapshot. Release
    orchestration passes the one snapshot it validated policy against, so a
    board edit mid-operation cannot make a later step disagree with the gate
    that admitted it. ``None`` reads fresh, exactly as before.
    """
    # =========================================================================
    # LOCAL main only -- never `git push` / touch origin. Production deploys
    # and `git push origin` are HUMAN-ONLY; this is the hard autonomy
    # boundary for Hermes. This function must NEVER call `git push`, and must
    # NEVER import or call web_git.py's push helpers (`_review_push` /
    # `review_push` / `review_create_pr`). Only local git verbs below:
    # rev-parse, worktree, merge, merge --abort, status, and update-ref.
    # =========================================================================
    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return None
    lifecycle_scope = conn.execute(
        "SELECT workflow_template_id, current_step_key FROM tasks WHERE id=?",
        (epic_id,),
    ).fetchone()
    if _is_engine_owned_integration_state(lifecycle_scope):
        return "not_ready"
    _validate_stored_product_workflow_state(conn, epic_id)

    # A repository contract owns verification for governed boards.  Do not
    # run the legacy boolean readiness probe first: it would discard typed
    # configuration/infrastructure results and return ``not_ready`` before the
    # candidate builder can persist the required attention evidence.
    readiness_verify = (
        (lambda _branch: True)
        if candidate_verify_fn is not None or "repository" in meta
        else verify_fn
    )
    if not epic_ready(conn, epic_id, board=board, verify_fn=readiness_verify):
        return "not_ready"

    try:
        board_default = str(meta.get("default_workdir") or "").strip()
        repo_root = _git_toplevel(Path(board_default).expanduser()) if board_default else None
        epic_branch = epic_branch_for(epic_id)
        if repo_root is None or not _git_branch_exists(repo_root, epic_branch):
            return "not_ready"
        contract = repository_contract_for_metadata(meta, repo_root=repo_root)
    except Exception:
        return "not_ready"

    def _fail(reason: str) -> None:
        _merge_epic_fail_safe(conn, epic_id, reason, board=board, notify_fn=notify_fn)

    try:
        effective_verify_fn = candidate_verify_fn or (
            (lambda _path: bool(verify_fn(main_branch)))
            if verify_fn is not None
            else None
        )
        candidate = _build_verified_merge_candidate(
            repo_root,
            main_branch,
            epic_branch,
            f"merge epic {epic_id}",
            effective_verify_fn,
            expected_source_sha=expected_source_sha,
            verification_profile=(
                contract.verification.get("epic_release") if contract is not None else None
            ),
            verification_contract_digest=(contract.digest if contract is not None else None),
            verification_scope="epic_release",
            verification_subject_id=epic_id,
            verification_profile_name="epic_release",
            verification_generated_policy_digest=(
                contract.generated_policy_digest if contract is not None else ""
            ),
            configured_verification_fn=(
                lambda path, source, candidate: _run_or_reuse_configured_verification(
                    conn, task_id=epic_id, candidate_path=path, source_sha=source,
                    candidate_sha=candidate, contract=contract,
                    profile_name="epic_release", gate_kind="epic_release"
                )
            ) if contract is not None and candidate_verify_fn is None else None,
        )
        if before_apply_fn is not None and not before_apply_fn():
            return "ownership_conflict"
        if not _fast_forward_target(candidate):
            _fail(
                "target moved or became dirty; candidate retained at "
                f"{candidate.candidate_ref}"
            )
            return "verify_failed"

        try:
            with write_txn(conn):
                if candidate.verification_result is not None and not candidate.verification_result.reused:
                    _append_event(
                        conn,
                        epic_id,
                        "repository_verification",
                        _verification_result_payload(
                            candidate.verification_result,
                            scope="epic_release",
                            subject_id=epic_id,
                        ),
                    )
                _append_event(
                    conn,
                    epic_id,
                    "epic_merged",
                    {
                        "epic_branch": epic_branch,
                        "source_branch": epic_branch,
                        "source_sha": candidate.source_sha,
                        "target_branch": main_branch,
                        "pre_sha": candidate.pre_sha,
                        "candidate_sha": candidate.candidate_sha,
                        "target": str(candidate.target_worktree or main_branch),
                        "test_command": "bash scripts/run_tests.sh",
                    },
                )
        except Exception:
            pass
        return "merged"
    except IntegrationCandidateError as exc:
        reason = str(exc)
        if exc.verification_result is not None:
            try:
                with write_txn(conn):
                    _append_event(
                        conn,
                        epic_id,
                        "repository_verification",
                        _verification_result_payload(
                            exc.verification_result,
                            scope="epic_release",
                            subject_id=epic_id,
                        ),
                    )
            except Exception:
                pass
        _fail(reason)
        if _verification_needs_attention(exc.verification_result):
            return "attention_required"
        if "merge conflict" in reason:
            return "conflict"
        return "verify_failed"
    except Exception:
        _fail("unexpected error while building integration candidate")
        return "verify_failed"


def _merge_standalone_story_to_main(
    conn: sqlite3.Connection,
    story_id: str,
    *,
    board: Optional[str] = None,
    board_meta: Optional[dict] = None,
    main_branch: str = "main",
    verify_fn: Optional[Callable[[str], bool]] = None,
    candidate_verify_fn: Optional[Callable[[Path], bool]] = None,
    expected_source_sha: Optional[str] = None,
    before_apply_fn: Optional[Callable[[], bool]] = None,
    notify_fn: Optional[Callable[[str, str, Optional[str]], None]] = None,
    allow_release_measure: bool = False,
) -> Optional[str]:
    """Merge a Done, epic-LESS product story's branch into LOCAL main.

    The common case for imported / single-story product boards: a finished
    story with no epic parent, whose work would otherwise strand on its
    per-card branch because the epic release path does not apply. This closes
    that gap for the standalone case, mirroring :func:`merge_epic_to_main`.

    Returns ``None`` on a non-``handoff_v2`` board or a story that actually HAS
    an epic parent (that path is the epic integration + merge); ``"not_ready"``
    when the story isn't ``done`` or the repo/branch can't be resolved (no git
    mutation); ``"already_merged"`` when the story branch is already an ancestor
    of main (idempotent); ``"conflict"`` when the merge fails (aborted, main
    left exactly as it was); ``"verify_failed"`` when the merge succeeds but the
    isolated candidate is dirty or the suite isn't green (the target remains
    unchanged); ``"merged"`` on success. On both failure outcomes the
    STORY (not an epic) is cleared of ``running``, blocked, and ``notify_fn``
    invoked.
    """
    # =========================================================================
    # LOCAL main only -- never `git push` / touch origin. Same hard autonomy
    # boundary as merge_epic_to_main: production deploys and `git push origin`
    # are HUMAN-ONLY. Only local git verbs below.
    # =========================================================================
    meta = board_meta if board_meta is not None else product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return None
    _validate_stored_product_workflow_state(conn, story_id)
    # Epic members are owned by the durable story-integration coordinator.
    if _is_epic_task(conn, story_id) or epic_id_for_task(conn, story_id) is not None:
        return None
    story = get_task(conn, story_id)
    if story is None or (
        story.status != "done"
        and not (allow_release_measure and story.current_step_key == "release_measure")
    ):
        return "not_ready"

    try:
        board_default = str(meta.get("default_workdir") or "").strip()
        repo_root = _git_toplevel(Path(board_default).expanduser()) if board_default else None
        story_branch = (story.branch_name or "").strip()
        if (
            repo_root is None
            or not story_branch
            or not _git_branch_exists(repo_root, story_branch)
        ):
            return "not_ready"
        contract = repository_contract_for_metadata(meta, repo_root=repo_root)
        authority_records = _terminal_run_records(conn, story_id)
        authority_phase_present = any(
            record.phase in {"test", "review"} for record in authority_records
        )
        reviewed_candidate = latest_review_authority(authority_records)
        passed_test = (
            latest_test_authority(authority_records, reviewed_candidate.source_sha)
            if reviewed_candidate is not None
            else None
        )
        if authority_phase_present:
            if (
                reviewed_candidate is None
                or passed_test is None
                or reviewed_candidate.branch != story_branch
                or (
                    expected_source_sha is not None
                    and expected_source_sha != reviewed_candidate.source_sha
                )
            ):
                return "verify_failed"
            try:
                candidate_eligibility(repo_root, reviewed_candidate, passed_test)
            except CandidateEligibilityError:
                return "verify_failed"
    except Exception:
        return "not_ready"

    def _fail(reason: str) -> None:
        try:
            set_running(conn, story_id, False, board=board)
            set_blocked(conn, story_id, True, board=board, reason=reason)
            with write_txn(conn):
                _append_event(
                    conn,
                    story_id,
                    "blocked",
                    {"reason": reason, "kind": "standalone_merge_failed"},
                )
        except Exception:
            pass
        if notify_fn is not None:
            try:
                notify_fn(story_id, reason, board)
            except Exception:
                pass

    try:
        ancestor_result = _integration_git(
            repo_root, ["merge-base", "--is-ancestor", story_branch, main_branch]
        )
        already_merged = ancestor_result.returncode == 0
        if already_merged and expected_source_sha is None:
            if reviewed_candidate is not None:
                return "verify_failed"
            with write_txn(conn):
                _append_event(
                    conn,
                    story_id,
                    "story_merged_to_main",
                    {
                        "branch": story_branch,
                        "source_branch": story_branch,
                        "source_sha": _git_ref_sha(repo_root, story_branch),
                        "target_branch": main_branch,
                        "candidate_sha": _git_ref_sha(repo_root, main_branch),
                        "already_merged": True,
                    },
                )
            return "already_merged"
        effective_verify_fn = candidate_verify_fn or (
            (lambda _path: bool(verify_fn(main_branch)))
            if verify_fn is not None
            else None
        )
        candidate = _build_verified_merge_candidate(
            repo_root,
            main_branch,
            story_branch,
            f"merge story {story_id}",
            effective_verify_fn,
            expected_source_sha=expected_source_sha,
            allow_empty_contribution=already_merged,
            verification_profile=(
                contract.verification.get("story_integration")
                if contract is not None
                else None
            ),
            verification_contract_digest=(contract.digest if contract is not None else None),
            verification_scope="story_integration",
            verification_subject_id=story_id,
            verification_profile_name="story_integration",
            verification_generated_policy_digest=(
                contract.generated_policy_digest if contract is not None else ""
            ),
            configured_verification_fn=(
                lambda path, source, candidate: _run_or_reuse_configured_verification(
                    conn, task_id=story_id, candidate_path=path, source_sha=source,
                    candidate_sha=candidate, contract=contract,
                    profile_name="story_integration", gate_kind="story_integration"
                )
            ) if contract is not None and candidate_verify_fn is None else None,
        )
        if before_apply_fn is not None and not before_apply_fn():
            return "ownership_conflict"
        if not _fast_forward_target(candidate):
            _fail(
                "target moved or became dirty; candidate retained at "
                f"{candidate.candidate_ref}"
            )
            return "verify_failed"

        try:
            with write_txn(conn):
                if candidate.verification_result is not None and not candidate.verification_result.reused:
                    _append_event(
                        conn,
                        story_id,
                        "repository_verification",
                        _verification_result_payload(
                            candidate.verification_result,
                            scope="story_integration",
                            subject_id=story_id,
                        ),
                    )
                _append_event(
                    conn,
                    story_id,
                    "story_merged_to_main",
                    {
                        "branch": story_branch,
                        "source_branch": story_branch,
                        "source_sha": candidate.source_sha,
                        "target_branch": main_branch,
                        "pre_sha": candidate.pre_sha,
                        "candidate_sha": candidate.candidate_sha,
                        "target": str(candidate.target_worktree or main_branch),
                        "test_command": "bash scripts/run_tests.sh",
                    },
                )
        except Exception:
            pass
        return "already_merged" if already_merged else "merged"
    except IntegrationCandidateError as exc:
        reason = str(exc)
        if exc.verification_result is not None:
            try:
                with write_txn(conn):
                    _append_event(
                        conn,
                        story_id,
                        "repository_verification",
                        _verification_result_payload(
                            exc.verification_result,
                            scope="story_integration",
                            subject_id=story_id,
                        ),
                    )
            except Exception:
                pass
        _fail(reason)
        if _verification_needs_attention(exc.verification_result):
            return "attention_required"
        if "merge conflict" in reason:
            return "conflict"
        return "verify_failed"
    except Exception:
        _fail("unexpected error while building story integration candidate")
        return "verify_failed"


def _event_by_id(
    conn: sqlite3.Connection, task_id: str, event_id: Any
) -> Optional[Event]:
    if not isinstance(event_id, int):
        return None
    return next((event for event in list_events(conn, task_id) if event.id == event_id), None)


def _latest_event_of_kind(
    conn: sqlite3.Connection, task_id: str, kinds: set[str]
) -> Optional[Event]:
    events = [event for event in list_events(conn, task_id) if event.kind in kinds]
    return events[-1] if events else None


def _reviewer_evidence(metadata: dict) -> tuple[str, str, str]:
    """Return ``(verdict, reviewed_branch, reviewed_commit)`` from a review run.

    The canonical location is ``ai_provenance.reviewer``. Reviewer runs have
    also recorded the same facts at the metadata root, in more than one shape,
    so those are accepted as fallbacks. This only widens where the values are
    *read from* — callers still compare them against the release branch and
    source SHA, so approval stays bound to the exact candidate.
    """
    reviewer = _provenance_payload(metadata).get("reviewer")
    reviewer = reviewer if isinstance(reviewer, dict) else {}
    candidate = metadata.get("candidate")
    candidate = candidate if isinstance(candidate, dict) else {}
    workflow = metadata.get("workflow_outcome")
    workflow = workflow if isinstance(workflow, dict) else {}

    def _first(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    return (
        _first(reviewer.get("verdict"), metadata.get("verdict"), workflow.get("verdict")),
        # Branch has no dispatcher-pinned source; release cross-checks this
        # worker-authored chain against the branch it is actually releasing.
        _first(
            reviewer.get("reviewed_branch"),
            metadata.get("reviewed_branch"),
            metadata.get("branch"),
            candidate.get("branch"),
        ),
        _first(
            metadata.get("review_head_sha"),
            reviewer.get("reviewed_commit"),
            metadata.get("reviewed_commit"),
            candidate.get("head_sha"),
        ),
    )


def _terminal_run_records(
    conn: sqlite3.Connection, task_id: str
) -> list[TerminalRunRecord]:
    """Convert ended runs to the authority kernel's immutable input shape."""

    records: list[TerminalRunRecord] = []
    ended_runs = sorted(
        list_runs(conn, task_id, include_active=False),
        key=lambda run: (int(run.ended_at or 0), int(run.id)),
    )
    development_writer_provider: Optional[str] = None
    for run in ended_runs:
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        if not _ordinary_product_evidence(run.profile, run.outcome, metadata):
            continue
        phase = str(run.step_key or "")
        if phase == "development":
            development_executor = _executor_from_run_metadata(metadata)
            development_writer_provider = (
                development_executor["provider"] if development_executor else None
            )
        writer_provider = _writer_agent_from_metadata(metadata)
        if phase == "test" and not writer_provider:
            writer_provider = development_writer_provider
        try:
            outcome = validate_terminal_outcome(
                task_id=task_id,
                run_id=run.id,
                phase=phase,
                summary=run.summary,
                result=None,
                metadata=metadata,
            )
        except OutcomeValidationError:
            outcome = None
        if (
            outcome is not None
            and outcome.verdict in {"passed", "approved"}
            and run.outcome not in {"advanced", "completed"}
        ):
            # A worker's optimistic blob cannot turn a crashed/reclaimed run
            # into success. Retain the attempt to invalidate older evidence.
            outcome = None
        records.append(
            TerminalRunRecord(
                run_id=run.id,
                phase=phase,
                outcome=outcome,
                test_branch=str(metadata.get("test_branch") or "").strip() or None,
                test_head_sha=str(metadata.get("test_head_sha") or "").strip() or None,
                review_branch=str(metadata.get("review_branch") or "").strip() or None,
                review_base_sha=str(metadata.get("review_base_sha") or "").strip() or None,
                review_head_sha=str(metadata.get("review_head_sha") or "").strip() or None,
                writer_provider=writer_provider,
                tester_provider=_tester_agent_from_metadata(metadata),
                reviewer_provider=_reviewer_agent_from_metadata(metadata),
            )
        )
    return records


def _latest_approved_review_candidate(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[ApprovedCandidate]:
    """Return the latest immutable branch/SHA approved by independent Review."""
    return latest_review_authority(_terminal_run_records(conn, task_id))


def _release_run_evidence(
    conn: sqlite3.Connection,
    task_id: str,
    branch: str,
    source_sha: str,
) -> dict[str, int]:
    runs = list_runs(conn, task_id, include_active=False)
    records = _terminal_run_records(conn, task_id)
    approved = latest_review_authority(records)
    passed = latest_test_authority(records, source_sha)
    test_run = next((run for run in runs if passed and run.id == passed.run_id), None)
    review_run = next((run for run in runs if approved and run.id == approved.run_id), None)
    reviewer_agent = approved.reviewer_provider if approved else None
    reviewed_writer_agent = approved.writer_provider if approved else None
    reviewed_branch = approved.branch if approved else None
    reviewed_commit = approved.source_sha if approved else None

    missing: list[str] = []
    if test_run is None:
        missing.append("tester_pass")
    if review_run is None:
        missing.append("reviewer_approval")
        latest_review = next(
            (record for record in reversed(records) if record.phase == "review"),
            None,
        )
        if (
            latest_review is not None
            and latest_review.outcome is not None
            and latest_review.outcome.verdict == "approved"
            and latest_review.writer_provider
            and latest_review.reviewer_provider
            and _agent_compare_key(latest_review.writer_provider)
            == _agent_compare_key(latest_review.reviewer_provider)
        ):
            missing.append("independent_reviewer")
    if (
        review_run is not None
        and reviewed_writer_agent
        and reviewer_agent
        and _agent_compare_key(reviewed_writer_agent)
        == _agent_compare_key(reviewer_agent)
    ):
        missing.append("independent_reviewer")
    if review_run is not None and (
        reviewed_branch != branch or reviewed_commit != source_sha
    ):
        missing.append("reviewed_candidate")
    if test_run is not None and passed is not None and passed.branch != branch:
        missing.append("tester_pass")
    if not reviewed_writer_agent:
        missing.append("writer_evidence")
    if review_run is not None and not reviewer_agent:
        missing.append("independent_reviewer")
    if missing:
        raise ReleaseEvidenceError(task_id, missing)
    return {"test_run_id": test_run.id, "review_run_id": review_run.id}


def _release_history_evidence(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, Any]:
    """Materialize the rejection/rework chain carried into terminal evidence."""
    events = list_events(conn, task_id)
    development_handoffs = [
        {"event_id": event.id, "sha": event.payload.get("sha")}
        for event in events
        if event.kind == "handoff"
        and isinstance(event.payload, dict)
        and event.payload.get("from_step") == "development"
    ]
    failed_test_run_ids = []
    for run in list_runs(conn, task_id, include_active=False):
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        workflow = metadata.get("workflow_outcome")
        if (
            run.step_key == "test"
            and run.outcome == "rework_requested"
            and isinstance(workflow, dict)
            and workflow.get("verdict") == "changes_requested"
        ):
            failed_test_run_ids.append(run.id)
    rework_events = [event for event in events if event.kind == "rework_requested"]
    task = get_task(conn, task_id)
    return {
        "development_handoffs": development_handoffs,
        "failed_test_run_ids": failed_test_run_ids,
        "rework_event_ids": [event.id for event in rework_events],
        "rework_count": int(task.rework_count if task is not None else 0),
    }


def _validate_done_evidence(
    conn: sqlite3.Connection, task_id: str, evidence: dict
) -> None:
    """Validate terminal evidence while ``complete_task`` holds its write txn."""
    missing: list[str] = []
    try:
        run_evidence = _release_run_evidence(
            conn,
            task_id,
            str(evidence.get("source_branch") or ""),
            str(evidence.get("source_sha") or ""),
        )
        if run_evidence.get("test_run_id") != evidence.get("test_run_id"):
            missing.append("tester_pass")
        if run_evidence.get("review_run_id") != evidence.get("review_run_id"):
            missing.append("reviewer_approval")
    except ReleaseEvidenceError as exc:
        missing.extend(exc.missing)

    history = _release_history_evidence(conn, task_id)
    if (
        "development_handoffs" not in evidence
        or evidence.get("development_handoffs")
        != history["development_handoffs"]
    ):
        missing.append("development_history")
    if (
        "failed_test_run_ids" not in evidence
        or evidence.get("failed_test_run_ids")
        != history["failed_test_run_ids"]
    ):
        missing.append("tester_failures")
    if (
        "rework_event_ids" not in evidence
        or evidence.get("rework_event_ids") != history["rework_event_ids"]
        or "rework_count" not in evidence
        or evidence.get("rework_count") != history["rework_count"]
    ):
        missing.append("rework_history")

    integration = _event_by_id(conn, task_id, evidence.get("integration_event_id"))
    if (
        integration is None
        or integration.kind
        not in {"story_merged_to_main", "story_integrated_to_epic", "epic_merged"}
        or not isinstance(integration.payload, dict)
        or integration.payload.get("candidate_sha") != evidence.get("integration_sha")
        or integration.payload.get("source_branch") != evidence.get("source_branch")
        or integration.payload.get("source_sha") != evidence.get("source_sha")
    ):
        missing.append("integrated_branch")

    policy = _event_by_id(conn, task_id, evidence.get("deployment_policy_event_id"))
    if policy is None or policy.kind != "deployment_policy_evaluated":
        missing.append("deployment_policy")
    policy_payload = policy.payload if policy and isinstance(policy.payload, dict) else {}
    if (
        policy_payload.get("policy") not in {"manual", "not_required", "required"}
        or "deployment_policy" not in evidence
        or evidence.get("deployment_policy") != policy_payload.get("policy")
    ):
        missing.append("deployment_policy")
    deployment_payload: dict[str, Any] = {}
    if policy_payload.get("deployment_required") is True:
        deployment = _event_by_id(
            conn, task_id, evidence.get("deployment_record_event_id")
        )
        deployment_payload = (
            deployment.payload
            if deployment and isinstance(deployment.payload, dict)
            else {}
        )
        if deployment is None or deployment.kind != "deployment_recorded":
            missing.extend(["smoke_evidence", "rollback_evidence", "runtime_evidence"])
        else:
            if deployment_payload.get("revision") != evidence.get("integration_sha"):
                missing.append("deployment_revision")
            if not deployment_payload.get("environment"):
                missing.append("deployment_environment")
            if not _release_evidence_succeeded(deployment_payload.get("smoke_result")):
                missing.append("smoke_evidence")
            if not deployment_payload.get("rollback_target"):
                missing.append("rollback_evidence")
            if not _release_evidence_succeeded(
                deployment_payload.get("runtime_evidence")
            ):
                missing.append("runtime_evidence")
    if (
        "smoke_result" not in evidence
        or evidence.get("smoke_result") != deployment_payload.get("smoke_result")
    ):
        missing.append("smoke_evidence")
    if (
        "rollback_target" not in evidence
        or evidence.get("rollback_target")
        != deployment_payload.get("rollback_target")
    ):
        missing.append("rollback_evidence")
    if not str(evidence.get("measurement_note") or "").strip():
        missing.append("measurement_note")
    board_meta = product_board_metadata(_board_slug_for_connection(conn)) or {}
    if (
        _product_workflow_dict(board_meta).get("pull_request_required") is True
        and not evidence.get("pull_request")
    ):
        missing.append("pull_request")
    if missing:
        raise ReleaseEvidenceError(task_id, missing)


def _release_run_is_current(
    conn: sqlite3.Connection,
    task_id: str,
    expected_run_id: Optional[int],
) -> bool:
    """Return whether a worker-owned release still has its exact run lease."""
    if expected_run_id is None:
        return True
    row = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return bool(
        row is not None
        and row["status"] == "running"
        and row["current_run_id"] == int(expected_run_id)
    )


def release_product_task(
    conn: sqlite3.Connection,
    task_id: str,
    board: Optional[str],
    candidate_verify_fn: Optional[Callable[[Path], bool]],
    release_adapter: Optional[Any],
    *,
    measurement_note: Optional[str] = None,
    completion_metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None,
    expected_run_id: Optional[int] = None,
) -> ReleaseResult:
    """Integrate, evaluate deployment policy, then atomically finish a product task."""
    board = board or _board_slug_for_connection(conn)
    task = get_task(conn, task_id)
    meta = product_board_metadata(board)
    is_epic_task = bool(task is not None and task.work_item_kind == "epic")
    lifecycle_scope = conn.execute(
        "SELECT workflow_template_id, current_step_key FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if _is_engine_owned_integration_state(lifecycle_scope):
        raise ReleaseEvidenceError(task_id, ["engine_owned_state"])
    if (
        task is None
        or meta is None
        or not _handoff_v2_enabled(meta)
        or (not is_epic_task and task.current_step_key != "release_measure")
    ):
        raise ReleaseEvidenceError(task_id, ["release_measure_state"])
    if not _release_run_is_current(conn, task_id, expected_run_id):
        return ReleaseResult(False, "completion_conflict")

    def before_apply_fn() -> bool:
        return _release_run_is_current(conn, task_id, expected_run_id)

    repo_value = str(meta.get("default_workdir") or task.workspace_path or "").strip()
    repo_root = _git_toplevel(Path(repo_value).expanduser()) if repo_value else None
    children = list_epic_members(conn, task_id) if _is_epic_task(conn, task_id) else []
    reviewed_candidate = _latest_approved_review_candidate(conn, task_id)
    branch = (
        reviewed_candidate[0]
        if reviewed_candidate is not None
        else str(task.branch_name or "").strip()
    )
    source_sha = _git_ref_sha(repo_root, branch) if repo_root and branch else None
    if repo_root is None or not branch or not source_sha:
        raise ReleaseEvidenceError(task_id, ["reviewed_candidate"])

    evidence: dict[str, Any] = _release_run_evidence(
        conn, task_id, branch, source_sha
    )
    if reviewed_candidate is not None:
        authority_records = _terminal_run_records(conn, task_id)
        passed_test = latest_test_authority(
            authority_records, reviewed_candidate.source_sha
        )
        if passed_test is None:
            raise ReleaseEvidenceError(task_id, ["tester_pass"])
        try:
            candidate_eligibility(repo_root, reviewed_candidate, passed_test)
        except CandidateEligibilityError as exc:
            missing = (
                "reviewed_candidate"
                if exc.code == "stale_review"
                else exc.code
            )
            raise ReleaseEvidenceError(task_id, [missing]) from exc
    evidence.update(source_branch=branch, source_sha=source_sha)
    evidence.update(_release_history_evidence(conn, task_id))
    note = str(measurement_note or "").strip()
    if not note:
        raise ReleaseEvidenceError(task_id, ["measurement_note"])

    # Deterministic gates first, from one metadata snapshot, BEFORE any
    # candidate construction, verification run, or Git integration. These
    # outcomes are already decided by board policy and caller metadata, so
    # evaluating them after integration meant a predictable refusal landed
    # with the target branch already moved.
    workflow = _product_workflow_dict(meta)
    policy_name = str(workflow.get("deployment_policy") or "manual").strip()
    if policy_name not in {"manual", "not_required", "required"}:
        raise ReleaseEvidenceError(task_id, ["deployment_policy"])
    if policy_name == "required" and release_adapter is None:
        with write_txn(conn):
            if not _release_run_is_current(conn, task_id, expected_run_id):
                return ReleaseResult(False, "completion_conflict")
            _append_event(
                conn, task_id, "release_adapter_missing",
                {"policy": policy_name, "source_sha": source_sha},
            )
        return ReleaseResult(False, "release_adapter_missing")
    pr_required = workflow.get("pull_request_required") is True
    pull_request = (
        completion_metadata.get("pull_request")
        if isinstance(completion_metadata, dict)
        else None
    )
    if pr_required and not pull_request:
        raise ReleaseEvidenceError(task_id, ["pull_request"])

    is_epic = _is_epic_task(conn, task_id)
    epic_id = epic_id_for_task(conn, task_id)
    if is_epic:
        integrated_children = all(
            (child := get_task(conn, child_id)) is not None
            and child.status == "done"
            and conn.execute(
                "SELECT 1 FROM epic_story_integrations "
                "WHERE epic_id=? AND story_id=? AND candidate_sha IS NOT NULL "
                "LIMIT 1",
                (task_id, child_id),
            ).fetchone()
            is not None
            for child_id in children
        )
        if not integrated_children:
            raise ReleaseEvidenceError(task_id, ["integrated_children"])
        integration_status = merge_epic_to_main(
            conn,
            task_id,
            board=board,
            board_meta=meta,
            candidate_verify_fn=candidate_verify_fn,
            expected_source_sha=source_sha,
            before_apply_fn=before_apply_fn,
        )
        integration_kinds = {"epic_merged"}
    elif epic_id is not None:
        # Epic-member release is engine-owned and is completed only by the
        # durable integration intent finalizer, never by this legacy release
        # surface.
        raise ReleaseEvidenceError(task_id, ["durable_story_integration"])
    else:
        integration_status = _merge_standalone_story_to_main(
            conn,
            task_id,
            board=board,
            board_meta=meta,
            candidate_verify_fn=candidate_verify_fn,
            expected_source_sha=source_sha,
            before_apply_fn=before_apply_fn,
            allow_release_measure=True,
        )
        integration_kinds = {"story_merged_to_main"}

    if integration_status == "ownership_conflict":
        return ReleaseResult(False, "completion_conflict")
    if integration_status not in {
        "merged", "already_merged", "integrated", "already_integrated"
    }:
        raise ReleaseEvidenceError(task_id, ["integrated_branch"])
    integration = _latest_event_of_kind(conn, task_id, integration_kinds)
    if integration is None or not isinstance(integration.payload, dict):
        raise ReleaseEvidenceError(task_id, ["integrated_branch"])
    integration_sha = str(integration.payload.get("candidate_sha") or "").strip()
    if not integration_sha:
        raise ReleaseEvidenceError(task_id, ["integrated_branch"])
    evidence.update(
        integration_event_id=integration.id,
        integration_sha=integration_sha,
        measurement_note=note,
    )

    with write_txn(conn):
        if not _release_run_is_current(conn, task_id, expected_run_id):
            return ReleaseResult(False, "completion_conflict")
        _append_event(
            conn,
            task_id,
            "deployment_policy_evaluated",
            {
                "policy": policy_name,
                "deployment_required": policy_name == "required",
                "deployment_occurred": False,
            },
        )
    policy_event = _latest_event_of_kind(
        conn, task_id, {"deployment_policy_evaluated"}
    )
    evidence["deployment_policy_event_id"] = policy_event.id if policy_event else None
    evidence["deployment_record_event_id"] = None
    evidence["deployment_policy"] = policy_name
    evidence["smoke_result"] = None
    evidence["rollback_target"] = None

    if policy_name == "required":
        # release_adapter is guaranteed non-None here: the deterministic gate
        # above refuses a required policy without one before integration.
        if not _release_run_is_current(conn, task_id, expected_run_id):
            return ReleaseResult(False, "completion_conflict")
        deployment = release_adapter.release(task_id, integration_sha)
        deployment = deployment if isinstance(deployment, dict) else {}
        deployment_missing: list[str] = []
        if not deployment.get("environment"):
            deployment_missing.append("deployment_environment")
        if deployment.get("revision") != integration_sha:
            deployment_missing.append("deployment_revision")
        if not _release_evidence_succeeded(deployment.get("smoke_result")):
            deployment_missing.append("smoke_evidence")
        if not deployment.get("rollback_target"):
            deployment_missing.append("rollback_evidence")
        if not _release_evidence_succeeded(deployment.get("runtime_evidence")):
            deployment_missing.append("runtime_evidence")
        if deployment_missing:
            raise ReleaseEvidenceError(task_id, deployment_missing)
        with write_txn(conn):
            _append_event(conn, task_id, "deployment_recorded", deployment)
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (
                    json.dumps(
                        {
                            "policy": policy_name,
                            "deployment_required": True,
                            "deployment_occurred": True,
                        }
                    ),
                    policy_event.id,
                ),
            )
        deployment_event = _latest_event_of_kind(
            conn, task_id, {"deployment_recorded"}
        )
        evidence["deployment_record_event_id"] = (
            deployment_event.id if deployment_event else None
        )
        evidence["smoke_result"] = deployment.get("smoke_result")
        evidence["rollback_target"] = deployment.get("rollback_target")

    evidence["pull_request"] = pull_request

    released = complete_task(
        conn,
        task_id,
        result=note,
        summary=note,
        metadata=completion_metadata,
        created_cards=created_cards,
        expected_run_id=expected_run_id,
        board=board,
        board_meta=meta,
        _release_evidence=evidence,
    )
    if not released:
        return ReleaseResult(False, "completion_conflict")
    return ReleaseResult(
        True,
        "released",
        integration.id,
        integration_sha,
        policy_event.id if policy_event else None,
        evidence.get("deployment_record_event_id"),
    )


# ---------------------------------------------------------------------------
# deploy_epic / notify_operations -- Hermes-run test->preprod deploy of a
# merged epic via an injected Ops API client, smoke-gated, then one
# #operations release notice (Phase 5, T5.1-T5.3).
#
# THE HARD BOUNDARY: this loop deploys test + pre-prod ONLY -- NEVER
# production, and NEVER `git push` / touches a git remote or origin. The
# real container Ops API adapter (HTTP/gh, feat/container-ops-api / PR #3)
# is deliberately not built here; ``ops_client`` must be injected, and the
# default stub below raises rather than silently no-op'ing.
# ---------------------------------------------------------------------------

_DEPLOYABLE_ENVS = {"test", "preprod"}


class OpsClient(Protocol):
    """Container Ops API surface Hermes deploys through.

    Concrete real implementation lives in feat/container-ops-api / PR #3 --
    out of scope here. Tests inject a mock; production code must inject a
    real client explicitly (see :class:`_DefaultOpsClient`).
    """

    def build_roll(self, env: str) -> Any:
        """Build/refresh the container(s) for ``env``. Raise on failure."""
        ...

    def smoke(self, env: str) -> bool:
        """Smoke/verify ``env``. ``True`` means healthy."""
        ...


class _DefaultOpsClient:
    """Stub Ops client used when no ``ops_client`` is injected.

    Both methods raise ``NotImplementedError`` -- the real HTTP/gh adapter
    is deferred to ``feat/container-ops-api`` (PR #3) and is intentionally
    NOT built by this task. Any caller relying on the default in
    production gets a loud, immediate failure instead of a silent no-op;
    tests always inject a mock ``ops_client``.
    """

    def build_roll(self, env: str) -> Any:
        raise NotImplementedError(
            "real Ops API adapter not wired — see feat/container-ops-api / PR #3"
        )

    def smoke(self, env: str) -> bool:
        raise NotImplementedError(
            "real Ops API adapter not wired — see feat/container-ops-api / PR #3"
        )


def _commit_range_for_epic(
    conn: sqlite3.Connection, epic_id: str, board: Optional[str]
) -> Optional[str]:
    """Best-effort ``pre_sha..HEAD`` commit range for the #operations notice.

    ``pre_sha`` comes from the epic's latest ``epic_merged`` event (recorded
    by :func:`merge_epic_to_main`); HEAD is resolved via a single LOCAL
    ``git rev-parse main`` -- no remote/origin touched. Returns ``None`` if
    either half can't be resolved (best-effort, per the brief).
    """
    pre_sha: Optional[str] = None
    for event in reversed(list_events(conn, epic_id)):
        if event.kind == "epic_merged" and isinstance(event.payload, dict):
            candidate = event.payload.get("pre_sha")
            if candidate:
                pre_sha = str(candidate)
                break
    if not pre_sha:
        return None
    try:
        meta = product_board_metadata(board)
        board_default = str((meta or {}).get("default_workdir") or "").strip()
        repo_root = _git_toplevel(Path(board_default).expanduser()) if board_default else None
        if repo_root is None:
            return None
        head_result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "main"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if head_result.returncode != 0:
            return None
        head_sha = (head_result.stdout or "").strip()
        if not head_sha:
            return None
    except Exception:
        return None
    return f"{pre_sha[:12]}..{head_sha[:12]}"


def notify_operations(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    envs_status: list[dict],
    failure: bool = False,
    reason: Optional[str] = None,
    notify_fn: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Build ONE #operations release/page message for ``epic_id`` and post it.

    Contains the epic title, stories shipped (id + title, via
    :func:`child_ids` / :func:`get_task`), the best-effort commit range
    (:func:`_commit_range_for_epic`), and the per-env build/smoke
    ``envs_status``. On ``failure`` the message additionally carries
    ``reason`` (this is the page). Calls ``notify_fn(message)`` if provided
    (the real Slack #operations hook wiring is separate and out of scope);
    any ``notify_fn`` exception is swallowed so a broken notifier can never
    blow up the deploy. Returns the message dict either way.
    """
    epic = get_task(conn, epic_id)
    epic_title = epic.title if epic is not None else epic_id
    stories = []
    for child_id in list_epic_members(conn, epic_id):
        child = get_task(conn, child_id)
        stories.append({"id": child_id, "title": child.title if child is not None else None})

    message: dict = {
        "channel": "#operations",
        "epic_id": epic_id,
        "epic_title": epic_title,
        "stories": stories,
        "commit_range": _commit_range_for_epic(conn, epic_id, board),
        "envs_status": envs_status,
        "failure": failure,
        "reason": reason if failure else None,
    }
    if notify_fn is not None:
        try:
            notify_fn(message)
        except Exception:
            pass
    return message


def deploy_epic(
    conn: sqlite3.Connection,
    epic_id: str,
    *,
    board: Optional[str] = None,
    envs: tuple[str, ...] = ("test", "preprod"),
    ops_client: Optional[OpsClient] = None,
    notify_fn: Optional[Callable[[dict], None]] = None,
) -> Optional[dict]:
    """Deploy a merged epic to ``envs`` (default test then preprod) via the
    injected Ops API client, smoke-gated, then post one #operations notice.

    Returns ``None`` on a non-``handoff_v2`` board (nothing to do here).
    Deploys ``envs`` IN ORDER (test before preprod): for each env,
    ``ops_client.build_roll(env)`` then (if built) ``ops_client.smoke(env)``.
    The first build/smoke failure STOPS the loop -- no later env is ever
    deployed (this is the "test smoke fails -> do not proceed to preprod"
    gate) -- blocks the epic (:func:`set_running` False + :func:`set_blocked`
    True), emits a ``deploy_failed`` event, pages #operations via
    :func:`notify_operations` (``failure=True``), and returns the message.
    On full success, emits a ``deployed`` event, posts one release notice
    via :func:`notify_operations` (``failure=False``), and returns the
    message.
    """
    # =========================================================================
    # BOUNDARY (T5.3): test + pre-prod ONLY -- NEVER production, and NEVER
    # `git push` / any git remote-or-origin verb. This function only calls
    # the injected `ops_client` and (via notify_operations ->
    # _commit_range_for_epic) reads git LOCALLY (`rev-parse main`) for the
    # commit range. Production deploys and `git push origin` remain
    # HUMAN-ONLY and are simply not reachable from this code path.
    # =========================================================================
    meta = product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return None

    unsupported = [env for env in envs if env not in _DEPLOYABLE_ENVS]
    if unsupported:
        raise ValueError(
            f"deploy_epic: envs must be a subset of {sorted(_DEPLOYABLE_ENVS)}; "
            f"got unsupported env(s) {unsupported!r} -- production deploys are "
            "human-only and are never reachable via deploy_epic"
        )

    client: OpsClient = ops_client if ops_client is not None else _DefaultOpsClient()
    envs_status: list[dict] = []

    for env in envs:
        built = False
        detail: Optional[str] = None
        try:
            client.build_roll(env)
            built = True
        except Exception as exc:
            detail = str(exc)

        smoke_ok = False
        if built:
            try:
                smoke_ok = bool(client.smoke(env))
                if not smoke_ok:
                    detail = "smoke check failed"
            except Exception as exc:
                smoke_ok = False
                detail = str(exc)

        envs_status.append(
            {"env": env, "built": built, "smoke_ok": smoke_ok, "detail": detail}
        )

        if not built or not smoke_ok:
            stage = "build" if not built else "smoke"
            reason = f"deploy: {env} {stage} failed"
            set_running(conn, epic_id, False, board=board)
            set_blocked(conn, epic_id, True, board=board, reason=reason)
            try:
                with write_txn(conn):
                    _append_event(
                        conn, epic_id, "deploy_failed",
                        {"env": env, "stage": stage, "envs_status": envs_status},
                    )
            except Exception:
                pass
            return notify_operations(
                conn, epic_id, board=board, envs_status=envs_status,
                failure=True, reason=reason, notify_fn=notify_fn,
            )

    try:
        with write_txn(conn):
            _append_event(conn, epic_id, "deployed", {"envs_status": envs_status})
    except Exception:
        pass
    return notify_operations(
        conn, epic_id, board=board, envs_status=envs_status,
        failure=False, notify_fn=notify_fn,
    )


def resolve_workspace(
    task: Task,
    *,
    board: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    - ``scratch``: a fresh dir under ``<board-root>/workspaces/<id>/``,
      where ``<board-root>`` is the active board's root. The path is the
      same for the dispatcher and every profile worker, so handoff is
      path-stable.
    - ``dir:<path>``: the path stored in ``workspace_path``.  Created
      if missing.  MUST be absolute — relative paths are rejected to
      prevent confused-deputy traversal where ``../../../tmp/attacker``
      resolves against the dispatcher's CWD instead of a meaningful
      root.  Users who want a kanban-root-relative workspace should
      compute the absolute path themselves.
    - ``worktree``: a real linked git worktree. If ``workspace_path`` names
      a repo root, Hermes treats it as an anchor and materializes a linked
      worktree at ``<repo>/.worktrees/<task-id>``. If ``workspace_path`` names
      a concrete target path, Hermes creates/reuses that linked worktree. With
      no ``workspace_path``, Hermes anchors on the board's ``default_workdir``
      and materializes ``<repo>/.worktrees/<task-id>`` per task; if no
      ``default_workdir`` is configured it raises rather than guessing from the
      dispatcher's CWD. When ``branch_name`` is empty, Hermes uses
      ``wt/<task-id>``.

    Persist the resolved path back to the task row via ``set_workspace_path``
    so subsequent runs reuse the same directory.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "scratch":
        if task.workspace_path:
            # Legacy scratch tasks that were set to an explicit path get the
            # same absolute-path guard as dir: — consistent with the
            # threat model.
            p = Path(task.workspace_path).expanduser()
            if not p.is_absolute():
                raise ValueError(
                    f"task {task.id} has non-absolute workspace_path "
                    f"{task.workspace_path!r}; workspace paths must be absolute"
                )
        else:
            p = workspaces_root(board=board) / task.id
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "dir":
        if not task.workspace_path:
            raise ValueError(
                f"task {task.id} has workspace_kind=dir but no workspace_path"
            )
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "worktree":
        p, _branch_name = _resolve_worktree_workspace(
            task, board=board, conn=conn
        )
        return p
    raise ValueError(f"unknown workspace_kind: {kind}")


def set_workspace_path(
    conn: sqlite3.Connection, task_id: str, path: Path | str
) -> None:
    path = str(path)
    _validate_resolver_cas_fields({"workspace_path": path})
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (path, task_id),
        )


def set_branch_name(
    conn: sqlite3.Connection, task_id: str, branch_name: str
) -> None:
    branch_name = str(branch_name)
    _validate_resolver_cas_fields({"branch_name": branch_name})
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET branch_name = ? WHERE id = ?",
            (branch_name, task_id),
        )


def _persist_source_completion_metadata(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    *,
    intent: dict[str, Any],
    receipt: Optional[dict[str, Any]] = None,
) -> None:
    """CAS-write one completion intent and optional receipt onto the owned run."""
    with write_txn(conn):
        row = conn.execute(
            "SELECT r.metadata FROM tasks t JOIN task_runs r "
            "ON r.id = t.current_run_id "
            "WHERE t.id = ? AND t.current_run_id = ? AND r.ended_at IS NULL",
            (task_id, int(run_id)),
        ).fetchone()
        if row is None:
            raise _SourceCommitError("run_changed")
        try:
            run_metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            run_metadata = {}
        if not isinstance(run_metadata, dict):
            run_metadata = {}
        run_metadata["source_completion_intent"] = dict(intent)
        if receipt is not None:
            run_metadata["source_completion_receipt"] = dict(receipt)
        updated = conn.execute(
            "UPDATE task_runs SET metadata = ? "
            "WHERE id = ? AND task_id = ? AND ended_at IS NULL",
            (json.dumps(run_metadata, ensure_ascii=False), int(run_id), task_id),
        )
        if updated.rowcount != 1:
            raise _SourceCommitError("run_changed")


def _commit_worker_diff(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    message: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> Optional[str]:
    """Commit a card's worktree, source-only, and return the new SHA.

    Stages with ``git add -A`` (honors ``.gitignore`` — gitignored runtime
    dirs like ``dashboard/``/``state/`` are never staged; never ``-f``).
    Returns ``None`` if there's no ``workspace_path``, the path isn't a git
    repo, there's nothing to commit (clean tree), or any git call fails.
    This is load-bearing for T2.2: no commit means the card does not advance.
    """
    row = conn.execute(
        "SELECT title, workspace_path, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    strict = expected_run_id is not None
    if strict and row["current_run_id"] != int(expected_run_id):
        raise _SourceCommitError("run_changed")
    workspace_path: Optional[str] = row["workspace_path"]
    if not workspace_path:
        if strict:
            raise _SourceCommitError("missing_workspace")
        return None
    repo_root = _git_toplevel(Path(workspace_path))
    if repo_root is None:
        if strict:
            raise _SourceCommitError("not_a_git_repository")
        return None

    def git(
        *args: str, ok_codes: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            if strict:
                raise _SourceCommitError("git_failed", str(exc)) from exc
            raise
        if completed.returncode not in ok_codes and strict:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise _SourceCommitError("git_failed", detail)
        return completed

    try:
        add_result = git("add", "-A")
    except Exception:
        if strict:
            raise
        return None
    if add_result.returncode != 0:
        return None

    try:
        diff_result = git("diff", "--cached", "--quiet", ok_codes=(0, 1))
    except Exception:
        if strict:
            raise
        return None
    if diff_result.returncode == 0:
        if strict:
            intent_row = conn.execute(
                "SELECT id, metadata FROM task_runs WHERE task_id = ? "
                "AND id != ? ORDER BY id DESC",
                (task_id, int(expected_run_id)),
            ).fetchall()
            head = git("rev-parse", "HEAD").stdout.strip()
            for prior in intent_row:
                try:
                    prior_metadata = json.loads(prior["metadata"] or "{}")
                except (TypeError, ValueError):
                    continue
                intent = (
                    prior_metadata.get("source_completion_intent")
                    if isinstance(prior_metadata, dict)
                    else None
                )
                if not isinstance(intent, dict):
                    continue
                base_sha = str(intent.get("base_sha") or "")
                tree_sha = str(intent.get("tree_sha") or "")
                parent = git("rev-parse", "HEAD^").stdout.strip()
                head_tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
                if parent != base_sha or head_tree != tree_sha:
                    continue
                paths = [
                    path
                    for path in git(
                        "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
                    ).stdout.splitlines()
                    if path
                ]
                if paths != intent.get("paths"):
                    continue
                receipt = {
                    "intent_id": intent["intent_id"],
                    "intent_run_id": int(prior["id"]),
                    "run_id": int(expected_run_id),
                    "base_sha": base_sha,
                    "commit_sha": head,
                    "tree_sha": tree_sha,
                    "diff_digest": intent["diff_digest"],
                    "paths": paths,
                    "created_at": int(time.time()),
                    "adopted": True,
                }
                _persist_source_completion_metadata(
                    conn,
                    task_id,
                    int(expected_run_id),
                    intent=intent,
                    receipt=receipt,
                )
                return head
            raise _SourceCommitError("nothing_to_commit")
        return None

    intent: Optional[dict[str, Any]] = None
    if strict:
        base_sha = git("rev-parse", "HEAD").stdout.strip()
        tree_sha = git("write-tree").stdout.strip()
        paths = [
            path
            for path in git("diff", "--cached", "--name-only", base_sha).stdout.splitlines()
            if path
        ]
        diff_bytes = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--cached", "--binary", base_sha],
            capture_output=True,
            timeout=30,
            check=True,
        ).stdout
        intent = {
            "intent_id": secrets.token_hex(16),
            "run_id": int(expected_run_id),
            "base_sha": base_sha,
            "tree_sha": tree_sha,
            "diff_digest": hashlib.sha256(diff_bytes).hexdigest(),
            "paths": paths,
            "created_at": int(time.time()),
        }
        _persist_source_completion_metadata(
            conn, task_id, int(expected_run_id), intent=intent
        )

    if message is not None:
        commit_message = message
    else:
        title: Optional[str] = row["title"]
        commit_message = f"handoff: {title} ({task_id})" if title else f"handoff: {task_id}"

    try:
        commit_result = git("commit", "-m", commit_message)
    except Exception:
        if strict:
            raise
        return None
    if commit_result.returncode != 0:
        return None

    try:
        sha_result = git("rev-parse", "HEAD")
    except Exception:
        if strict:
            raise
        return None
    if sha_result.returncode != 0:
        return None
    sha = (sha_result.stdout or "").strip()
    if strict and sha and intent is not None:
        receipt = {
            "intent_id": intent["intent_id"],
            "intent_run_id": int(expected_run_id),
            "run_id": int(expected_run_id),
            "base_sha": intent["base_sha"],
            "commit_sha": sha,
            "tree_sha": intent["tree_sha"],
            "diff_digest": intent["diff_digest"],
            "paths": intent["paths"],
            "created_at": int(time.time()),
            "adopted": False,
        }
        _persist_source_completion_metadata(
            conn,
            task_id,
            int(expected_run_id),
            intent=intent,
            receipt=receipt,
        )
    return sha or None


def record_generated_mutations(
    conn: sqlite3.Connection,
    run_id: int,
    declared_generated: Iterable[object],
    *,
    metadata: Optional[dict] = None,
) -> dict:
    """Persist the generated-file observation before restoring its paths."""
    run = get_run(conn, run_id)
    if run is None or run.ended_at is not None:
        raise EvidenceWorkspaceError("run_changed")
    active_metadata = run.metadata if isinstance(run.metadata, dict) else {}
    current = dict(active_metadata)
    if metadata is not None:
        # Keep the active dispatcher snapshot as the base so the returned
        # completion metadata retains the worker's outcome/provenance while
        # ``_end_run`` can still compare it with the untouched pin.
        current.update(metadata)
    evidence = current.get("evidence_workspace")
    evidence_payload = dict(evidence) if isinstance(evidence, dict) else {}
    evidence_payload["declared_generated"] = [
        path.as_posix() if hasattr(path, "as_posix") else str(path)
        for path in declared_generated
    ]
    current["evidence_workspace"] = evidence_payload
    with write_txn(conn):
        _append_event(
            conn,
            run.task_id,
            "evidence_generated_mutations",
            {
                "run_id": int(run_id),
                "paths": list(evidence_payload["declared_generated"]),
            },
            run_id=int(run_id),
        )
    return current


def _evidence_generated_paths(
    board: Optional[str],
    workspace: Path,
    error_type: type[RuntimeError],
) -> tuple:
    """Load the board-owned generated-path allowlist for an evidence run."""
    meta = product_board_metadata(board) or {}
    if "repository" not in meta:
        return ()
    try:
        contract = load_repository_contract(meta, repo_root=workspace)
    except RepositoryConfigurationError as exc:
        raise error_type(f"repository contract: {exc}") from exc
    return contract.generated_paths


def _latest_test_target(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[dict[str, str]]:
    """Return the latest ordinary successful Test pin, never an older success."""
    records = _terminal_run_records(conn, task_id)
    latest = next((record for record in reversed(records) if record.phase == "test"), None)
    if latest is None:
        return None
    passed = latest_test_authority(records, latest.test_head_sha or "")
    if passed is None:
        return {}
    return {"test_branch": passed.branch, "test_head_sha": passed.source_sha}


def _evidence_pin(
    conn: sqlite3.Connection,
    task_id: str,
    step: str,
) -> tuple[Optional[str], Optional[str], Optional[int], Optional[Path]]:
    """Read the dispatcher-owned pin from the active evidence run."""
    task = get_task(conn, task_id)
    if task is None or task.current_run_id is None or not task.workspace_path:
        return None, None, None, None
    run = get_run(conn, task.current_run_id)
    if run is None or run.ended_at is not None or not isinstance(run.metadata, dict):
        return None, None, None, None
    head = str(run.metadata.get(f"{step}_head_sha") or "").strip()
    branch = str(run.metadata.get(f"{step}_branch") or "").strip()
    if not _FULL_GIT_SHA_RE.fullmatch(head) or not branch:
        return None, None, run.id, Path(task.workspace_path).expanduser().resolve(strict=False)
    return head, branch, run.id, Path(task.workspace_path).expanduser().resolve(strict=False)


def handoff(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    expected_run_id: Optional[int] = None,
    expected_phase: Optional[str] = None,
) -> bool:
    """Atomically advance a handoff_v2 product card.

    Self-contained v2 path (T2.2-T2.4): does NOT call or mutate
    ``_complete_product_workflow_step`` / :func:`complete_task` -- those
    remain the legacy completion path, byte-for-byte unchanged.

    Order: gate on ``handoff_v2`` -> resolve the current step's transition
    (no transition, e.g. the terminal ``release_measure`` step, means no
    auto-advance -- returns ``False``, nothing touched) -> validate AI
    provenance (raises :class:`ProductProvenanceError` on failure, card
    untouched) -> when ``expected_run_id`` is given, a run-ownership
    precondition check (before any commit, so a reclaimed worker can't
    create a stale commit) -> commit Development's source diff; Test/Review
    inspect the dispatcher-pinned evidence workspace and never author source
    commits -> one atomic
    transaction: advance the phase, clear ``running``, retag the assignee,
    sync the legacy ``status``, and emit exactly one ``handoff`` event
    carrying the optional commit SHA. The advance UPDATE re-checks run ownership
    via a CAS (``AND current_run_id = ?``) to guard the window between the
    precondition check and the write.

    Returns ``False`` (no mutation) when the board isn't handoff_v2, the
    card doesn't exist, its current step has no advancing transition, a
    source-producing step has no commit SHA, or (when ``expected_run_id`` is
    given) the card's run ownership was lost before the commit or before the
    advance.
    """
    meta = product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return False

    row = conn.execute(
        "SELECT current_step_key, status, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    _validate_stored_product_workflow_state(conn, task_id)
    step = row["current_step_key"]
    transition = PRODUCT_WORKFLOW_TRANSITIONS.get(str(step or ""))
    if not transition or not transition.get("next_step"):
        return False

    if expected_run_id is not None and (
        row["status"] != "running" or row["current_run_id"] != expected_run_id
    ):
        # Ownership was reclaimed out from under this worker -- refuse
        # before the commit-first gate so we never create a stale commit.
        return False
    if expected_phase is not None and row["current_step_key"] != expected_phase:
        # A structured positive verdict was validated for a different phase.
        # Refuse before the commit-first gate so a same-run set_phase cannot
        # carry Test evidence through Review (or vice versa).
        return False

    metadata = _canonicalize_product_ai_provenance(
        conn, task_id, step, metadata,
    )
    _validate_product_ai_provenance(conn, task_id, step, metadata, meta)

    sha: Optional[str] = None
    if str(step or "") in {"test", "review"}:
        pinned_sha, pinned_branch, run_id, workspace = _evidence_pin(
            conn, task_id, str(step)
        )
        if (run_id is not None or workspace is not None) and (
            pinned_sha is None
            or not pinned_branch
            or run_id is None
            or workspace is None
        ):
            raise EvidenceWorkspaceError("missing_pin")
        if pinned_sha is not None:
            if run_id is None or workspace is None:
                raise EvidenceWorkspaceError("missing_pin")
            generated_paths = _evidence_generated_paths(
                board,
                workspace,
                ReviewTargetPreparationError if str(step) == "review" else TestTargetPreparationError,
            )
            observed = inspect_evidence_workspace(
                workspace,
                pinned_sha,
                generated_paths,
            )
            if (
                observed.branch != pinned_branch
                or observed.branch_head != pinned_sha
            ):
                raise EvidenceWorkspaceError("source_moved")
            if observed.undeclared_tracked:
                raise EvidenceWorkspaceError(
                    "source_moved",
                    ", ".join(observed.undeclared_tracked),
                )
            if observed.untracked:
                raise EvidenceWorkspaceError(
                    "untracked_output",
                    ", ".join(observed.untracked),
                )
            evidence_metadata = dict(metadata or {})
            evidence_metadata["evidence_workspace"] = {
                "branch": observed.branch,
                "branch_head": observed.branch_head,
                "pinned_sha": pinned_sha,
            }
            metadata = record_generated_mutations(
                conn,
                run_id,
                observed.declared_generated,
                metadata=evidence_metadata,
            )
            if observed.declared_generated:
                restore_generated_paths(
                    workspace,
                    pinned_sha,
                    observed.declared_generated,
                )
            sha = pinned_sha
    else:
        sha = _commit_worker_diff(conn, task_id)
        if sha is None and str(step or "") == "development":
            repair_payload = _latest_resolver_repair_payload(conn, task_id) or {}
            repair = repair_payload.get("repair")
            adopted_sha = (
                repair.get("adopt_handoff_sha")
                if isinstance(repair, dict)
                else None
            )
            if adopted_sha:
                try:
                    sha = _validate_adopted_handoff_sha(
                        conn, task_id, str(adopted_sha),
                    )
                except ValueError:
                    return False
    if sha is None and str(step or "") in _PRODUCT_COMMIT_REQUIRED_STEPS:
        return False

    if str(step or "") == "review":
        epic_id = epic_id_for_task(conn, task_id)
        if epic_id is not None:
            if expected_run_id is None:
                return False
            active_run = get_run(conn, expected_run_id)
            if (
                active_run is None
                or active_run.ended_at is not None
                or not isinstance(active_run.metadata, dict)
            ):
                return False
            final_metadata = dict(metadata or {})
            for key in ("review_branch", "review_base_sha", "review_head_sha"):
                pinned = active_run.metadata.get(key)
                if pinned is not None:
                    final_metadata[key] = pinned
            approved = ApprovedCandidate(
                run_id=expected_run_id,
                branch=str(final_metadata.get("review_branch") or "").strip(),
                base_sha=str(final_metadata.get("review_base_sha") or "").strip(),
                source_sha=str(final_metadata.get("review_head_sha") or "").strip(),
                reviewer_provider=str(
                    _reviewer_agent_from_metadata(final_metadata) or ""
                ).strip(),
                writer_provider=str(
                    _writer_agent_from_metadata(final_metadata) or ""
                ).strip(),
            )
            authority_records = _terminal_run_records(conn, task_id)
            passed = latest_test_authority(authority_records, approved.source_sha)
            if passed is None or workspace is None:
                return False
            try:
                eligibility: CandidateEligibility = candidate_eligibility(
                    workspace, approved, passed
                )
            except CandidateEligibilityError:
                return False
            from hermes_cli.kanban_story_integration import enqueue_approved_story

            enqueue_approved_story(
                conn,
                epic_id=epic_id,
                story_id=task_id,
                approved=approved,
                passed=passed,
                eligibility=eligibility,
                expected_run_id=expected_run_id,
                summary=summary,
                metadata=metadata,
            )
            return True

    next_step = transition["next_step"]
    next_role = transition.get("assignee_role")
    next_assignee = _product_role_assignee(meta, next_role)

    try:
        with authorized_governance_write(), write_txn(conn):
            sql = (
            # Release the completing worker's claim as part of the atomic advance:
            # the card is being handed to a NEW assignee, so the old claim is dead.
            # Without this, the handed-off card stays ready+claimed, which
            # spawn_after_handoff (WHERE claim_lock IS NULL) skips -> the next
            # agent never fires and the event-driven chain stalls every handoff.
            "UPDATE tasks SET current_step_key = ?, running = 0, assignee = ?, result = ?, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ?"
            )
            params: list[Any] = [next_step, next_assignee, summary, task_id]
            if expected_run_id is not None:
                sql += " AND status = 'running' AND current_run_id = ?"
                params.append(int(expected_run_id))
            if expected_phase is not None:
                sql += " AND current_step_key = ?"
                params.append(expected_phase)
            cur = conn.execute(sql, tuple(params))
            if cur.rowcount != 1:
                raise RuntimeError("handoff run ownership changed")
            _sync_legacy_status(conn, task_id, meta)
            if next_step == "development":
                release_measure_unblocks = (
                    _product_release_measure_unblocks_dependents(meta)
                )
                parents = conn.execute(
                    "SELECT p.status, p.workflow_template_id, p.current_step_key "
                    "FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                    "WHERE l.child_id = ?",
                    (task_id,),
                ).fetchall()
                if any(
                    not _dependency_parent_satisfied(
                        parent,
                        release_measure_unblocks=release_measure_unblocks,
                    )
                    for parent in parents
                ):
                    conn.execute(
                        "UPDATE tasks SET status = 'todo' "
                        "WHERE id = ? AND status = 'ready'",
                        (task_id,),
                    )
            run_id = _end_run(
                conn,
                task_id,
                outcome="advanced",
                status="completed",
                summary=summary,
                metadata=metadata,
                expected_run_id=expected_run_id,
            )
            if expected_run_id is not None and run_id is None:
                raise RuntimeError("handoff run ownership changed")
            if str(step or "") == "development" and sha is not None:
                resolve_rework_directive(
                    conn,
                    task_id,
                    new_sha=sha,
                    resolved_by_run_id=run_id,
                )
            _append_event(
                conn,
                task_id,
                "handoff",
                {
                    "from_step": step,
                    "to_step": next_step,
                    "sha": sha,
                    "assignee": next_assignee,
                    "summary": summary,
                },
                run_id=run_id,
            )
    except RuntimeError as exc:
        if str(exc) == "handoff run ownership changed":
            return False
        raise
    return True


_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ReviewTargetPreparationError(RuntimeError):
    """Reviewer input could not be pinned safely before worker launch."""


class TestTargetPreparationError(RuntimeError):
    """Tester input could not be pinned safely before worker launch."""


class WorkerRuntimeIdentityError(RuntimeError):
    """A governed worker runtime could not be identified canonically."""


def _review_git_output(workspace: Path, *args: str) -> str:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise ReviewTargetPreparationError("git executable is unavailable")
    try:
        result = subprocess.run(
            [git_executable, "-C", str(workspace), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewTargetPreparationError(
            f"git {' '.join(args)} failed: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown git error").strip()
        raise ReviewTargetPreparationError(
            f"git {' '.join(args)} failed: {detail[:300]}"
        )
    return (result.stdout or "").strip()


def _review_target_branch(
    conn: sqlite3.Connection,
    task_id: str,
    workspace: Path,
    *,
    board: Optional[str] = None,
) -> str:
    """Resolve the board-owned branch against which Reviewer compares."""
    story_base = _story_base_branch(conn, task_id, board=board)
    if story_base:
        return story_base
    meta = product_board_metadata(board) or {}
    board_checkout = str(meta.get("default_workdir") or "").strip()
    if not board_checkout:
        raise ReviewTargetPreparationError(
            "product board has no target checkout"
        )
    checkout_root = _git_toplevel(Path(board_checkout).expanduser())
    if checkout_root is None:
        raise ReviewTargetPreparationError(
            "product board target checkout is not a git repository"
        )
    if _git_common_dir(checkout_root) != _git_common_dir(workspace):
        raise ReviewTargetPreparationError(
            "product board target checkout does not match the task repository"
        )
    target_branch = _git_current_branch(checkout_root)
    if not target_branch:
        raise ReviewTargetPreparationError(
            "product board target checkout has no active branch"
        )
    return target_branch


def _prepare_review_target(
    conn: sqlite3.Connection,
    task_id: str,
    workspace: Path | str,
    *,
    board: Optional[str] = None,
    default_review_status: bool = False,
) -> Optional[dict[str, str]]:
    """Pin immutable base/head commits into the active review run."""
    task = get_task(conn, task_id)
    if task is None:
        raise ReviewTargetPreparationError(f"task {task_id} not found")
    product_review = (
        task.workflow_template_id == "product" and task.current_step_key == "review"
    )
    default_review = (
        default_review_status
        and task.workflow_template_id is None
        and task.current_step_key in (None, "review")
    )
    if not product_review and not default_review:
        return None
    if default_review and (task.assignee != "reviewer" or not task.source_commit_forbidden):
        raise ReviewTargetPreparationError(
            "Default review requires reviewer ownership and forbidden source commits"
        )
    if not task.workspace_path:
        raise ReviewTargetPreparationError("task has no workspace path")
    try:
        expected_workspace = Path(task.workspace_path).expanduser().resolve(strict=True)
        actual_workspace = Path(workspace).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ReviewTargetPreparationError(f"workspace is unavailable: {exc}") from exc
    if actual_workspace != expected_workspace:
        raise ReviewTargetPreparationError(
            f"launch workspace {actual_workspace} does not match task workspace "
            f"{expected_workspace}"
        )
    if not actual_workspace.is_dir():
        raise ReviewTargetPreparationError(
            f"task workspace is not a directory: {actual_workspace}"
        )
    if task.current_run_id is None:
        raise ReviewTargetPreparationError("task has no active review run")
    run = get_run(conn, task.current_run_id)
    if (
        run is None
        or run.task_id != task_id
        or run.ended_at is not None
        or run.status != "running"
        or run.profile != "reviewer"
        or run.step_key != ("review" if task.current_step_key == "review" else None)
    ):
        raise ReviewTargetPreparationError("active run is not the current review run")

    dirty = _review_git_output(
        actual_workspace, "status", "--porcelain", "--untracked-files=all"
    )
    if dirty:
        raise ReviewTargetPreparationError("review workspace is dirty or uncommitted")
    workspace_branch = (_git_current_branch(actual_workspace) or "").strip()
    if not workspace_branch:
        raise ReviewTargetPreparationError("review workspace has no active branch")
    if task.branch_name and task.branch_name != workspace_branch:
        raise ReviewTargetPreparationError("review workspace branch does not match task branch")
    head_sha = _review_git_output(
        actual_workspace, "rev-parse", "--verify", "HEAD^{commit}"
    )
    predecessor_base = ""
    if product_review:
        tested_target = _latest_test_target(conn, task_id)
        if not tested_target:
            raise ReviewTargetPreparationError("product Review requires a successful applicable Test pin")
        if tested_target["test_branch"] != workspace_branch:
            raise ReviewTargetPreparationError("review branch does not match tested branch")
        if tested_target["test_head_sha"] != head_sha:
            raise ReviewTargetPreparationError("review head does not match tested SHA")
        base_ref = _review_target_branch(
            conn, task_id, actual_workspace, board=board
        )
    else:
        if not task.branch_name:
            raise ReviewTargetPreparationError(
                "Default review execution contract has no task branch binding"
            )
        predecessor = conn.execute(
            "SELECT json_extract(r.metadata, '$.candidate_sha') AS candidate_sha, "
            "json_extract(r.metadata, '$.review_base_sha') AS review_base_sha "
            "FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "JOIN task_runs r ON r.task_id = p.id "
            "WHERE l.child_id = ? AND p.status = 'done' "
            "AND r.ended_at IS NOT NULL AND r.outcome = 'completed' "
            "AND p.workspace_path = ? AND p.branch_name = ? "
            "AND json_valid(COALESCE(r.metadata, '{}')) "
            "ORDER BY p.completed_at DESC, r.ended_at DESC, r.id DESC LIMIT 1",
            (task_id, task.workspace_path, task.branch_name),
        ).fetchone()
        if predecessor is None:
            raise ReviewTargetPreparationError(
                "Default review execution contract has no completed predecessor target"
            )
        candidate_sha = str(predecessor["candidate_sha"] or "").strip()
        if not _FULL_GIT_SHA_RE.fullmatch(candidate_sha):
            raise ReviewTargetPreparationError(
                "Default review predecessor has no full candidate SHA"
            )
        resolved_candidate = _review_git_output(
            actual_workspace,
            "rev-parse",
            "--verify",
            f"{candidate_sha}^{{commit}}",
        )
        if resolved_candidate != candidate_sha or candidate_sha != head_sha:
            raise ReviewTargetPreparationError(
                "Default review head does not match the completed predecessor candidate"
            )
        predecessor_base = str(predecessor["review_base_sha"] or "").strip()
        if predecessor_base:
            if not _FULL_GIT_SHA_RE.fullmatch(predecessor_base):
                raise ReviewTargetPreparationError(
                    "Default review predecessor has no full review base SHA"
                )
            resolved_base = _review_git_output(
                actual_workspace,
                "rev-parse",
                "--verify",
                f"{predecessor_base}^{{commit}}",
            )
            if resolved_base != predecessor_base:
                raise ReviewTargetPreparationError(
                    "Default review predecessor base does not resolve exactly"
                )
            base_ref = predecessor_base
        else:
            board_meta = read_board_metadata(board)
            try:
                contract = repository_contract_for_metadata(board_meta)
            except RepositoryConfigurationError as exc:
                raise ReviewTargetPreparationError(
                    f"Default review repository binding is invalid: {exc.code}"
                ) from exc
            if contract is None:
                raise ReviewTargetPreparationError(
                    "Default review execution contract has no repository binding"
                )
            if _git_common_dir(contract.repo_root) != _git_common_dir(actual_workspace):
                raise ReviewTargetPreparationError(
                    "Default review workspace does not match the configured repository"
                )
            base_ref = contract.base_ref
    base_sha = _review_git_output(
        actual_workspace, "merge-base", base_ref, head_sha
    )
    if not _FULL_GIT_SHA_RE.fullmatch(base_sha):
        raise ReviewTargetPreparationError("review base is not a full commit SHA")
    if not _FULL_GIT_SHA_RE.fullmatch(head_sha):
        raise ReviewTargetPreparationError("review head is not a full commit SHA")
    if default_review and predecessor_base and base_sha != predecessor_base:
        raise ReviewTargetPreparationError(
            "Default review predecessor base is not an ancestor of the candidate"
        )
    review_branch = workspace_branch
    _evidence_generated_paths(board, actual_workspace, ReviewTargetPreparationError)

    metadata = dict(run.metadata or {})
    metadata.update(
        {
            "review_branch": review_branch,
            "review_base_sha": base_sha,
            "review_head_sha": head_sha,
        }
    )
    if default_review:
        metadata["review_contract_kind"] = "default"
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE task_runs SET metadata = ? "
            "WHERE id = ? AND task_id = ? AND ended_at IS NULL",
            (json.dumps(metadata, sort_keys=True), run.id, task_id),
        )
        if cur.rowcount != 1:
            raise ReviewTargetPreparationError(
                "active review run changed before target pinning"
            )
    return {
        "review_branch": review_branch,
        "review_base_sha": base_sha,
        "review_head_sha": head_sha,
    }


def _pin_review_target_or_block(
    conn: sqlite3.Connection,
    task: Task,
    workspace: Path | str,
    *,
    board: Optional[str] = None,
    default_review_status: bool = False,
) -> bool:
    product_review = (
        task.workflow_template_id == "product"
        and task.current_step_key == "review"
        and task.assignee == "reviewer"
    )
    default_review_candidate = (
        default_review_status
        and task.workflow_template_id is None
        and task.current_step_key in (None, "review")
        and task.assignee == "reviewer"
    )
    if not product_review and not default_review_candidate:
        return True
    try:
        _prepare_review_target(
            conn,
            task.id,
            workspace,
            board=board,
            default_review_status=default_review_status,
        )
        return True
    except Exception as exc:
        _record_task_failure(
            conn,
            task.id,
            f"review target preparation: {exc}",
            outcome="spawn_failed",
            failure_limit=1,
            force_trip=True,
            release_claim=True,
            end_run=True,
        )
        return False


def _prepare_test_target(
    conn: sqlite3.Connection,
    task_id: str,
    workspace: Path | str,
    *,
    board: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """Pin the exact clean branch/head that a product Test run verifies."""
    task = get_task(conn, task_id)
    if task is None:
        raise TestTargetPreparationError(f"task {task_id} not found")
    if task.workflow_template_id != "product" or task.current_step_key != "test":
        return None
    if not task.workspace_path:
        raise TestTargetPreparationError("task has no workspace path")
    try:
        expected_workspace = Path(task.workspace_path).expanduser().resolve(strict=True)
        actual_workspace = Path(workspace).expanduser().resolve(strict=True)
    except OSError as exc:
        raise TestTargetPreparationError(f"workspace is unavailable: {exc}") from exc
    if actual_workspace != expected_workspace:
        raise TestTargetPreparationError(
            f"launch workspace {actual_workspace} does not match task workspace "
            f"{expected_workspace}"
        )
    if not actual_workspace.is_dir():
        raise TestTargetPreparationError(
            f"task workspace is not a directory: {actual_workspace}"
        )
    if task.current_run_id is None:
        raise TestTargetPreparationError("task has no active test run")
    run = get_run(conn, task.current_run_id)
    if (
        run is None
        or run.task_id != task_id
        or run.ended_at is not None
        or run.status != "running"
        or run.profile != "tester"
        or run.step_key != "test"
    ):
        raise TestTargetPreparationError("active run is not the current test run")

    dirty = _review_git_output(
        actual_workspace, "status", "--porcelain", "--untracked-files=all"
    )
    if dirty:
        raise TestTargetPreparationError("test workspace is dirty or uncommitted")
    head_sha = _review_git_output(
        actual_workspace, "rev-parse", "--verify", "HEAD^{commit}"
    )
    test_branch = (_git_current_branch(actual_workspace) or "").strip()
    if not test_branch:
        raise TestTargetPreparationError("test workspace has no active branch")
    if task.branch_name and task.branch_name != test_branch:
        raise TestTargetPreparationError("test workspace branch does not match task branch")
    if not _FULL_GIT_SHA_RE.fullmatch(head_sha):
        raise TestTargetPreparationError("test head is not a full commit SHA")
    _evidence_generated_paths(board, actual_workspace, TestTargetPreparationError)

    metadata = dict(run.metadata or {})
    metadata.update({"test_branch": test_branch, "test_head_sha": head_sha})
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE task_runs SET metadata = ? "
            "WHERE id = ? AND task_id = ? AND ended_at IS NULL",
            (json.dumps(metadata, sort_keys=True), run.id, task_id),
        )
        if cur.rowcount != 1:
            raise TestTargetPreparationError(
                "active test run changed before target pinning"
            )
    return {"test_branch": test_branch, "test_head_sha": head_sha}


def _pin_test_target_or_block(
    conn: sqlite3.Connection,
    task: Task,
    workspace: Path | str,
    *,
    board: Optional[str] = None,
) -> bool:
    if (
        task.workflow_template_id != "product"
        or task.current_step_key != "test"
        or has_unresolved_product_preflight(conn, task.id)
    ):
        return True
    try:
        _prepare_test_target(conn, task.id, workspace, board=board)
        return True
    except Exception as exc:
        _record_task_failure(
            conn,
            task.id,
            f"test target preparation: {exc}",
            outcome="spawn_failed",
            failure_limit=1,
            force_trip=True,
            release_claim=True,
            end_run=True,
        )
        return False


def _resolver_resume_retry_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    failed: sqlite3.Row,
) -> Optional[tuple[_D4ResolvedPreflightProvenance, sqlite3.Row, dict[str, Any]]]:
    """Return one bounded, not-started retry after a Resolver resume."""
    if failed["kind"] != _RESOLVER_RESUME_RETRY_EVENT_KIND:
        return None
    failed_payload = _d4_event_payload(failed)
    if not isinstance(failed_payload, dict):
        return None
    failed_event_id = _d4_positive_event_id(failed["id"])
    if failed_event_id is None:
        return None
    failed_run_id = _d4_positive_event_id(failed["run_id"])
    if failed_run_id is None:
        return None
    resolved = conn.execute(
        "SELECT id, kind, payload, run_id FROM task_events "
        "WHERE task_id = ? AND kind = 'human_input_preflight_resolved' "
        "AND id < ? ORDER BY id DESC LIMIT 1",
        (task_id, failed_event_id),
    ).fetchone()
    provenance = _d4_resolved_preflight_provenance(conn, task_id, resolved)
    if provenance is None:
        return None
    resolver_run_id = _d4_positive_event_id(provenance.resolver_run["id"])
    if resolver_run_id is None:
        return None
    later_runs = conn.execute(
        "SELECT id, task_id, profile, step_key, status, outcome, ended_at, "
        "claim_lock, claim_expires, worker_pid "
        "FROM task_runs WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT 2",
        (task_id, resolver_run_id),
    ).fetchall()
    if len(later_runs) != 1:
        return None
    failed_run = later_runs[0]
    if failed_run["id"] != failed_run_id:
        return None
    if (
        failed_run["status"] != _RESOLVER_RESUME_RETRY_RUN_STATUS
        or failed_run["outcome"] != _RESOLVER_RESUME_RETRY_RUN_OUTCOME
        or failed_run["ended_at"] is None
        or failed_run["claim_lock"] is not None
        or failed_run["claim_expires"] is not None
        or failed_run["worker_pid"] is not None
    ):
        return None

    # The failed attempt must be the dispatch authorized by this resume.
    # Run count alone misses assignments/refreshes that invalidate authority.
    benign = tuple(sorted(_RESOLVER_ESCALATION_BENIGN_EVENT_KINDS))
    placeholders = ", ".join("?" for _ in benign)
    dispatch_events = conn.execute(
        "SELECT kind, run_id FROM task_events "
        "WHERE task_id = ? AND id > ? AND id < ? "
        f"AND kind NOT IN ({placeholders}) ORDER BY id ASC LIMIT 3",
        (task_id, provenance.resolved["id"], failed_event_id, *benign),
    ).fetchall()
    if (
        tuple(event["kind"] for event in dispatch_events)
        not in (("claimed",), ("claimed", "executor_stamped"))
        or any(event["run_id"] != failed_run_id for event in dispatch_events)
    ):
        return None
    return provenance, failed_run, failed_payload


def _can_bypass_story_refresh_after_resolver_resume(
    conn: sqlite3.Connection,
    task: Task,
    *,
    board: Optional[str] = None,
) -> bool:
    """Return whether the current Resolver resume authorizes one dispatch."""
    meta = product_board_metadata(board)
    if (
        meta is None
        or task.workflow_template_id != PRODUCT_WORKFLOW_TEMPLATE_ID
        or task.current_step_key not in {"architecture", "development"}
        or task.running
        or task.blocked
        or task.current_run_id is not None
        or task.claim_lock is not None
    ):
        return False
    latest = _d4_latest_non_benign_event(conn, task.id)
    if latest is None:
        return False

    failed_run: Optional[sqlite3.Row] = None
    failed_payload: Optional[dict[str, Any]] = None
    if latest["kind"] == "human_input_preflight_resolved":
        provenance = _d4_resolved_preflight_provenance(conn, task.id, latest)
    elif latest["kind"] == _RESOLVER_RESUME_RETRY_EVENT_KIND:
        retry = _resolver_resume_retry_provenance(conn, task.id, latest)
        if retry is None:
            return False
        provenance, failed_run, failed_payload = retry
    else:
        return False
    if provenance is None:
        return False

    step_key = task.current_step_key
    resume_status = _column_status_for_step(meta, step_key)
    if (
        resume_status not in VALID_STATUSES
        or resume_status in {"running", "blocked", "done", "archived"}
    ):
        return False
    resolved_payload = provenance.resolved_payload
    source_payload = provenance.preflight_payload
    resolver_run = provenance.resolver_run
    if (
        task.status != resume_status
        or resolved_payload.get("action") != "resume"
        or resolved_payload.get("resolver_profile") != RESOLVER_PROFILE
        or resolved_payload.get("step_key") != step_key
        or resolved_payload.get("status") != resume_status
        or source_payload.get("hermes_assignee") != RESOLVER_PROFILE
        or source_payload.get("step_key") != step_key
        or source_payload.get("resume_status") != resume_status
        or resolver_run["profile"] != RESOLVER_PROFILE
        or resolver_run["step_key"] != step_key
        or resolver_run["status"] != "completed"
        or resolver_run["outcome"] != "preflight_resolved"
        or resolver_run["ended_at"] is None
        or resolver_run["claim_lock"] is not None
        or resolver_run["claim_expires"] is not None
        or resolver_run["worker_pid"] is not None
    ):
        return False
    if failed_run is not None:
        if (
            failed_payload is None
            or failed_payload.get("retry_status") != resume_status
            or failed_run["step_key"] != step_key
        ):
            return False

    try:
        original_assignee = _d4_canonical_assignee(
            source_payload.get("original_assignee"), field="original_assignee"
        )
        resolved_assignee = _d4_canonical_assignee(
            resolved_payload.get("assignee"), field="resolved_assignee"
        )
        current_assignee = _d4_canonical_assignee(
            task.assignee, field="current_assignee"
        )
        failed_assignee = (
            _d4_canonical_assignee(failed_run["profile"], field="failed_assignee")
            if failed_run is not None else None
        )
    except TaskSnapshotConflict:
        return False
    return (
        original_assignee == resolved_assignee == current_assignee
        and (failed_run is None or failed_assignee == original_assignee)
    )


def _story_refresh_preflight(
    conn: sqlite3.Connection,
    task: Task,
    *,
    board: Optional[str],
) -> tuple[Optional[RefreshRequest], Optional[RefreshResult]]:
    """Build and execute the dispatcher-owned story refresh preflight."""

    meta = product_board_metadata(board)
    if (
        meta is None
        or not _handoff_v2_enabled(meta)
        or task.workflow_template_id != "product"
        or task.current_step_key not in {"architecture", "development"}
        or epic_id_for_task(conn, task.id) is None
    ):
        return None, None

    # A recovery assignee must inspect the exact state that raised the pending
    # preflight, even when an ordinary story refresh would otherwise be safe.
    # Refresh is deferred until the preflight resolves; reassignment to any
    # profile other than the one recorded by the preflight does not bypass it.
    preflight = _latest_unresolved_product_preflight(conn, task.id)
    if preflight is not None:
        _event_id, payload = preflight
        recovery_assignee = _canonical_assignee(payload.get("hermes_assignee"))
        if (
            recovery_assignee
            and _canonical_assignee(task.assignee) == recovery_assignee
        ):
            return None, None

    if _can_bypass_story_refresh_after_resolver_resume(
        conn, task, board=board
    ):
        return None, None

    if task.workspace_kind != "worktree":
        return None, RefreshResult(
            "error", error="product story refresh requires a worktree workspace"
        )

    try:
        workspace, resolved_branch = _resolve_worktree_workspace(
            task,
            board=board,
            base_branch=_story_base_branch(conn, task.id, board=board),
            conn=conn,
        )
        branch = (
            str(resolved_branch or task.branch_name or "").strip()
            or _git_current_branch(workspace)
        )
        repo_root = _git_toplevel(workspace)
        if repo_root is None or not branch:
            return None, RefreshResult("error", error="story_repository_unresolved")
        story_sha = _git_ref_sha(repo_root, branch)
        epic_id = epic_id_for_task(conn, task.id)
        epic_branch = epic_branch_for(epic_id) if epic_id else ""
        epic_tip_sha = _git_ref_sha(repo_root, epic_branch) if epic_branch else None
        if story_sha is None or epic_tip_sha is None:
            return None, RefreshResult("error", error="story_refresh_source_missing")

        # Materialization is part of preflight, so the first Architecture
        # dispatch and every later Development dispatch inspect the same
        # durable story worktree rather than the board's repository root.
        if task.workspace_path != str(workspace):
            set_workspace_path(conn, task.id, str(workspace))
        if task.branch_name != branch:
            set_branch_name(conn, task.id, branch)
        request = RefreshRequest(
            repo_root=repo_root,
            story_id=task.id,
            story_branch=branch,
            story_worktree=workspace,
            story_sha=story_sha,
            epic_branch=epic_branch,
            epic_tip_sha=epic_tip_sha,
        )
        return request, refresh_story_branch(request)
    except Exception as exc:
        _log.warning(
            "kanban story refresh preflight failed for %s: %s",
            task.id,
            exc,
        )
        return None, RefreshResult("error", error="story_refresh_preflight_failed")


def _route_story_refresh_rework(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str],
    request: RefreshRequest,
    refresh: RefreshResult,
) -> bool:
    """Route an isolated refresh conflict to the Development lane."""

    meta = product_board_metadata(board) or {}
    workflow = meta.get("product_workflow") if isinstance(meta, dict) else {}
    try:
        max_cycles = max(1, int((workflow or {}).get("max_rework_cycles", 3)))
    except (TypeError, ValueError):
        max_cycles = 3
    findings = [
        "isolated story refresh found merge conflicts in: "
        + ", ".join(refresh.conflict_paths),
        "retained conflict worktree: "
        + str(refresh.conflict_worktree or "(unavailable)"),
        f"story source SHA: {request.story_sha}",
        f"Epic tip SHA: {request.epic_tip_sha}",
    ]
    with authorized_governance_write(), write_txn(conn):
        row = conn.execute(
            "SELECT current_step_key, status, claim_lock, rework_count "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "ready"
            or row["claim_lock"] is not None
        ):
            return False
        origin_phase = str(row["current_step_key"] or "development")
        observed_count = int(row["rework_count"] or 0)
        next_count = observed_count + 1
        limit_reached = next_count > max_cycles
        target_phase = "development"
        next_status = (
            "blocked"
            if limit_reached
            else _column_status_for_step(meta, target_phase)
        )
        next_assignee = (
            "default"
            if limit_reached
            else _product_role_assignee(meta, "developer")
        )
        updated = conn.execute(
            "UPDATE tasks SET rework_count = ?, current_step_key = ?, "
            "status = ?, assignee = ?, running = 0, blocked = ?, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "workflow_template_id = 'product' "
            "WHERE id = ? AND status = 'ready' AND claim_lock IS NULL "
            "AND current_step_key IS ? AND rework_count = ?",
            (
                next_count,
                target_phase,
                next_status,
                next_assignee,
                1 if limit_reached else 0,
                task_id,
                row["current_step_key"],
                observed_count,
            ),
        )
        if updated.rowcount != 1:
            return False
        directive = create_rework_directive(
            conn,
            task_id,
            origin_kind="refresh",
            origin_phase=origin_phase,
            target_phase=target_phase,
            rejected_branch=request.story_branch,
            rejected_sha=request.story_sha,
            epic_tip_sha=request.epic_tip_sha,
            findings=findings,
        )
        _append_event(
            conn,
            task_id,
            "story_refresh_rework_routed",
            {
                "from_step": origin_phase,
                "target_step": target_phase,
                "directive_id": directive.id,
                "findings": findings,
                "rework_count": next_count,
                "max_rework_cycles": max_cycles,
                "conflict_worktree": str(refresh.conflict_worktree or ""),
            },
        )
        if limit_reached:
            _append_event(
                conn,
                task_id,
                "rework_limit_reached",
                {
                    "reason": "maximum product rework cycles exceeded",
                    "kind": "rework_limit",
                    "rework_count": next_count,
                },
            )
    return True


def _consume_story_refresh_preflight(
    conn: sqlite3.Connection,
    task: Task,
    *,
    board: Optional[str],
    request: Optional[RefreshRequest],
    refresh: Optional[RefreshResult],
) -> bool:
    """Record preflight evidence and say whether the task may be claimed."""

    if refresh is None:
        return True
    payload: dict[str, Any] = {"kind": refresh.kind}
    if request is not None:
        payload.update(
            {
                "story_branch": request.story_branch,
                "story_sha": request.story_sha,
                "epic_branch": request.epic_branch,
                "epic_tip_sha": request.epic_tip_sha,
            }
        )
    if refresh.after_sha:
        payload["after_sha"] = refresh.after_sha
    if refresh.current_sha:
        payload["current_sha"] = refresh.current_sha
    if refresh.current_epic_tip_sha:
        payload["current_epic_tip_sha"] = refresh.current_epic_tip_sha
    if refresh.dirty_paths:
        payload["dirty_paths"] = list(refresh.dirty_paths)
    if refresh.conflict_paths:
        payload["conflict_paths"] = list(refresh.conflict_paths)
    if refresh.conflict_worktree is not None:
        payload["conflict_worktree"] = str(refresh.conflict_worktree)
    if refresh.error:
        payload["error"] = refresh.error

    if refresh.kind == "unchanged":
        with write_txn(conn):
            _append_event(conn, task.id, "story_refresh_checked", payload)
        return True
    if refresh.kind == "refreshed":
        payload["authority_invalidated"] = True
        with write_txn(conn):
            _append_event(conn, task.id, "story_refreshed", payload)
        return True
    if refresh.kind == "conflict" and request is not None:
        with write_txn(conn):
            _append_event(conn, task.id, "story_refresh_conflict", payload)
        _route_story_refresh_rework(
            conn, task.id, board=board, request=request, refresh=refresh
        )
        return False

    with write_txn(conn):
        _append_event(conn, task.id, "story_refresh_attention_required", payload)
    return False


def _spawn_one_v2(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    failure_limit: Optional[int] = None,
) -> Optional[int]:
    """Claim -> resolve-workspace -> spawn recipe shared by the v2 spawn
    consumers (:func:`spawn_after_handoff`, T3.1, and :func:`reconcile`,
    T3.2).

    This deliberately duplicates the claim -> resolve-workspace -> spawn
    recipe used by the ready-queue loop in :func:`_dispatch_once_locked`
    (kanban_db.py ~8780-8825) rather than calling it, so that the live,
    heavily-guarded dispatch loop remains byte-for-byte unchanged (spawn-
    storm history makes that loop deliberately not-to-be-touched). See the
    ready loop for the canonical version of this recipe; keep the two in
    sync by hand if the primitives' contracts change.

    Returns the spawned pid (``0`` if the spawn succeeded but ``spawn_fn``
    returned a falsy pid) if the card was claimed and no exception was
    raised, or ``None`` if the claim CAS lost the race (already claimed / no
    longer ready) or the spawn attempt failed (a spawn failure is recorded
    via :func:`_record_spawn_failure` in that case).
    """
    refresh_task = get_task(conn, task_id)
    if refresh_task is not None:
        refresh_request, refresh_result = _story_refresh_preflight(
            conn, refresh_task, board=board
        )
        if not _consume_story_refresh_preflight(
            conn,
            refresh_task,
            board=board,
            request=refresh_request,
            refresh=refresh_result,
        ):
            return None
    claimed = claim_task(conn, task_id, ttl_seconds=ttl_seconds)
    if claimed is None:
        # Already claimed (or no longer ready) -- the CAS fire-once
        # guarantee: nothing to do on this or any later pass.
        return None
    try:
        _stamp_run_executor_identity(conn, claimed)
    except WorkerRuntimeIdentityError as exc:
        _record_spawn_failure(
            conn,
            claimed.id,
            str(exc),
            failure_limit=failure_limit,
        )
        return None
    try:
        resolved_branch_name = None
        if claimed.workspace_kind == "worktree":
            base_branch = _story_base_branch(conn, task_id, board=board)
            workspace, resolved_branch_name = _resolve_worktree_workspace(
                claimed, board=board, base_branch=base_branch, conn=conn
            )
        else:
            workspace = resolve_workspace(claimed, board=board, conn=conn)
    except Exception as exc:
        _record_spawn_failure(
            conn, claimed.id, f"workspace: {exc}",
            failure_limit=failure_limit,
        )
        return None
    # Persist the resolved workspace path so the worker can cd there.
    set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        set_branch_name(
            conn, claimed.id,
            resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}",
        )
    if not _pin_test_target_or_block(
        conn, claimed, workspace, board=board
    ):
        return None
    if not _pin_review_target_or_block(
        conn, claimed, workspace, board=board
    ):
        return None
    _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
    _spawn = spawn_fn if spawn_fn is not None else _default_spawn
    try:
        # Back-compat: older spawn_fn signatures accept only
        # (task, workspace). Introspect and pass `board` only when
        # supported -- mirrors the ready loop's same accommodation.
        import inspect
        try:
            sig = inspect.signature(_spawn)
            if "board" in sig.parameters:
                pid = _spawn(claimed, str(workspace), board=board)
            else:
                pid = _spawn(claimed, str(workspace))
        except (TypeError, ValueError):
            pid = _spawn(claimed, str(workspace))
        if pid:
            _set_worker_pid(conn, claimed.id, int(pid))
        # NOTE: intentionally do NOT reset consecutive_failures here --
        # matches the ready loop's rule (a successful spawn doesn't
        # prove the run will succeed; see _dispatch_once_locked).
        return int(pid) if pid else 0
    except Exception as exc:
        _record_spawn_failure(
            conn, claimed.id, str(exc),
            failure_limit=failure_limit,
        )
        return None



def spawn_after_handoff(
    conn: sqlite3.Connection,
    *,
    board: Optional[str] = None,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    failure_limit: Optional[int] = None,
) -> list[str]:
    """Event-driven fire-once spawn consumer for handoff_v2 boards (T3.1).

    Reacts to ``handoff`` task_events rather than polling: a candidate is a
    card whose *most recent* event is ``handoff`` and that is currently
    spawnable (``status='ready'``, unclaimed, and has a next-role assignee --
    the terminal review -> release_measure handoff clears ``assignee`` so it
    is correctly never a candidate). Non-v2 boards return ``[]`` -- those
    keep using the existing time-polling dispatcher, untouched.

    Fire-once is guaranteed by :func:`claim_task`'s ``ready -> running`` CAS:
    a second call over a card already claimed (now ``running``) finds
    ``claim_task`` returns ``None`` and skips it, so calling this function
    repeatedly over the same handoff is safe and idempotent.

    The per-card claim -> resolve-workspace -> spawn recipe lives in
    :func:`_spawn_one_v2`, shared with the slow-poller safety net
    (:func:`reconcile`, T3.2).
    """
    meta = product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return []

    rows = conn.execute(
        """
        SELECT t.id AS id
          FROM tasks t
          JOIN task_events e
            ON e.id = (
                SELECT id FROM task_events
                 WHERE task_id = t.id
                 ORDER BY created_at DESC, id DESC
                 LIMIT 1
            )
         WHERE e.kind = 'handoff'
           AND t.status = 'ready'
           AND t.claim_lock IS NULL
           AND t.assignee IS NOT NULL
           AND t.assignee != ''
         ORDER BY t.priority DESC, t.created_at ASC
        """
    ).fetchall()

    spawned_ids: list[str] = []
    for row in rows:
        task_id = row["id"]
        pid_or_none = _spawn_one_v2(
            conn, task_id, board=board, spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds, failure_limit=failure_limit,
        )
        if pid_or_none is not None:
            spawned_ids.append(task_id)

    return spawned_ids


@dataclass
class ReconcileResult:
    """Outcome of a single :func:`reconcile` pass over a handoff_v2 board.

    Bounded to ONE action per card per pass -- see ``reconcile``'s docstring.
    """

    reclaimed: list[str] = field(default_factory=list)
    """Task ids whose dead-worker ``running`` card was re-idled back to
    ``ready`` this pass. NOT spawned this pass -- see the one-action-per-card
    rule that prevents thrashing."""
    spawned: list[str] = field(default_factory=list)
    """Task ids spawned this pass via :func:`_spawn_one_v2` (claim-CAS makes
    this fire-once)."""
    integrated: list[str] = field(default_factory=list)
    """Story task ids durably finalized into their epic integration branch
    by the claimed intent coordinator this pass."""
    merged_to_main: list[str] = field(default_factory=list)
    """Story ids (standalone) or epic ids whose branch was merged into LOCAL
    main this pass via :func:`_merge_standalone_story_to_main` /
    :func:`merge_epic_to_main`. Populated ONLY when the board opts into
    ``product_workflow.merge_after_green`` (default OFF); at most one per pass."""
    requalification_requested: list[str] = field(default_factory=list)
    """Qualified scheduled card ids sent back through the existing Hermes
    qualification intake this pass; at most one per pass."""


def reconcile(
    conn: sqlite3.Connection,
    *,
    board: Optional[str] = None,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    failure_limit: Optional[int] = None,
    spawn_ready: bool = True,
) -> ReconcileResult:
    """Bounded safety-net poller for handoff_v2 boards (T3.2).

    The fast path (:func:`spawn_after_handoff`, T3.1) is event-driven and
    reacts immediately to a ``handoff``. ``reconcile`` is the SLOW safety
    net -- meant to run on a poll cadence -- that recovers v2 cards that
    fell through the fast path: a ``running`` card whose worker died, or a
    ``ready``+idle card that was never spawned.

    Takes **at most ONE action per card per pass** -- this is the direct
    regression guard against the multi-spawn storm. Two independent steps,
    applied in order, act on disjoint sets of cards:

    1. **Dead-worker recovery.** Any ``running`` card whose ``worker_pid``
       is no longer alive (:func:`_pid_alive`) is re-idled to ``ready`` via
       a CAS UPDATE (``status='running' AND claim_lock IS ?``), and a
       ``reconcile_reclaimed`` event is recorded. It is **not** spawned this
       pass -- recovery and spawn are kept in separate passes so a single
       dead worker can never trigger more than one DB-mutating action per
       tick. A live-PID running card gets no action.
    2. **Stranded-ready spawn.** Any ``ready``+unclaimed+assigned card that
       was **not** re-idled in step 1 this pass is spawned once via
       :func:`_spawn_one_v2` (the claim CAS makes this fire-once). Skipped
       entirely when ``spawn_ready=False`` (Codex re-review P1): unlike
       ``dispatch_once``, this step has no ``max_spawn`` / concurrency-cap
       awareness -- it is only safe to run standalone (dogfood / the tests
       below), never after ``dispatch_once`` in the same tick, since
       ``dispatch_once`` already spawns ready v2 cards under its caps and a
       second, uncapped spawn pass immediately after it would spawn past
       the live concurrency cap. The live gateway tick therefore calls
       ``reconcile(..., spawn_ready=False)`` -- see
       :func:`gateway.kanban_watchers._dispatch_once_then_reconcile` --
       leaving ``dispatch_once`` as the tick's sole capped spawn owner and
       reconcile's job (there) to just recover + integrate. A re-idled or
       stranded ready card is still picked up (capped) by ``dispatch_once``
       on its next tick.

    A dead-PID running card therefore converges in two passes: pass 1
    reclaims it (zero spawns that pass), pass 2 spawns it (now
    ready+idle) -- never more than one action, never a spawn storm. Running
    ``reconcile`` repeatedly over a healthy (live-PID) running card does
    nothing on every pass.

    3. **Scheduled-card requalification.** On strict product boards, at most
       ONE qualified ``scheduled`` card with no active worker is submitted as
       inert requalification intake. A pending intake makes the step
       idempotent. All other waits and terminal states are left untouched.

    4. **Story->epic integration.** Recover every prepared crash boundary,
       then claim, prepare, advance, and atomically finalize at most one new
       durable integration intent. The immutable integration fact, terminal
       story state, event, and release-snapshot invalidation commit together.

    Non-v2 boards return an empty ``ReconcileResult`` (a no-op); legacy
    boards keep using ``dispatch_once`` unchanged.
    """
    result = ReconcileResult()
    meta = product_board_metadata(board)
    if meta is None or not _handoff_v2_enabled(meta):
        return result

    # Step 1: reclaim running cards whose worker died. One CAS UPDATE per
    # card, in its own transaction -- never spawned this pass.
    # ``worker_pid IS NOT NULL`` matches detect_crashed_workers' convention
    # (kanban_db.py ~7904, query ~7944): a card claimed but not yet
    # pid-stamped (the brief window between claim_task and _set_worker_pid
    # inside _spawn_one_v2) has no pid to check yet and must not be mistaken
    # for a dead worker.
    running_rows = conn.execute(
        "SELECT id, worker_pid, claim_lock, started_at FROM tasks "
        "WHERE status = 'running' AND worker_pid IS NOT NULL"
    ).fetchall()
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    for row in running_rows:
        # Only check liveness for claims owned by this host -- a remote
        # host's PID is meaningless to a local _pid_alive check (mirrors
        # detect_crashed_workers).
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Skip liveness check inside the launch-window grace period so a
        # freshly-spawned worker isn't reclaimed before its PID is visible
        # on /proc (mirrors detect_crashed_workers).
        started_at = row["started_at"] if "started_at" in row.keys() else None
        if started_at is not None:
            grace = _resolve_crash_grace_seconds()
            if time.time() - started_at < grace:
                continue
        if _pid_alive(row["worker_pid"]):
            continue
        task_id = row["id"]
        with write_txn(conn):
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status = 'ready', claim_lock = NULL,
                       claim_expires = NULL, worker_pid = NULL,
                       running = 0, blocked = 0
                 WHERE id = ? AND status = 'running' AND claim_lock IS ?
                """,
                (task_id, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            _append_event(
                conn, task_id, "reconcile_reclaimed",
                {"reason": "dead_worker_pid", "worker_pid": row["worker_pid"]},
            )
        result.reclaimed.append(task_id)

    # Step 2: spawn stranded ready+idle cards, excluding this pass's
    # reclaims -- the one-action-per-card rule that prevents thrash. Skipped
    # entirely when spawn_ready=False (see the docstring's Codex re-review
    # P1 note) -- this step has no max_spawn awareness, so it must never run
    # after dispatch_once in the same tick.
    reclaimed_ids = set(result.reclaimed)
    ready_rows = conn.execute(
        """
        SELECT id FROM tasks
         WHERE status = 'ready'
           AND claim_lock IS NULL
           AND assignee IS NOT NULL
           AND assignee != ''
         ORDER BY priority DESC, created_at ASC
        """
    ).fetchall() if spawn_ready else []
    for row in ready_rows:
        task_id = row["id"]
        if task_id in reclaimed_ids:
            continue
        pid_or_none = _spawn_one_v2(
            conn, task_id, board=board, spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds, failure_limit=failure_limit,
        )
        if pid_or_none is not None:
            result.spawned.append(task_id)

    # Step 3: send at most one qualified scheduled card back through the
    # existing qualification intake. Development sequencing is dependency-
    # driven; ``scheduled`` has no normal dispatcher wake on a strict v2 board.
    qualification = meta.get("qualification")
    if isinstance(qualification, dict) and qualification.get("required") is True:
        from hermes_cli import kanban_intake

        candidates = conn.execute(
            """
            SELECT t.id
              FROM tasks t
             WHERE t.status = 'scheduled'
               AND t.work_item_kind = 'card'
               AND t.work_contract_id IS NOT NULL
               AND t.current_run_id IS NULL
               AND t.claim_lock IS NULL
               AND COALESCE(t.current_step_key, '') != 'release_measure'
             ORDER BY t.priority DESC, t.created_at ASC, t.id ASC
            """
        ).fetchall()
        for candidate in candidates:
            task_id = str(candidate["id"])
            if _has_sticky_block(conn, task_id):
                continue
            receipt = kanban_intake.submit_requalification(
                conn,
                task_id=task_id,
                reason="qualified scheduled work has no normal wake action",
            )
            if not receipt["created"]:
                continue
            result.requalification_requested.append(task_id)
            break

    # Step 4: converge prepared crash boundaries, then advance at most one new
    # pending intent through the claimed coordinator. Facts observed before
    # recovery let the result report only stories finalized by this pass.
    from hermes_cli.kanban_story_integration import (
        advance_prepared_intent,
        claim_next_intent,
        finish_intent,
        prepare_claimed_intent,
        recover_expired_intents,
        route_intent_failure,
    )

    facts_before = {
        (row["epic_id"], row["story_id"], row["source_sha"])
        for row in conn.execute(
            "SELECT epic_id, story_id, source_sha FROM epic_story_integrations"
        ).fetchall()
    }
    recovery = recover_expired_intents(conn, board=board)
    facts_after_recovery = {
        (row["epic_id"], row["story_id"], row["source_sha"])
        for row in conn.execute(
            "SELECT epic_id, story_id, source_sha FROM epic_story_integrations"
        ).fetchall()
    }
    result.integrated.extend(
        sorted({story_id for _epic_id, story_id, _source in facts_after_recovery - facts_before})
    )
    if recovery.finalized == 0:
        claimed_intent = claim_next_intent(
            conn,
            _claimer_id(),
            _resolve_claim_ttl_seconds(ttl_seconds),
            board=board,
        )
        if claimed_intent is not None:
            active_intent = claimed_intent
            try:
                prepared_intent = prepare_claimed_intent(
                    conn, claimed_intent, board=board
                )
                active_intent = prepared_intent
                cas_result = advance_prepared_intent(
                    conn, prepared_intent, board=board
                )
                if (
                    cas_result.kind in {"advanced", "reflected"}
                    and cas_result.current_sha == prepared_intent.candidate_sha
                ):
                    fact = finish_intent(
                        conn, prepared_intent, cas_result, board=board
                    )
                    result.integrated.append(fact.story_id)
                else:
                    route_intent_failure(
                        conn, prepared_intent, cas_result, board=board
                    )
            except Exception as exc:
                try:
                    route_intent_failure(conn, active_intent, exc, board=board)
                except Exception:
                    # Lost ownership or an interrupted routing transaction keeps
                    # the durable intent available to same-lineage recovery.
                    pass

    # Step 5 (merge-back): carry finished work to LOCAL main. OFF unless the
    # board opts into product_workflow.merge_after_green.
    done_rows = conn.execute(
        """
        SELECT t.id, em.epic_id
          FROM tasks t
          LEFT JOIN epic_memberships em ON em.task_id = t.id
          LEFT JOIN tasks e ON e.id = em.epic_id
         WHERE t.status = 'done'
           AND (em.epic_id IS NULL OR e.status NOT IN ('done', 'archived'))
         ORDER BY t.completed_at ASC
        """
    ).fetchall()
    merge_after_green = _product_merge_after_green(product_board_metadata(board))
    if merge_after_green:
        attempted_epics: set[str] = set()
        for row in done_rows:
            story_id = row["id"]
            epic_id = row["epic_id"]
            if epic_id is not None:
                if epic_id in attempted_epics:
                    continue
                attempted_epics.add(epic_id)
                integrated = conn.execute(
                    "SELECT 1 FROM epic_story_integrations "
                    "WHERE epic_id=? AND story_id=? LIMIT 1",
                    (epic_id, story_id),
                ).fetchone()
                if integrated is not None and epic_ready(conn, epic_id, board=board):
                    if merge_epic_to_main(conn, epic_id, board=board) == "merged":
                        result.merged_to_main.append(epic_id)
                        break
            elif _merge_standalone_story_to_main(conn, story_id, board=board) == "merged":
                result.merged_to_main.append(story_id)
                break

    return result


# ---------------------------------------------------------------------------
def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable)
    until ``unblock_task`` re-gates it."""
    with write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        _apply_v2_flags_for_status(conn, task_id, "scheduled")
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason),
        )
        _append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True


# --- Worker context builder (what a spawned worker sees) ---





def build_worker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Everything a worker should read about its task: header, body,
    attachments, prior attempts, done-parent handoffs, the assignee's recent
    work, comments. Lists are tail-capped and fields char-capped
    (``_CTX_MAX_*``) so the prompt stays bounded on pathological boards."""
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")
    # One clock reading so every relative age in this rendering agrees.
    now = int(time.time())
    lines: list[str] = []
    _ctx_header(lines, conn, task_id, task)
    _ctx_attachments(lines, list_attachments(conn, task_id))
    _ctx_prior_attempts(lines, conn, task_id, now)
    _ctx_parent_results(lines, conn, task_id, now)
    _ctx_role_history(lines, conn, task, now)
    _ctx_comments(lines, list_comments(conn, task_id), now)
    return "\n".join(lines).rstrip() + "\n"


def _ctx_cap(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
    """Truncate to ``limit`` chars with a visible ellipsis."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"


def _ctx_stamp(ts: int, now: int) -> str:
    """``YYYY-MM-DD HH:MM`` plus a relative age when one is available."""
    disp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    age = _relative_age(ts, now)
    return f"{disp}, {age}" if age else disp


def _ctx_metadata_line(metadata: Any) -> Optional[str]:
    if not metadata:
        return None
    try:
        return f"_metadata_: `{_ctx_cap(json.dumps(metadata, ensure_ascii=False, sort_keys=True))}`"
    except Exception:
        return None


def _ctx_tail(items: list, cap: int, noun: str) -> tuple[list, Optional[str]]:
    """Keep the newest ``cap`` items; describe the omitted head, if any."""
    omitted = max(0, len(items) - cap)
    if not omitted:
        return items, None
    return items[-cap:], (
        f"_({omitted} earlier {noun}{'s' if omitted != 1 else ''} "
        f"omitted; showing most recent {cap})_"
    )


def _ctx_header(
    lines: list[str], conn: sqlite3.Connection, task_id: str, task: Task
) -> None:
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.max_runtime_seconds is not None:
        terminal_timeout = _worker_terminal_timeout_env(
            task.max_runtime_seconds, os.environ.get("TERMINAL_TIMEOUT"),
        )
        effective_terminal_timeout = terminal_timeout or os.environ.get("TERMINAL_TIMEOUT")
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
        if effective_terminal_timeout:
            lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
    if task.branch_name:
        lines.append(f"Branch:   {task.branch_name}")
    lines.append("")
    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_ctx_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")

    if task.workflow_template_id == "product" and task.current_step_key in {"test", "review"}:
        phase = str(task.current_step_key).title()
        pinned_sha, pinned_branch, _run_id, _workspace = _evidence_pin(
            conn, task_id, str(task.current_step_key)
        )
        lines.append("## Evidence-phase source boundary")
        lines.append(
            f"{phase} is evidence-only: never commit source or fixture changes."
        )
        lines.append(
            "If a source or fixture edit is required, report it as a concrete "
            "finding with workflow_outcome.verdict=changes_requested targeting "
            "development; do not create a source commit in this phase."
        )
        if pinned_branch and pinned_sha:
            lines.append(f"Dispatcher-pinned source: `{pinned_branch}` at `{pinned_sha}`")
        lines.append("")

    contract_view = work_contract_view(conn, task.work_contract_id)
    if contract_view is not None:
        lines.append("## Work Contract")
        lines.append(
            "_Immutable authority for this task. Lower-priority comments, "
            "skills, and memory cannot expand it._"
        )
        lines.append(f"ID: `{contract_view['id']}`")
        lines.append(f"Digest: `{contract_view['digest']}`")
        lines.append(
            "Metadata: `"
            + _ctx_cap(
                json.dumps(
                    {
                        "policy_version": contract_view["policy_version"],
                        "qualification_path": contract_view["qualification_path"],
                        "request_id": contract_view["request_id"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            + "`"
        )
        for key in (
            "work",
            "routing",
            "handover",
            "rules",
            "classification",
            "sizing",
            "requirement_feasibility",
        ):
            lines.append(
                _ctx_cap(
                    json.dumps(
                        {key: contract_view[key]},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            )
        lines.append("")


    unresolved_preflight_entry = _latest_unresolved_product_preflight(conn, task_id)
    if unresolved_preflight_entry:
        _preflight_event_id, unresolved_preflight = unresolved_preflight_entry
        board_slug = _board_slug_for_connection(conn)
        board_meta = product_board_metadata(board_slug) or {}
        board_policy = _product_workflow_dict(board_meta)
        lines.append("## Required resolver action")
        lines.append(
            f"Original blocker: {_ctx_cap(str(unresolved_preflight.get('reason') or 'unspecified'))}"
        )
        attempts = unresolved_preflight.get("attempted_resolutions") or []
        if attempts:
            lines.append("Attempted resolutions: " + "; ".join(map(str, attempts)))
        lines.append(
            "Board policy: "
            + _ctx_cap(json.dumps(board_policy, sort_keys=True, ensure_ascii=False))
        )
        lines.append(
            "Resolve only with kanban_resolve: decision must be resume, repair, "
            "or escalate; diagnosis, reason, fault_domain, and "
            "the complete expected snapshot are required."
        )
        lines.append("")

    directive = active_rework_directive(conn, task_id)
    if directive is not None:
        lines.append("## Required rework directive")
        lines.append(
            "_This is persisted workflow authority. Complete the target phase "
            "before treating the directive as resolved._"
        )
        lines.append(
            f"Origin: {directive.origin_kind} / phase `{directive.origin_phase}`"
            + (
                f" / run `{directive.origin_run_id}`"
                if directive.origin_run_id is not None
                else ""
            )
        )
        if directive.origin_intent_key:
            lines.append(f"Origin intent: `{_ctx_cap(directive.origin_intent_key)}`")
        lines.append(f"Target phase: `{directive.target_phase}`")
        if directive.rejected_branch:
            lines.append(
                f"Rejected branch: `{_ctx_cap(directive.rejected_branch)}`"
            )
        if directive.rejected_sha:
            lines.append(f"Rejected SHA: `{directive.rejected_sha}`")
        if directive.epic_tip_sha:
            lines.append(f"Epic tip SHA: `{directive.epic_tip_sha}`")
        lines.append("Findings:")
        for finding in directive.findings:
            lines.append(f"- {_ctx_cap(finding)}")
        lines.append("")

    # Attachments — files uploaded to this task (PDFs, source docs,
    # images). Surface the absolute on-disk path so the worker, which has
    # full file-tool access, can read them directly (read_file, terminal
    # `pdftotext`, etc.). On the local terminal backend the path resolves
    # as-is; remote backends need the kanban attachments dir mounted.
    attachments = list_attachments(conn, task_id)
    if attachments:
        lines.append("## Attachments")
        lines.append(
            "Files attached to this task. Read them with the file/terminal "
            "tools at the absolute paths below:"
        )
        for att in attachments:
            size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
            size_str = f", {size_kb} KB" if size_kb else ""
            ctype = f", {att.content_type}" if att.content_type else ""
            lines.append(f"- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`")
        lines.append("")

def _ctx_attachments(lines: list[str], attachments: list[Attachment]) -> None:
    """Absolute on-disk paths so the worker's file tools read them directly
    (remote terminal backends need the attachments dir mounted)."""
    if not attachments:
        return
    lines.append("## Attachments")
    lines.append(
        "Files attached to this task. Read them with the file/terminal "
        "tools at the absolute paths below:"
    )
    for att in attachments:
        size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
        size_str = f", {size_kb} KB" if size_kb else ""
        ctype = f", {att.content_type}" if att.content_type else ""
        lines.append(f"- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`")
    lines.append("")


def _ctx_prior_attempts(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Closed runs on this task (the active run is this worker), newest
    ``_CTX_MAX_PRIOR_ATTEMPTS`` in full, older ones as a one-line marker."""
    all_prior = [r for r in list_runs(conn, task_id) if r.ended_at is not None]
    shown, omitted_note = _ctx_tail(all_prior, _CTX_MAX_PRIOR_ATTEMPTS, "attempt")
    if not shown:
        return
    first_shown_idx = len(all_prior) - len(shown) + 1
    lines.append("## Prior attempts on this task")
    if omitted_note:
        lines.append(omitted_note)
    for offset, run in enumerate(shown):
        profile = run.profile or "(unknown)"
        outcome = run.outcome or run.status
        lines.append(
            f"### Attempt {first_shown_idx + offset} — {outcome} ({profile}, {_ctx_stamp(run.started_at, now)})"
        )
        if run.summary and run.summary.strip():
            lines.append(_ctx_cap(run.summary))
        if run.error and run.error.strip():
            lines.append(f"_error_: {_ctx_cap(run.error)}")
        meta_line = _ctx_metadata_line(run.metadata)
        if meta_line:
            lines.append(meta_line)
        lines.append("")


def _ctx_parent_results(lines: list[str], conn: sqlite3.Connection, task_id: str, now: int) -> None:
    """Done-parent handoffs: newest ``completed`` run's summary+metadata,
    falling back to ``task.result`` for pre-runs-table data. Stamped with a
    relative age so the worker re-verifies stale upstream results."""
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id", (task_id,),
    ).fetchall()
    wrote_header = False
    for pid in (r["parent_id"] for r in parent_rows):
        pt = get_task(conn, pid)
        if not pt or pt.status != "done":
            continue
        runs = [r for r in list_runs(conn, pid) if r.outcome == "completed"]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        run = runs[0] if runs else None
        if not wrote_header:
            lines.append("## Parent task results")
            lines.append(
                "_Handoffs from upstream tasks, captured when each parent "
                "completed (see age below). These are point-in-time "
                "snapshots, not live state — if a result drives your "
                "current work and it's not recent, re-verify against the "
                "source before acting on it as current._"
            )
            wrote_header = True
        done_ts = run.ended_at if run is not None and run.ended_at else (pt.completed_at or None)
        age = _relative_age(done_ts, now)
        lines.append(f"### {pid}" + (f" (completed {age})" if age else ""))
        if run is not None and run.summary and run.summary.strip():
            lines.append(_ctx_cap(run.summary))
        elif pt.result:
            lines.append(_ctx_cap(pt.result))
        else:
            lines.append("(no result recorded)")
        meta_line = _ctx_metadata_line(run.metadata) if run is not None else None
        if meta_line:
            lines.append(meta_line)
        lines.append("")


def _ctx_role_history(lines: list[str], conn: sqlite3.Connection, task: Task, now: int) -> None:
    """The assignee's 5 most recent completed runs on OTHER tasks — implicit
    role continuity without wiring anything into SOUL.md / MEMORY.md."""
    if not task.assignee:
        return
    role_rows = conn.execute(
        "SELECT t.id, t.title, r.summary, r.ended_at "
        "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
        "WHERE r.profile = ? AND r.task_id != ? "
        "  AND r.outcome = 'completed' "
        "ORDER BY r.ended_at DESC LIMIT 5", (task.assignee, task.id),
    ).fetchall()
    if not role_rows:
        return
    lines.append(f"## Recent work by @{task.assignee}")
    for row in role_rows:
        first = _first_line(row["summary"], 200) or "(no summary)"
        lines.append(
            f"- {row['id']} — {row['title']} ({_ctx_stamp(int(row['ended_at']), now)}): {first}"
        )
    lines.append("")


def _ctx_comments(lines: list[str], comments: list[Comment], now: int) -> None:
    """Newest ``_CTX_MAX_COMMENTS`` comments. The explicit "comment from
    worker" framing stops an operator-controlled HERMES_PROFILE like
    "hermes-system" being read as a system directive above an
    attacker-influenceable body (defense-in-depth)."""
    shown, omitted_note = _ctx_tail(comments, _CTX_MAX_COMMENTS, "comment")
    if not shown:
        return
    lines.append("## Comment thread")
    if omitted_note:
        lines.append(omitted_note)
    for c in shown:
        # Render author with explicit "comment from worker" framing so operator-controlled HERMES_PROFILE
        # values like "hermes-system" or "operator" can't be misread by the next worker as a system
        # directive above the (attacker-influenceable) comment body. Defense-in-depth — the LLM-controlled
        # author-forgery surface was already closed in #22435. See #22452.
        safe_author = (c.author or "").replace("`", "")
        lines.append(f"comment from worker `{safe_author}` at {_ctx_stamp(c.created_at, now)}:")
        lines.append(_ctx_cap(c.body, _CTX_MAX_COMMENT_BYTES))
        lines.append("")


# --- Stats + SLA helpers ---

def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts and the oldest ``ready`` age (staleness signal)."""
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee = _counts_by_assignee(conn)

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
    }


def _counts_by_assignee(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``{assignee: {status: n}}`` over non-archived tasks."""
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])
    return counts


def _to_epoch(val) -> Optional[int]:
    """Epoch seconds from int/float/numeric string/ISO-8601; None for empty/invalid."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c = _to_epoch(task.created_at)
    _s = _to_epoch(task.started_at)
    _co = _to_epoch(task.completed_at)
    return {
        "created_age_seconds": now - _c if _c is not None else None,
        "started_age_seconds": now - _s if _s is not None else None,
        "time_to_complete_seconds": _co - (_s or _c) if _co is not None else None,
    }


# --- Retention + garbage collection ---

def gc_events(conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600) -> int:
    """Prune old done/archived events, retaining decomposition identity until task deletion."""
    cutoff = int(time.time()) - int(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND kind != 'decomposed' AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived'))", (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(*, older_than_seconds: int = 30 * 24 * 3600, board: Optional[str] = None) -> int:
    """Delete worker log files older than the cutoff on one board; returns the count."""
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        with contextlib.suppress(OSError):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
    return removed


# --- Worker log accessor ---

def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Worker log path (may not exist). The dispatcher always passes ``board``
    explicitly to avoid resolution ambiguity."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None, board: Optional[str] = None,
) -> Optional[str]:
    """Worker log text (last ``tail_bytes`` when set); None when the file is missing."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip the partial first line unless the window has no newline
                # at all (readline() would eat everything).
                probe = f.tell()
                if not f.readline().endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


# --- Assignee enumeration (known profiles + per-profile board stats) ---

def list_profiles_on_disk() -> list[str]:
    """Profiles with a ``config.yaml`` plus the implicit ``default``; reads paths
    directly to avoid importing ``hermes_cli.profiles`` at startup."""
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")
    if profiles_dir.is_dir():
        try:
            names.update(e.name for e in profiles_dir.iterdir() if e.is_dir() and (e / "config.yaml").is_file())
        except OSError:
            pass
    return sorted(names)


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """``{"name", "on_disk", "counts"}`` for every on-disk profile or task
    assignee, so a fresh profile appears in pickers before it has a task."""
    on_disk = set(list_profiles_on_disk())
    counts = _counts_by_assignee(conn)
    return [
        {"name": name, "on_disk": name in on_disk, "counts": counts.get(name, {})}
        for name in sorted(on_disk | set(counts))
    ]


# --- Runs (attempt history on a task) ---

def list_runs(
    conn: sqlite3.Connection, task_id: str, *, include_active: bool = True,
    state_type: Optional[str] = None, state_name: Optional[str] = None,
) -> list[Run]:
    """Runs in start order; ``include_active=False`` = closed only; ``state_type``
    (``status``/``outcome``) + ``state_name`` filter together."""
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None and state_type not in ("status", "outcome"):
        raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Newest non-empty run summary, or None. Workers hand off via ``summary`` and
    leave ``tasks.result`` NULL, so views need this or a done task looks empty."""
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, str]:
    """``{task_id: newest non-empty run summary}`` in one query (window function,
    SQLite >= 3.25); tasks without a summary are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}


def latest_ai_provenance_by_task(
    conn: sqlite3.Connection,
    task_ids: Iterable[str],
    *,
    include_summaries: bool = False,
) -> dict[str, dict[str, Any]]:
    """Batch-fetch compact AI provenance summaries for dashboard cards.

    The returned shape is ``{task_id: {by_step, writer_agent, tester_agent,
    reviewer_agent, review_rule, ...}}``. It is derived from run metadata,
    not from model self-report prose, so a card becomes an audit ledger for
    which AI wrote/tested/reviewed each workflow step.
    """
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, step_key, metadata,
               {'summary' if include_summaries else 'NULL'} AS run_summary
          FROM task_runs
         WHERE task_id IN ({placeholders})
           AND metadata IS NOT NULL AND metadata != ''
         ORDER BY COALESCE(ended_at, started_at) ASC, id ASC
        """,
        ids,
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except Exception:
            metadata = {}
        if not isinstance(metadata, dict):
            continue
        tid = row["task_id"]
        aggregate = out.setdefault(tid, {})
        _merge_ai_provenance_summary(
            aggregate,
            row["step_key"],
            metadata,
            row["run_summary"],
        )
    return {tid: summary for tid, summary in out.items() if summary}


@dataclass(frozen=True)
class ClearTerminalStateRequest:
    task_id: str
    expected_completed_at: int
    expected_phase: str
    expected_latest_event_id: int
    actor: str
    reason: str

def _find_missing_parents(conn: sqlite3.Connection, parents: Iterable[str]) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        parents,
    ).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in parents if p not in present]

def _development_budget_exhaustion_count(
    conn: sqlite3.Connection, task_id: str
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM task_events "
        "WHERE task_id = ? AND kind = 'development_budget_exhausted'",
        (task_id,),
    ).fetchone()
    return int(row["count"] or 0) if row is not None else 0

def handle_development_budget_exhaustion(
    conn: sqlite3.Connection, task_id: str, *, board: Optional[str] = None
) -> bool:
    """Atomically park a Development card for PO review or human action."""
    from hermes_cli import kanban_intake

    wake_product_owner = False
    with authorized_governance_write(), write_txn(conn):
        task = conn.execute(
            "SELECT status, workflow_template_id, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if (
            task is None
            or task["status"] != "running"
            or task["workflow_template_id"] != "product"
            or task["current_step_key"] != "development"
        ):
            return False

        occurrence = _development_budget_exhaustion_count(conn, task_id) + 1
        run_id = _end_run(
            conn,
            task_id,
            outcome="budget_exhausted",
            status="budget_exhausted",
            summary="Development iteration budget exhausted",
        )
        if run_id is None:
            return False
        next_status = "scheduled" if occurrence == 1 else "ready"
        updated = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, running = 0, blocked = 0 "
            "WHERE id = ? AND status = 'running' AND current_run_id IS NULL",
            (next_status, task_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Development budget state changed during finalization")
        _append_event(
            conn,
            task_id,
            "development_budget_exhausted",
            {"occurrence": occurrence},
            run_id=run_id,
        )

        if occurrence == 1:
            kanban_intake.submit_requalification(
                conn,
                task_id=task_id,
                reason=(
                    "Development exhausted its configured iteration budget; "
                    "Product Owner must resize or decompose the work."
                ),
                qualification_route="product_owner",
                _wake=False,
            )
            wake_product_owner = True
        elif not block_task(
            conn,
            task_id,
            reason=(
                "Development exhausted its configured iteration budget twice; "
                "human decision required."
            ),
            kind="transient",
            board=board or _board_slug_for_connection(conn),
        ):
            raise RuntimeError("could not block repeated Development budget exhaustion")

    if wake_product_owner:
        kanban_intake._wake_intake_qualifier()
    return True

def _promote_ready_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    failure_limit: int,
    release_measure_unblocks: bool,
) -> bool:
    """Promote one eligible task; caller must hold the write transaction."""
    row = conn.execute(
        "SELECT id, status, consecutive_failures, max_retries, "
        "workflow_template_id, current_step_key "
        "FROM tasks WHERE id = ? AND status IN ('todo', 'blocked')",
        (task_id,),
    ).fetchone()
    if row is None or _is_engine_owned_integration_state(row):
        return False
    cur_status = row["status"]
    if cur_status == "blocked" and _has_sticky_block(conn, task_id):
        return False
    parents = conn.execute(
        "SELECT t.status, t.workflow_template_id, t.current_step_key FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ?",
        (task_id,),
    ).fetchall()
    if not all(
        _dependency_parent_satisfied(
            parent, release_measure_unblocks=release_measure_unblocks
        )
        for parent in parents
    ):
        return False
    if cur_status == "blocked":
        failures = int(row["consecutive_failures"] or 0)
        task_limit = row["max_retries"]
        effective_limit = (
            int(task_limit) if task_limit is not None else int(failure_limit)
        )
        if failures >= effective_limit:
            return False
    # Restore the phase the task was actually interrupted in: a card that
    # entered ``review`` and was demoted by a parent reopen must return to
    # ``review``, not to ``ready`` (which would silently convert a reviewer
    # handoff back into implementation work).
    resume_status = _resume_status_from_events(conn, task_id)
    cur = conn.execute(
        "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
        (resume_status, task_id, cur_status),
    )
    if cur.rowcount != 1:
        return False
    _append_event(
        conn, task_id, "promoted",
        {"status": resume_status} if resume_status != "ready" else None,
    )
    return True

def resolve_profile_runtime_identity(
    profile_name: str,
    *,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
    source: str = "dispatcher",
    surface: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Resolve provider, model, and effective effort from one named profile."""
    if not profile_name:
        return None
    try:
        from hermes_constants import (
            resolve_reasoning_config,
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_cli.config import load_config
        from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

        profile = normalize_profile_name(profile_name)
        profile_home = resolve_profile_env(profile)
        token = set_hermes_home_override(profile_home)
        try:
            config = load_config()
        finally:
            reset_hermes_home_override(token)
        model_config = config.get("model")
        if not isinstance(model_config, dict):
            model_config = {"default": model_config}
        provider = str(
            provider_override or model_config.get("provider") or ""
        ).strip().lower()
        model = str(
            model_override
            or model_config.get("default")
            or model_config.get("model")
            or ""
        ).strip()
        reasoning_config = resolve_reasoning_config(config, model)
        if provider in {"claude-cli", "codex-cli"}:
            from agent.cli_emulated_provider import resolve_cli_effort

            effort = resolve_cli_effort(provider, reasoning_config)
        elif (
            isinstance(reasoning_config, dict)
            and reasoning_config.get("enabled") is False
        ):
            effort = "none"
        elif isinstance(reasoning_config, dict):
            configured_effort = reasoning_config.get("effort")
            effort = (
                configured_effort.strip().lower()
                if isinstance(configured_effort, str)
                else None
            )
        else:
            effort = None
        if not provider or provider == "auto" or not model or not effort:
            return None
        return {
            "profile": profile,
            "provider": provider,
            "model": model,
            "effort": effort,
            "surface": surface
            or ("claude-cli" if provider == "claude-cli" else "hermes-primary"),
            "source": source,
            "version": 1,
        }
    except Exception as exc:
        _log.warning(
            "kanban worker: could not resolve canonical runtime identity for "
            "profile=%r (%s)",
            profile_name,
            exc,
        )
        return None

def resolve_profile_iteration_budget(profile_name: str) -> Optional[int]:
    """Return the configured ``agent.max_turns`` for one profile."""
    if not profile_name:
        return None
    try:
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_cli.config import DEFAULT_CONFIG, load_config
        from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

        profile = normalize_profile_name(profile_name)
        token = set_hermes_home_override(resolve_profile_env(profile))
        try:
            config = load_config()
        finally:
            reset_hermes_home_override(token)
        agent = config.get("agent") if isinstance(config, dict) else None
        configured = agent.get("max_turns") if isinstance(agent, dict) else None
        if configured is None:
            configured = DEFAULT_CONFIG["agent"]["max_turns"]
        budget = int(configured)
        return budget if budget > 0 else None
    except (FileNotFoundError, TypeError, ValueError, KeyError, OSError):
        return None

def _resolve_worker_runtime_identity(task: Task) -> Optional[dict[str, Any]]:
    """Resolve the fixed profile runtime selected for this dispatched run."""
    if not task.assignee:
        return None
    return resolve_profile_runtime_identity(
        task.assignee,
        provider_override=task.provider_override,
        model_override=task.model_override,
    )

def _stamp_run_executor_identity(
    conn: sqlite3.Connection,
    task: Task,
) -> Optional[dict[str, Any]]:
    """Persist trusted profile/provider/model/effort facts on the active run."""
    if task.current_run_id is None:
        return None
    governance = conn.execute(
        "SELECT qualification_required "
        "FROM board_governance WHERE id=1"
    ).fetchone()
    governed_product = (
        task.workflow_template_id == "product"
        and governance is not None
        and int(governance["qualification_required"]) == 1
    )
    identity = _resolve_worker_runtime_identity(task)
    if identity is None:
        if governed_product:
            raise WorkerRuntimeIdentityError(
                "Governed product dispatch requires an explicit provider, "
                "model, and effective effort for the selected worker profile."
            )
        return None
    run = get_run(conn, task.current_run_id)
    if run is None or run.ended_at is not None:
        if governed_product:
            raise WorkerRuntimeIdentityError(
                "Governed product worker identity could not be persisted on "
                "the active run."
            )
        return None
    metadata = dict(run.metadata or {})
    metadata["executor"] = identity
    with write_txn(conn):
        updated = conn.execute(
            "UPDATE task_runs SET metadata=? "
            "WHERE id=? AND task_id=? AND ended_at IS NULL",
            (
                json.dumps(metadata, ensure_ascii=False),
                task.current_run_id,
                task.id,
            ),
        )
        if updated.rowcount != 1:
            if governed_product:
                raise WorkerRuntimeIdentityError(
                    "Governed product worker identity could not be persisted "
                    "on the active run."
                )
            return None
        _append_event(
            conn,
            task.id,
            "executor_stamped",
            identity,
            run_id=task.current_run_id,
        )
    return identity

def _record_spawn_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    failure_limit: int = None,
) -> bool:
    return _record_task_failure(
        conn, task_id, error,
        outcome="spawn_failed",
        failure_limit=failure_limit,
        release_claim=True,
        end_run=True,
    )

def task_repository_evidence(row: sqlite3.Row) -> dict[str, Any]:
    """Capture read-only repository facts for a task's current workspace."""

    keys = set(row.keys())
    evidence = {
        "branch_name": row["branch_name"] if "branch_name" in keys else None,
        "project_id": row["project_id"] if "project_id" in keys else None,
        "workspace_kind": row["workspace_kind"] if "workspace_kind" in keys else None,
        "workspace_path": row["workspace_path"] if "workspace_path" in keys else None,
    }
    workspace_path = evidence["workspace_path"]
    if not workspace_path:
        evidence["available"] = False
        return evidence
    git_executable = shutil.which("git")
    if git_executable is None:
        evidence["available"] = False
        return evidence
    root = _git_toplevel(
        Path(str(workspace_path)).expanduser(),
        git_executable=git_executable,
    )
    if root is None:
        evidence["available"] = False
        return evidence

    def git_output(*args: str) -> Optional[str]:
        try:
            result = subprocess.run(
                [git_executable, "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (result.stdout or "").strip() if result.returncode == 0 else None

    def git_bytes(*args: str) -> Optional[bytes]:
        try:
            result = subprocess.run(
                [git_executable, "-C", str(root), *args],
                capture_output=True,
                text=False,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    head = git_output("rev-parse", "HEAD")
    status = git_output("status", "--porcelain")
    branch = git_output("branch", "--show-current")
    tracked_diff = git_bytes("diff", "--binary", "HEAD", "--")
    untracked = git_bytes("ls-files", "--others", "--exclude-standard", "-z")
    worktree_digest = None
    if tracked_diff is not None and untracked is not None:
        digest = hashlib.sha256(tracked_diff)
        for relative_bytes in sorted(path for path in untracked.split(b"\0") if path):
            digest.update(relative_bytes)
            try:
                digest.update((root / os.fsdecode(relative_bytes)).read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
        worktree_digest = digest.hexdigest()
    evidence.update(
        {
            "available": (
                head is not None
                and status is not None
                and worktree_digest is not None
            ),
            "repository_root": str(root),
            "head": head,
            "current_branch": branch or None,
            "status_porcelain": status.splitlines() if status is not None else None,
            "worktree_digest": worktree_digest,
        }
    )
    return evidence

def parent_results(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, Optional[str]]]:
    """Return ``(parent_id, result)`` for every done parent of ``task_id``."""
    rows = conn.execute(
        """
        SELECT t.id AS id, t.result AS result
        FROM tasks t
        JOIN task_links l ON l.parent_id = t.id
        WHERE l.child_id = ? AND t.status = 'done'
        ORDER BY t.completed_at ASC
        """,
        (task_id,),
    ).fetchall()
    return [(r["id"], r["result"]) for r in rows]


# Split-module bindings are intentionally installed at the tail: the siblings
# import this facade as ``_kb`` and need its row models and core transitions to
# be fully defined before their implementations are loaded.
from hermes_cli.kanban_db_connect import (  # noqa: E402
    _INITIALIZED_PATHS,
    _is_busy_error,
    connect,
    connect_closing,
    init_db,
    repair_db,
    write_txn,
)
from hermes_cli.kanban_db_workspace import (  # noqa: E402
    _cleanup_workspace,
    _is_managed_scratch_path,
    _managed_scratch_path_info,
    _maybe_emit_scratch_tip,
    _scratch_workspace,
    resolve_workspace,
    set_branch_name,
    set_workspace_path,
)
from hermes_cli.kanban_db_dispatch import (  # noqa: E402
    DEFAULT_FAILURE_LIMIT,
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    DispatchResult,
    _clear_failure_counter,
    _defer_reclaim_for_live_worker,
    _pid_alive,
    _record_task_failure,
    _default_spawn,
    _set_worker_pid,
    _terminate_reclaimed_worker,
    _worker_survived_termination,
    _worker_terminal_timeout_env,
    dispatch_once,
)


GENERIC_BOARD_COLUMNS: list[dict[str, str]] = [
    {"name": "triage", "label": "Triage", "status": "triage", "help": "Raw ideas — a specifier will flesh out the spec"},
    {"name": "todo", "label": "Todo", "status": "todo", "help": "Waiting on dependencies or unassigned"},
    {"name": "scheduled", "label": "Scheduled", "status": "scheduled", "help": "Waiting on a known time delay or scheduled follow-up"},
    {"name": "ready", "label": "Ready", "status": "ready", "help": "Dependencies satisfied; assign a profile to dispatch"},
    {"name": "running", "label": "In Progress", "status": "running", "help": "Claimed by a worker — in-flight"},
    {"name": "blocked", "label": "Blocked", "status": "blocked", "help": "Worker asked for human input"},
    {"name": "review", "label": "Review", "status": "review", "help": "Needs independent review"},
    {"name": "done", "label": "Done", "status": "done", "help": "Completed"},
]

BOARD_PRESETS: dict[str, list[dict[str, str]]] = {
    "generic": GENERIC_BOARD_COLUMNS,
    "product": PRODUCT_BOARD_COLUMNS,
}


def _sync_board_governance(
    conn: sqlite3.Connection, board: Optional[str]
) -> None:
    """Mirror the board's qualification gate into its SQLite write boundary."""

    if board is None:
        return
    metadata = read_board_metadata(board)
    qualification = metadata.get("qualification")
    required = int(
        isinstance(qualification, dict) and qualification.get("required") is True
    )
    row = conn.execute(
        "SELECT qualification_required FROM board_governance WHERE id = 1"
    ).fetchone()
    if row is not None and int(row["qualification_required"]) == required:
        return
    with authorized_governance_write():
        conn.execute(
            "INSERT INTO board_governance (id, qualification_required) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET qualification_required = excluded.qualification_required",
            (required,),
        )


def _migrate_qualification_intake_lifecycle(conn: sqlite3.Connection) -> None:
    """Add the direct-PO lease lifecycle without losing immutable intake data."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'qualification_intake'"
    ).fetchone()
    if row is None:
        return
    table_sql = str(row["sql"] or "")
    cols = {
        str(item["name"])
        for item in conn.execute("PRAGMA table_info(qualification_intake)")
    }
    required = {
        "idempotency_digest",
        "current_run_id",
        "claim_lock",
        "claim_expires",
    }
    legacy_exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'qualification_intake_legacy'"
    ).fetchone() is not None
    if legacy_exists and required <= cols and "needs_clarification" in table_sql:
        try:
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                INSERT OR IGNORE INTO qualification_intake (
                    id, raw_request, source, session_id, attachments_json,
                    status, created_at, updated_at
                )
                SELECT id, raw_request, source, session_id, attachments_json,
                       status, created_at, updated_at
                FROM qualification_intake_legacy;
                DROP TABLE qualification_intake_legacy;
                COMMIT;
                """
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        legacy_exists = False
    if not required <= cols or "needs_clarification" not in table_sql:
        try:
            conn.executescript(
                """
            BEGIN IMMEDIATE;
            DROP TRIGGER IF EXISTS qualification_intake_immutable_fields;
            DROP TRIGGER IF EXISTS qualification_intake_no_delete;
            DROP TRIGGER IF EXISTS qualification_intake_status_requires_decision;
            DROP TRIGGER IF EXISTS qualification_intake_no_terminal_reopen;
            DROP TRIGGER IF EXISTS strict_requalification_intake_service_insert;
            DROP INDEX IF EXISTS idx_qualification_intake_status;
            ALTER TABLE qualification_intake RENAME TO qualification_intake_legacy;
            CREATE TABLE qualification_intake (
                id TEXT PRIMARY KEY,
                raw_request TEXT NOT NULL,
                source TEXT NOT NULL,
                session_id TEXT,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                idempotency_digest TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'running', 'needs_clarification',
                        'attention_required', 'qualified', 'rejected', 'overridden'
                    )),
                current_run_id INTEGER,
                claim_lock TEXT,
                claim_expires INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO qualification_intake (
                id, raw_request, source, session_id, attachments_json,
                status, created_at, updated_at
            )
            SELECT id, raw_request, source, session_id, attachments_json,
                   status, created_at, updated_at
            FROM qualification_intake_legacy;
            DROP TABLE qualification_intake_legacy;
            COMMIT;
            """
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    _reconcile_legacy_requalification_duplicates(conn)
    try:
        conn.executescript(
            """
        BEGIN IMMEDIATE;
        CREATE INDEX IF NOT EXISTS idx_qualification_intake_status
            ON qualification_intake(status, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_qualification_intake_idempotency
            ON qualification_intake(idempotency_digest)
            WHERE idempotency_digest IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_requalification_one_active_target
            ON qualification_intake(json_extract(raw_request, '$.target_task_id'))
            WHERE json_valid(raw_request)
              AND json_extract(raw_request, '$.kind') = 'task_requalification'
              AND status IN (
                  'pending', 'running', 'needs_clarification', 'attention_required'
              );
        CREATE INDEX IF NOT EXISTS idx_qualification_intake_runs
            ON qualification_intake_runs(intake_id, id);
        CREATE INDEX IF NOT EXISTS idx_qualification_intake_events
            ON qualification_intake_events(intake_id, id);

        DROP TRIGGER IF EXISTS qualification_intake_immutable_fields;
        CREATE TRIGGER qualification_intake_immutable_fields
        BEFORE UPDATE ON qualification_intake
        WHEN NEW.id IS NOT OLD.id
          OR NEW.raw_request IS NOT OLD.raw_request
          OR NEW.source IS NOT OLD.source
          OR NEW.session_id IS NOT OLD.session_id
          OR NEW.attachments_json IS NOT OLD.attachments_json
          OR NEW.idempotency_digest IS NOT OLD.idempotency_digest
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'qualification intake provenance is immutable');
        END;
        DROP TRIGGER IF EXISTS qualification_intake_no_delete;
        CREATE TRIGGER qualification_intake_no_delete
        BEFORE DELETE ON qualification_intake BEGIN
            SELECT RAISE(ABORT, 'qualification intake provenance is immutable');
        END;
        DROP TRIGGER IF EXISTS qualification_intake_status_requires_decision;
        CREATE TRIGGER qualification_intake_status_requires_decision
        BEFORE UPDATE OF status ON qualification_intake
        WHEN NEW.status IS NOT OLD.status
         AND NEW.status IN ('qualified', 'rejected', 'overridden')
         AND COALESCE(
             (SELECT decision FROM qualification_intake_decisions
              WHERE intake_id = OLD.id ORDER BY id DESC LIMIT 1),
             ''
         ) != NEW.status
        BEGIN
            SELECT RAISE(ABORT, 'qualification status requires an append-only decision');
        END;
        DROP TRIGGER IF EXISTS qualification_intake_no_terminal_reopen;
        CREATE TRIGGER qualification_intake_no_terminal_reopen
        BEFORE UPDATE OF status ON qualification_intake
        WHEN OLD.status IN ('qualified', 'rejected', 'overridden')
         AND NEW.status NOT IN ('qualified', 'rejected', 'overridden')
        BEGIN
            SELECT RAISE(ABORT, 'terminal qualification intake cannot be reopened');
        END;
        COMMIT;
        """
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _reconcile_legacy_requalification_duplicates(
    conn: sqlite3.Connection,
) -> None:
    """Reject older active requalification duplicates before indexing targets."""

    placeholders = ",".join("?" for _ in ACTIVE_REQUALIFICATION_STATUSES)
    duplicate_targets = conn.execute(
        f"""
        SELECT json_extract(raw_request, '$.target_task_id') AS target_task_id
          FROM qualification_intake
         WHERE json_valid(raw_request)
           AND json_extract(raw_request, '$.kind') = 'task_requalification'
           AND json_type(raw_request, '$.target_task_id') = 'text'
           AND status IN ({placeholders})
         GROUP BY json_extract(raw_request, '$.target_task_id')
        HAVING COUNT(*) > 1
        """,
        ACTIVE_REQUALIFICATION_STATUSES,
    ).fetchall()
    if not duplicate_targets:
        return

    now = int(time.time())
    with write_txn(conn):
        for target in duplicate_targets:
            rows = conn.execute(
                f"""
                SELECT id
                  FROM qualification_intake
                 WHERE json_valid(raw_request)
                   AND json_extract(raw_request, '$.kind') = 'task_requalification'
                   AND json_extract(raw_request, '$.target_task_id') = ?
                   AND status IN ({placeholders})
                 ORDER BY created_at DESC, id DESC
                """,
                (target["target_task_id"], *ACTIVE_REQUALIFICATION_STATUSES),
            ).fetchall()
            if len(rows) < 2:
                continue
            newest_id = str(rows[0]["id"])
            reason = f"superseded by active requalification intake {newest_id}"
            for row in rows[1:]:
                intake_id = str(row["id"])
                conn.execute(
                    """
                    INSERT INTO qualification_intake_decisions (
                        intake_id, decision, actor_profile, reason, contract_id, created_at
                    ) VALUES (?, 'rejected', 'hermes-migration', ?, NULL, ?)
                    """,
                    (intake_id, reason, now),
                )
                conn.execute(
                    f"""
                    UPDATE qualification_intake
                       SET status = 'rejected', updated_at = ?
                     WHERE id = ? AND status IN ({placeholders})
                    """,
                    (now, intake_id, *ACTIVE_REQUALIFICATION_STATUSES),
                )


def _ensure_qualification_boundary_objects(conn: sqlite3.Connection) -> None:
    """Install strict-write objects only after legacy task columns exist."""

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_work_contract_unique "
        "ON tasks(work_contract_id) WHERE work_contract_id IS NOT NULL"
    )
    required_tables = {
        "board_governance",
        "work_contracts",
        "qualification_intake_decisions",
        "task_links",
        "epic_memberships",
    }
    present_tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not required_tables <= present_tables:
        return
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS strict_tasks_require_qualification;
        CREATE TRIGGER IF NOT EXISTS strict_tasks_require_qualification_v2
        BEFORE INSERT ON tasks
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND (
             NEW.work_contract_id IS NULL
             OR NOT EXISTS (
                 SELECT 1
                 FROM work_contracts wc
                 JOIN qualification_intake_decisions qd
                   ON qd.intake_id = wc.request_id
                 WHERE wc.id = NEW.work_contract_id
                   AND qd.decision IN ('qualified', 'overridden')
                   AND qd.id = (
                       SELECT MAX(latest.id)
                       FROM qualification_intake_decisions latest
                       WHERE latest.intake_id = wc.request_id
                   )
                   AND (
                       qd.contract_id = wc.id
                       OR (
                           json_extract(wc.canonical_json, '$.work.item_kind') = 'card'
                           AND EXISTS (
                               SELECT 1
                               FROM tasks epic
                               WHERE epic.id = json_extract(
                                   wc.canonical_json, '$.routing.epic_id'
                               )
                                 AND epic.work_item_kind = 'epic'
                                 AND epic.work_contract_id = qd.contract_id
                           )
                       )
                   )
             )
         )
        BEGIN
            SELECT RAISE(ABORT, 'qualification required before task materialization');
        END;
        CREATE TRIGGER IF NOT EXISTS strict_requalification_intake_service_insert
        BEFORE INSERT ON qualification_intake
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND EXISTS (
             SELECT 1
               FROM json_each(
                   CASE WHEN json_valid(NEW.raw_request) = 1
                        THEN NEW.raw_request ELSE '{}'
                   END
               )
              WHERE json_each.key = 'kind'
                AND json_each.value = 'task_requalification'
         )
         AND hermes_governance_write_authorized() != 1
        BEGIN
            SELECT RAISE(ABORT, 'requalification intake requires Hermes service authority');
        END;
        CREATE TRIGGER IF NOT EXISTS strict_task_links_service_insert
        BEFORE INSERT ON task_links
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND hermes_governance_write_authorized() != 1
        BEGIN
            SELECT RAISE(ABORT, 'strict-board dependencies are Work Contract-owned');
        END;
        CREATE TRIGGER IF NOT EXISTS strict_tasks_service_delete
        BEFORE DELETE ON tasks
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND hermes_governance_write_authorized() != 1
        BEGIN
            SELECT RAISE(ABORT, 'strict-board task deletion requires Hermes service authority');
        END;
        CREATE TRIGGER IF NOT EXISTS strict_tasks_service_archive
        BEFORE UPDATE OF status ON tasks
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND OLD.work_contract_id IS NOT NULL
         AND NEW.status = 'archived'
         AND OLD.status IS NOT NEW.status
         AND hermes_governance_write_authorized() != 1
        BEGIN
            SELECT RAISE(ABORT, 'strict-board archival requires Hermes service authority');
        END;
        CREATE TRIGGER IF NOT EXISTS strict_task_routing_service_update
        BEFORE UPDATE OF assignee, workflow_template_id, current_step_key,
                         work_contract_id, work_item_kind ON tasks
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND OLD.work_contract_id IS NOT NULL
         AND hermes_governance_write_authorized() != 1
         AND (
             NEW.assignee IS NOT OLD.assignee
             OR NEW.workflow_template_id IS NOT OLD.workflow_template_id
             OR NEW.current_step_key IS NOT OLD.current_step_key
             OR NEW.work_contract_id IS NOT OLD.work_contract_id
             OR NEW.work_item_kind IS NOT OLD.work_item_kind
         )
        BEGIN
            SELECT RAISE(ABORT, 'strict-board routing is Work Contract-owned');
        END;
        CREATE TRIGGER IF NOT EXISTS strict_task_links_service_delete
        BEFORE DELETE ON task_links
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND hermes_governance_write_authorized() != 1
        BEGIN
            SELECT RAISE(ABORT, 'strict-board dependencies are Work Contract-owned');
        END;
        CREATE TRIGGER IF NOT EXISTS strict_epic_memberships_service_insert
        BEFORE INSERT ON epic_memberships
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND hermes_governance_write_authorized() != 1
        BEGIN
            SELECT RAISE(ABORT, 'strict-board Epic membership is Work Contract-owned');
        END;
        CREATE TRIGGER IF NOT EXISTS strict_epic_memberships_service_delete
        BEFORE DELETE ON epic_memberships
        WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
         AND hermes_governance_write_authorized() != 1
        BEGIN
            SELECT RAISE(ABORT, 'strict-board Epic membership is Work Contract-owned');
        END;
        """
    )
