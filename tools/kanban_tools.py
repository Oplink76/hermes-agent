"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

Registered only under the dispatcher (``HERMES_KANBAN_TASK`` set) or when the profile
enables the ``kanban`` toolset. Tools rather than ``hermes kanban`` shell-outs: they run
in the agent's process (reach ``kanban.db`` from a container/SSH terminal backend, no
shlex quoting of JSON metadata, structured-JSON failures). Humans use CLI/dashboard.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from agent.redact import redact_sensitive_text
from hermes_cli.goals import judge_goal
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get, load_config
from tools.kanban_tools_schemas import (
    KANBAN_ATTACH_SCHEMA,
    KANBAN_ATTACH_URL_SCHEMA, KANBAN_ATTACHMENTS_SCHEMA, KANBAN_BLOCK_SCHEMA, KANBAN_COMMENT_SCHEMA,
    KANBAN_COMPLETE_SCHEMA, KANBAN_CREATE_SCHEMA, KANBAN_HEARTBEAT_SCHEMA, KANBAN_LINK_SCHEMA,
    KANBAN_LIST_SCHEMA, KANBAN_REQUEST_CHANGES_SCHEMA, KANBAN_REQUEST_REVIEW_SCHEMA,
    KANBAN_SHOW_SCHEMA, KANBAN_UNBLOCK_SCHEMA)


import time
from contextlib import contextmanager
from tools.kanban_tools_schemas import (
    KANBAN_CONFIGURE_SCHEMA, KANBAN_RESOLVE_SCHEMA, KANBAN_UNLINK_SCHEMA, REVIEW_TARGET_SCHEMA, WORK_INBOX_DECIDE_SCHEMA, WORK_INBOX_HEARTBEAT_SCHEMA, WORK_INBOX_SHOW_SCHEMA
)


logger = logging.getLogger(__name__)

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200
REVIEW_TARGET_PAGE_LINES = 400
REVIEW_TARGET_PAGE_CHARS = 48_000
REVIEW_TARGET_MAX_LINE_CHARS = 4_000
REVIEW_TARGET_FILE_LIST_LIMIT = 200
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Resolver-facing `kanban_show` is consumed inside a model context, so its
# safety limit applies to the complete serialized JSON document, not to any
# individual field or row. Keep this below the external 100 KB result limit.
KANBAN_SHOW_MAX_BYTES = 96_000


def _show_json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _show_text_envelope(
    text: str,
    budget: int,
    *,
    original_chars: Optional[int] = None,
    original_bytes: Optional[int] = None,
) -> dict[str, Any]:
    """Return a byte-bounded, explicit summary for one oversized value."""
    original_chars = len(text) if original_chars is None else original_chars
    original_bytes = (
        len(text.encode("utf-8")) if original_bytes is None else original_bytes
    )
    envelope: dict[str, Any] = {
        "truncated": True,
        "original_chars": original_chars,
        "original_bytes": original_bytes,
        "preview": "",
    }
    if budget <= 0 or _show_json_bytes(envelope) > budget:
        return envelope

    low, high = 0, len(text)
    best = envelope
    while low <= high:
        middle = (low + high) // 2
        candidate = dict(envelope)
        candidate["preview"] = text[:middle]
        if _show_json_bytes(candidate) <= budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _show_bounded_value(value: Any, budget: int) -> Any:
    """Keep small JSON values exact and summarize oversized values."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) <= budget:
            return value
        return _show_text_envelope(value, budget)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=str
        )
    except Exception:
        encoded = str(value)
    encoded_bytes = len(encoded.encode("utf-8"))
    if encoded_bytes <= budget:
        return value
    return _show_text_envelope(
        encoded,
        budget,
        original_chars=len(encoded),
        original_bytes=encoded_bytes,
    )


def _show_bounded_mapping(
    value: dict[str, Any],
    budget: int,
    *,
    preserve_keys: tuple[str, ...] = (),
    nested_preserve: Optional[dict[str, tuple[str, ...]]] = None,
) -> dict[str, Any]:
    """Bound a mapping while retaining selected small control fields."""
    if _show_json_bytes(value) <= budget:
        return value
    nested_preserve = nested_preserve or {}
    ordered_keys = list(preserve_keys) + [
        key for key in value if key not in preserve_keys
    ]
    result: dict[str, Any] = {}
    pending: list[str] = []
    omitted: list[str] = []

    def _bounded_child(key: str, child: Any, child_budget: int) -> Any:
        if isinstance(child, dict) and key in nested_preserve:
            return _show_bounded_mapping(
                child,
                child_budget,
                preserve_keys=nested_preserve[key],
            )
        return _show_bounded_value(child, child_budget)

    def _reserved_child_bytes(key: str, child: Any) -> int:
        """Estimate the minimum useful representation for a later field."""
        try:
            if _show_json_bytes(child) <= 2_048:
                reserved = child
            else:
                reserve_budget = min(
                    1_024,
                    max(256, budget // max(2, len(value) * 2)),
                )
                reserved = _bounded_child(key, child, reserve_budget)
            return _show_json_bytes({key: reserved})
        except Exception:
            # The actual child is still bounded and checked below; this only
            # keeps an unusual value from consuming the entire parent budget.
            return 128

    # Keep normal-sized values exact whenever the mapping budget allows it.
    # Oversized values are deferred so their previews cannot crowd out later
    # control fields that would otherwise fit exactly.
    for key in ordered_keys:
        if key not in value:
            continue
        candidate = dict(result)
        candidate[key] = value[key]
        if _show_json_bytes(candidate) <= budget:
            result[key] = value[key]
        else:
            pending.append(key)

    for index, key in enumerate(pending):
        child = value[key]
        current_bytes = _show_json_bytes(result)
        reserved_for_later = sum(
            _reserved_child_bytes(later_key, value[later_key])
            for later_key in pending[index + 1:]
        )
        key_bytes = len(json.dumps(key, ensure_ascii=False).encode("utf-8"))
        separator_bytes = 4 if result else 2
        child_budget = max(
            0,
            budget
            - current_bytes
            - reserved_for_later
            - key_bytes
            - separator_bytes,
        )
        bounded = _bounded_child(key, child, child_budget)
        candidate = dict(result)
        candidate[key] = bounded
        if _show_json_bytes(candidate) <= budget:
            result[key] = bounded
            continue

        # Estimates are deliberately conservative. If they left too little
        # room, retry with all currently available bytes before omitting this
        # optional field.
        available = max(
            0,
            budget - current_bytes - key_bytes - separator_bytes,
        )
        bounded = _bounded_child(key, child, available)
        candidate[key] = bounded
        if _show_json_bytes(candidate) <= budget:
            result[key] = bounded
        else:
            omitted.append(key)

    if omitted:
        marker = {
            "truncated": True,
            "omitted_count": len(omitted),
            "omitted_fields": omitted,
        }
        candidate = dict(result)
        candidate["_truncation"] = marker
        if _show_json_bytes(candidate) <= budget:
            result = candidate
    return result


def _show_bounded_preflight(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    """Preserve Resolver control fields while summarizing legacy evidence."""
    return _show_bounded_mapping(
        payload,
        budget,
        preserve_keys=(
            "kind",
            "original_assignee",
            "hermes_assignee",
            "step_key",
            "resume_status",
            "reason",
            "attempted_resolutions",
            "metadata",
        ),
        nested_preserve={"metadata": ("attempt_index",)},
    )


def _show_serialized(response: dict[str, Any]) -> str:
    return json.dumps(response, ensure_ascii=False)


# --- Gating ---

def _profile_has_kanban_toolset() -> bool:
    # load_config() is mtime-cached and check_fn results are TTL-cached (~30s).
    try:
        return "kanban" in load_config().get("toolsets", [])
    except Exception:
        return False


def _delegation_ctx(predicate: str, default: bool) -> bool:
    """``agent.delegation_context.<predicate>()``; ``default`` when it cannot be evaluated."""
    try:
        from agent import delegation_context
        return getattr(delegation_context, predicate)()
    except Exception:
        return default


def _is_delegated_child_context() -> bool:
    return _delegation_ctx("is_delegated_child_context", False)


def _is_dispatcher_owned_worker() -> bool:
    """False for delegate_task children AND for cron jobs fired in-process from
    a worker — i.e. whenever HERMES_KANBAN_* is present but not ours."""
    return _delegation_ctx("is_dispatcher_owned_worker_context", True)


def _visible(*, to_env_worker: bool) -> bool:
    """check_fn core: never for delegate children; dispatcher-spawned env workers
    (HERMES_KANBAN_TASK) per flag; else the profile toolset decides."""
    if _is_delegated_child_context():
        return False
    if os.environ.get("HERMES_KANBAN_TASK") and _is_dispatcher_owned_worker():
        return to_env_worker
    return _profile_has_kanban_toolset()


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if _is_delegated_child_context():
        return False
    if os.environ.get("HERMES_WORK_INBOX_INTAKE"):
        return False
    if os.environ.get("HERMES_KANBAN_TASK") and _is_dispatcher_owned_worker():
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if _is_delegated_child_context():
        return False
    if os.environ.get("HERMES_WORK_INBOX_INTAKE"):
        return False
    if os.environ.get("HERMES_KANBAN_TASK") and _is_dispatcher_owned_worker():
        return False
    return _profile_has_kanban_toolset()


def _check_resolver_mode() -> bool:
    """Expose the Resolver mutation only to a task-scoped Resolver run."""
    return bool(os.environ.get("HERMES_KANBAN_TASK")) and (
        os.environ.get("HERMES_PROFILE") == "resolver"
    )


def _check_reviewer_mode() -> bool:
    """Expose immutable review input only to the current Reviewer worker."""
    return bool(os.environ.get("HERMES_KANBAN_TASK")) and (
        os.environ.get("HERMES_PROFILE") == "reviewer"
    )


def _check_ordinary_worker_mode() -> bool:
    """Normal lifecycle exits are unavailable to the privileged Resolver."""
    return _check_kanban_mode() and not _check_resolver_mode()


def _check_work_inbox_mode() -> bool:
    """Expose only intake authority to an exact Product Owner intake run."""
    if _is_delegated_child_context():
        return False
    if not (
        os.environ.get("HERMES_WORK_INBOX_INTAKE")
        and os.environ.get("HERMES_WORK_INBOX_RUN_ID")
        and os.environ.get("HERMES_WORK_INBOX_CLAIM_LOCK")
        and os.environ.get("HERMES_PROFILE")
    ):
        return False
    capability = os.environ.get("HERMES_MCP_CAPABILITY_SET")
    return capability in {None, "", "product-owner-intake"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class _Reject(Exception):
    """Carries a finished ``tool_error`` payload out of a validation helper."""

    def __init__(self, message: str):
        super().__init__(tool_error(message))



def _check(cond: Any, message: str) -> None:
    """Reject (as a tool error) unless ``cond`` is truthy."""
    if not cond:
        raise _Reject(message)



def _kanban_handler(tool_name: str) -> Callable:
    """Wrap a handler so every failure is a structured tool error. ``ValueError``
    (invalid board slug, DB validation such as cycle/self-link, ``AttachmentTooLarge``)
    is reported without a traceback; anything else is logged with ``logger.exception``."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(args: dict, **kw) -> str:
            try:
                return fn(args, **kw)
            except _Reject as e:
                return e.args[0]
            except Exception as e:
                if not isinstance(e, ValueError):
                    logger.exception(f"{tool_name} failed")
                return tool_error(f"{tool_name}: {e}")
        return wrapper
    return deco



def _reject_delegated_child_mutation(tool_name: str) -> None:
    """A delegate_task child shares the parent's process, so inherited HERMES_KANBAN_*
    env is not proof of ownership: it may report findings but must not mutate."""
    if _delegation_ctx("is_delegated_child_process_context", False):
        raise _Reject(
            f"{tool_name} refused: delegate_task child agents are not Kanban run owners. "
            "Return findings to the parent agent; the dispatcher worker or an explicitly "
            "configured Kanban orchestrator must perform board mutations.")



def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """``task_id`` arg or the dispatcher's env var. A delegate child or an
    in-process cron job must never inherit the worker's task id implicitly."""
    if arg:
        return arg
    if _is_delegated_child_context() or not _is_dispatcher_owned_worker():
        return None
    return os.environ.get("HERMES_KANBAN_TASK") or None


def _require_task_id(args: dict) -> str:
    tid = _default_task_id(args.get("task_id"))
    _check(tid, "task_id is required (or set HERMES_KANBAN_TASK in the env)")
    return tid


def _own_task_env(task_id: str, var: str) -> Optional[str]:
    """``$var`` only when this worker is scoped to ``task_id``; else None."""
    return os.environ.get(var) if os.environ.get("HERMES_KANBAN_TASK") == task_id else None


def _worker_run_id(task_id: str) -> Optional[int]:
    """This worker's dispatcher run id when it is scoped to task_id."""
    raw = _own_task_env(task_id, "HERMES_KANBAN_RUN_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _stamp_worker_session_metadata(task_id: str, metadata: Optional[dict]) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    session_id = _own_task_env(task_id, "HERMES_SESSION_ID")
    return {**(metadata or {}), "worker_session_id": session_id} if session_id else metadata


def _enforce_worker_task_ownership(tid: str) -> None:
    """A dispatcher-spawned worker may only mutate its own HERMES_KANBAN_TASK; a
    prompt-injected ``task_id`` must not corrupt sibling/cross-tenant runs.
    Orchestrators (toolset enabled, no env task) legitimately route child tasks.

    Tools like ``kanban_complete`` / ``kanban_block`` / ``kanban_heartbeat`` mutate run-lifecycle state, so
    a buggy or prompt-injected worker that passed an explicit ``task_id`` for some other task could corrupt
    sibling or cross-tenant runs (see #19534).
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and tid != env_tid:
        raise _Reject(
            f"worker is scoped to task {env_tid}; refusing to mutate {tid}. Use kanban_comment "
            f"to hand off information to other tasks, or kanban_create to spawn follow-up work.")


def _worker_guard(tool_name: str, args: dict) -> str:
    """Worker mutation preamble, in order: delegate-child rejection, task id
    resolution, task-scope ownership. Returns the task id."""
    _reject_delegated_child_mutation(tool_name)
    tid = _require_task_id(args)
    _enforce_worker_task_ownership(tid)
    return tid


def _require_orchestrator_tool(tool_name: str) -> None:
    """The check_fn already hides orchestrator tools from workers; this catches
    a stale registration or test harness routing a worker here anyway."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise _Reject(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers must use "
            "kanban_complete, kanban_block, kanban_heartbeat, or kanban_comment for their "
            "assigned task.")


def _connect(board: Optional[str] = None):
    """Open the selected board lazily for the existing tool handlers."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    return kb, kbc.connect(board=board)


@contextmanager
def _board(board: Optional[str], *, quiet_close: bool = False):
    """``with _board(slug) as (kb, conn)``; lazy import so the module loads in non-kanban
    contexts. ``board=None`` keeps the env/symlink resolution chain; an explicit slug
    overrides it per call. ``quiet_close`` swallows close() errors (best-effort bridges)."""
    kb, conn = _connect(board)
    try:
        yield kb, conn
    finally:
        try:
            conn.close()
        except Exception:
            if not quiet_close:
                raise


def _existing_task(kb, conn, tid: str):
    task = kb.get_task(conn, tid)
    _check(task is not None, f"task {tid} not found")
    return task


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _ok_landed(kb, conn, tid: str, default_status: str, **extra: Any) -> str:
    """Success payload reporting where the task actually landed (routing may
    not leave it in the requested status)."""
    run = kb.latest_run(conn, tid)
    landed = kb.get_task(conn, tid)
    return _ok(task_id=tid, run_id=run.id if run else None,
               status=landed.status if landed else default_status, **extra)


def _redact(value: Any) -> str:
    return redact_sensitive_text(str(value), force=True)


def _redact_opt(value: Any) -> Any:
    return _redact(value) if value else value


def _redact_metadata(metadata: dict) -> Optional[dict]:
    """Redact via a JSON round-trip; None if the result can't be re-parsed."""
    try:
        return json.loads(redact_sensitive_text(json.dumps(metadata), force=True))
    except json.JSONDecodeError:
        return None


def _coerce_str_list(value: Any, name: str, what: str, *, strip: bool = False):
    """Accept a single string (convenience) or a list/tuple; with ``strip`` the
    items are stringified, stripped, and empties dropped."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise _Reject(f"{name} must be a list of {what}, got {type(value).__name__}")
    if strip:
        value = [str(x).strip() for x in value if str(x).strip()]
    return value


def _require_dict_metadata(metadata: Any) -> None:
    _check(metadata is None or isinstance(metadata, dict),
           f"metadata must be an object/dict, got {type(metadata).__name__}")


def _merge_artifacts(metadata: Any, artifacts: list[str]) -> dict:
    """Fold ``artifacts`` into ``metadata["artifacts"]`` (merged with, never overwriting, a
    list the worker passed manually). Artifacts ride inside metadata so the completed-event
    payload needs no DB schema change; the gateway notifier uploads each as an attachment."""
    _require_dict_metadata(metadata)
    metadata = {} if metadata is None else metadata
    existing = metadata.get("artifacts")
    if isinstance(existing, (list, tuple)):
        merged = (str(item).strip() for item in [*existing, *artifacts])
        metadata["artifacts"] = list(dict.fromkeys(s for s in merged if s))
    else:
        metadata["artifacts"] = artifacts
    return metadata


def _require_text(args: dict, name: str, message: Optional[str] = None) -> Any:
    """``args[name]``; rejects when missing or blank."""
    value = args.get(name)
    _check(value and str(value).strip(), message or f"{name} is required")
    return value


_BOOL_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}


def _parse_bool_arg(args: dict, name: str) -> bool:
    value = args.get(name)
    if value is None or isinstance(value, bool):
        return bool(value)
    parsed = _BOOL_WORDS.get(str(value).strip().lower())
    _check(parsed is not None, f"{name} must be a boolean or 'true'/'false'")
    return parsed


def _opt_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    return int(value) if value is not None else default


_TASK_FIELDS = tuple(
    "id title body assignee status tenant priority workspace_kind workspace_path created_by "
    "created_at started_at completed_at result current_run_id model_override "
    "provider_override completion_contract last_failure_error".split())
_TASK_SUMMARY_FIELDS = tuple(
    "id title assignee status priority tenant workspace_kind workspace_path project_id created_by "
    "created_at started_at completed_at current_run_id model_override provider_override".split())
_RUN_FIELDS = tuple("id profile status outcome summary error metadata started_at ended_at".split())
_COMMENT_FIELDS = ("author", "body", "created_at")
_EVENT_FIELDS = ("kind", "payload", "created_at", "run_id")
_ATTACHMENT_FIELDS = tuple(
    "id filename content_type size uploaded_by stored_path created_at".split())
_CREATED_FIELDS = ("status", "workspace_kind", "workspace_path", "project_id")


def _fields(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """``{name: getattr(obj, name)}``; every value None when ``obj`` is None."""
    return {n: getattr(obj, n) if obj is not None else None for n in names}


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    epic_id = kb.epic_id_for_task(conn, task.id)
    epic = kb.get_task(conn, epic_id) if epic_id else None
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "project_id": task.project_id,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "provider_override": task.provider_override,
        "work_item_kind": task.work_item_kind,
        "epic": (
            {"id": epic_id, "title": epic.title if epic is not None else epic_id}
            if epic_id
            else None
        ),
        "dependencies": parents,
        "dependents": children,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# --- Goal-mode judge gate ---

_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input"})


def _goal_judge_available() -> bool:
    """``judge_goal`` fails open (no auxiliary model -> ``"continue"``), which is
    indistinguishable from "not done yet" and would wedge every goal_mode
    worker; so the gate is enforced only when a judge is actually reachable."""
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        return False
    return client is not None and bool(model)


# Per-tool guidance for a judge rejection: verdict -> message. ``{reason}``/``{tid}`` are filled in.
_GOAL_GATE_MESSAGES = {
    "kanban_complete": {
        "blocked": (
            "Goal completion rejected: judge ruled the goal unachievable — {reason}. The task "
            "will NOT complete silently. Either re-scope the task with kanban_edit, or record "
            "the block with kanban_block and hand the decision to a human / reviewer."),
        "continue": (
            "Goal completion rejected by judge: {reason}. To proceed, either: (1) provide "
            "explicit acceptance evidence in your summary matching the task's criteria, or (2) "
            "create continuation tasks with parents=[{tid}] and keep this task alive.")},
    "kanban_request_review": {
        "blocked": (
            "Goal review handoff rejected: judge ruled the goal unachievable — {reason}. "
            "Record the block with kanban_block instead of requesting review."),
        "continue": (
            "Goal review handoff rejected by judge: {reason}. Provide acceptance evidence "
            "matching the card before requesting review.")}}


def _goal_gate(tool_name: str, task, tid: str, evidence: str) -> None:
    """Goal-mode pre-handoff judge gate: a worker must not complete / request
    review before acceptance criteria are met. ``blocked`` gets its own
    guidance; any other non-``done`` verdict gets the ``continue`` guidance.
    A broken judge fails open (logged) so it cannot permanently wedge work."""
    if not task or not task.goal_mode or not _goal_judge_available():
        return
    try:
        verdict, reason, _, _, _ = judge_goal(
            goal=f"{task.title}\n\n{task.body or ''}".strip(), last_response=evidence.strip())
    except Exception as judge_exc:
        logger.warning(
            "goal judge check failed, allowing lifecycle handoff: %s", judge_exc, exc_info=True)
        return
    if verdict == "done":
        return
    key = "blocked" if verdict == "blocked" else "continue"
    raise _Reject(_GOAL_GATE_MESSAGES[tool_name][key].format(reason=reason, tid=tid))


# --- Runtime-activity → board bridges (auto-heartbeat, live comment injection) ---
# The dispatcher watchdog reads ``tasks.last_heartbeat_at``, not the agent's in-process
# activity timestamp, so normal work is mirrored onto the board here (``kanban_heartbeat``
# stays for notes / pre-extending a claim). Best-effort: never raise into the agent loop;
# rate-limited per process (a race costs one harmless extra write); no-op outside a
# dispatcher-spawned worker.

# --------------------------------------------------------------------------- Runtime-activity →
# board-heartbeat bridge (#31752)
# --------------------------------------------------------------------------- When the agent ticks
# ``_touch_activity`` during normal work (between tool calls, mid-stream chunks, etc.), we want the kanban
# board's ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher watchdog (which reads
# ``tasks.last_heartbeat_at``, not the agent's in-process timestamp) doesn't reclaim an actively-running
# worker as stale. The model is not required to call the explicit ``kanban_heartbeat`` tool for this to work
# — that tool stays available for workers that want to attach a note or pre-emptively extend a claim across
# a known-long op. Constraints: - Best-effort: never raise. The agent loop must not care if the bridge fails
# (board missing, DB locked, etc.). - Rate-limited to one DB write per 60s per-process; runtime activity can
# tick on every chunk/tool result and we don't need that resolution. - No-op outside dispatcher-spawned
# worker context (no ``HERMES_KANBAN_TASK``). - No durable note on these auto-heartbeats; that's reserved
# for the explicit tool which carries a model-supplied note.
_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env() -> bool:
    """Claim extension + board heartbeat for the current worker; True iff a write was
    attempted. ``HERMES_KANBAN_RUN_ID`` pins the run row so a reclaimed stale run is not
    heartbeated; ``HERMES_KANBAN_CLAIM_LOCK`` absent -> default claimer (local workers)."""
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    now = time.monotonic()
    if not tid or (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        from hermes_cli import kanban_db_dispatch as kbd
        with _board(None, quiet_close=True) as (kb, conn):
            ops = ((kb.heartbeat_claim, {"claimer": os.environ.get("HERMES_KANBAN_CLAIM_LOCK")}),
                   (kbd.heartbeat_worker, {"note": None, "expected_run_id": _worker_run_id(tid)}))
            for fn, kwargs in ops:
                op = fn.__name__
                try:
                    fn(conn, tid, **kwargs)
                except Exception:
                    logger.debug("auto-heartbeat: %s failed", op, exc_info=True)
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


# Live operator-note injection: poll the task for new comments and steer them in
# OUT-OF-BAND, so a user can talk to a running task without block → comment → unblock.
# Watermarked per task (seeded on first poll: that history is already in the context).
_COMMENT_POLL_MIN_INTERVAL_SECONDS = 6.0
_comment_poll_last_attempt: float = 0.0
_comment_watermark: dict[str, int] = {}


def inject_new_comments_from_env(agent: Any) -> bool:
    """Steer new operator comments on the worker's task into ``agent``; True iff a
    steer was injected; never raises. Own comments (``HERMES_PROFILE``) are skipped."""
    global _comment_poll_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    now = time.monotonic()
    if (not tid or agent is None or not hasattr(agent, "steer")
            or (now - _comment_poll_last_attempt) < _COMMENT_POLL_MIN_INTERVAL_SECONDS):
        return False
    _comment_poll_last_attempt = now
    seen = _comment_watermark.get(tid)
    try:
        with _board(None, quiet_close=True) as (kb, conn):
            rows = kb.list_comments_after(conn, tid, after_id=seen or 0)
    except Exception:
        logger.debug("comment-inject: bridge failed", exc_info=True)
        return False
    if seen is None:
        _comment_watermark[tid] = max((c.id for c in rows), default=0)
    if seen is None or not rows:
        return False
    # Advance past everything read (including our own notes) so nothing is re-injected.
    _comment_watermark[tid] = max(c.id for c in rows)
    own = (os.environ.get("HERMES_PROFILE") or "").strip()
    fresh = [c for c in rows if (c.author or "").strip() != own and (c.body or "").strip()]
    if not fresh:
        return False
    lines = [f"- {c.author or 'operator'}: {c.body.strip()}" for c in fresh]
    note = ("New note" + ("s" if len(fresh) > 1 else "")
            + " on your kanban task from the operator (delivered mid-run). "
            + "Take it into account for the work you're doing right now:\n" + "\n".join(lines))
    try:
        return bool(agent.steer(note))
    except Exception:
        logger.debug("comment-inject: steer failed", exc_info=True)
        return False


# --- Handlers ---

@_kanban_handler("kanban_show")
def _handle_show(args: dict, **kw) -> str:
    """Read a task's state, with a bounded view for task-scoped Resolver calls."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            comments = kb.list_comments(conn, tid)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)
            epic_id = kb.epic_id_for_task(conn, tid)
            epic = kb.get_task(conn, epic_id) if epic_id else None
            resolver_view = (
                os.environ.get("HERMES_PROFILE") == "resolver"
                and os.environ.get("HERMES_KANBAN_TASK") == tid
            )

            def _bounded_text(value, limit):
                return _show_bounded_value("" if value is None else str(value), limit)

            def _bounded_value(value, limit):
                return _show_bounded_value(value, limit)

            def _task_dict(t, *, field_budget: Optional[int] = None):
                def _field(value):
                    return value if field_budget is None else _show_bounded_value(
                        value, field_budget
                    )

                return {
                    "id": t.id, "title": _field(t.title),
                    "body": _field(t.body),
                    "assignee": t.assignee, "status": t.status,
                    "tenant": _field(t.tenant), "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": _field(t.created_by), "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": _field(t.result),
                    "current_run_id": t.current_run_id,
                    "model_override": _field(t.model_override),
                    "provider_override": _field(t.provider_override),
                    "project_id": t.project_id,
                    "branch_name": t.branch_name,
                    "workflow_template_id": t.workflow_template_id,
                    "current_step_key": t.current_step_key,
                    "running": t.running,
                    "blocked": t.blocked,
                    "work_item_kind": t.work_item_kind,
                    **kb.task_execution_contract(t),
                }

            def _run_dict(
                r,
                *,
                text_budget: Optional[int] = None,
                value_budget: Optional[int] = None,
            ):
                def _text(value):
                    return value if text_budget is None else _show_bounded_value(
                        value, text_budget
                    )

                def _value(value):
                    return value if value_budget is None else _show_bounded_value(
                        value, value_budget
                    )

                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": _text(r.summary),
                    "error": _text(r.error),
                    "metadata": _value(r.metadata),
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            contract = kb.work_contract_view(conn, task.work_contract_id)
            if resolver_view:
                preflight = kb._latest_unresolved_product_preflight(conn, tid)
                expected = kb.resolver_expected_snapshot(conn, tid)

                def _build_resolver_response(
                    *,
                    comment_limit: int,
                    event_limit: int,
                    run_limit: int,
                    text_budget: int,
                    value_budget: int,
                    field_budget: int,
                    contract_field_budget: int,
                    preflight_budget: int,
                    relation_budget: int,
                ) -> dict[str, Any]:
                    bounded_contract = (
                        {
                            key: _show_bounded_value(value, contract_field_budget)
                            for key, value in contract.items()
                        }
                        if isinstance(contract, dict)
                        else _show_bounded_value(contract, contract_field_budget)
                    )
                    shown_comments = comments[-comment_limit:] if comment_limit else []
                    shown_events = events[-event_limit:] if event_limit else []
                    shown_runs = runs[-run_limit:] if run_limit else []
                    preflight_payload = (
                        preflight[1]
                        if preflight is not None and isinstance(preflight[1], dict)
                        else {}
                    )
                    bounded_preflight = (
                        {
                            "event_id": preflight[0],
                            "payload": _show_bounded_preflight(
                                preflight_payload, preflight_budget
                            ),
                        }
                        if preflight is not None
                        else None
                    )
                    response = {
                        "task": _task_dict(task, field_budget=field_budget),
                        "work_contract": bounded_contract,
                        "epic": (
                            {
                                "id": epic_id,
                                "title": _show_bounded_value(
                                    epic.title if epic is not None else epic_id,
                                    field_budget,
                                ),
                            }
                            if epic_id
                            else None
                        ),
                        "dependencies": _show_bounded_value(
                            parents, relation_budget
                        ),
                        "dependents": _show_bounded_value(
                            children, relation_budget
                        ),
                        "parents": _show_bounded_value(parents, relation_budget),
                        "children": _show_bounded_value(children, relation_budget),
                        "comments": [
                            {
                                "author": _show_bounded_value(c.author, text_budget),
                                "body": _show_bounded_value(c.body, text_budget),
                                "created_at": c.created_at,
                            }
                            for c in shown_comments
                        ],
                        "comments_omitted": max(
                            0, len(comments) - len(shown_comments)
                        ),
                        "comments_total": len(comments),
                        "events": [
                            {
                                "id": e.id,
                                "kind": _show_bounded_value(e.kind, text_budget),
                                "payload": _show_bounded_value(
                                    e.payload, value_budget
                                ),
                                "created_at": e.created_at,
                                "run_id": e.run_id,
                            }
                            for e in shown_events
                        ],
                        "events_omitted": max(
                            0, len(events) - len(shown_events)
                        ),
                        "events_total": len(events),
                        "runs": [
                            _run_dict(
                                r,
                                text_budget=text_budget,
                                value_budget=value_budget,
                            )
                            for r in shown_runs
                        ],
                        "runs_omitted": max(0, len(runs) - len(shown_runs)),
                        "runs_total": len(runs),
                        "unresolved_preflight": bounded_preflight,
                        # This object is the Resolver's exact CAS contract;
                        # never pass it through a text or byte bound.
                        "expected": expected,
                        "worker_context": (
                            "Resolver view is bounded; use work_contract, comments, "
                            "runs, events, unresolved_preflight, and expected."
                        ),
                    }
                    if task.work_item_kind == "epic":
                        response["members"] = _show_bounded_value(
                            kb.list_epic_members(conn, tid), relation_budget
                        )
                        response["progress"] = _show_bounded_value(
                            kb.epic_progress(conn, tid), relation_budget
                        )
                    response["history_truncated"] = bool(
                        response["comments_omitted"]
                        or response["events_omitted"]
                        or response["runs_omitted"]
                    )
                    return response

                limits = {
                    "comment_limit": min(10, len(comments)),
                    "event_limit": min(12, len(events)),
                    "run_limit": min(6, len(runs)),
                    "text_budget": 4_096,
                    "value_budget": 2_048,
                    "field_budget": 4_096,
                    "contract_field_budget": 2_048,
                    "preflight_budget": 12_288,
                    "relation_budget": 16_384,
                }
                response = None
                for _ in range(48):
                    candidate = _build_resolver_response(**limits)
                    if _show_json_bytes(candidate) < KANBAN_SHOW_MAX_BYTES:
                        response = candidate
                        break
                    # Drop optional history first, halving each recent slice
                    # so the response converges quickly without a fixed row
                    # count pretending to be a whole-response guarantee.
                    if limits["comment_limit"]:
                        limits["comment_limit"] //= 2
                        continue
                    if limits["event_limit"]:
                        limits["event_limit"] //= 2
                        continue
                    if limits["run_limit"]:
                        limits["run_limit"] //= 2
                        continue
                    if limits["text_budget"] > 256:
                        limits["text_budget"] //= 2
                        continue
                    if limits["value_budget"] > 256:
                        limits["value_budget"] //= 2
                        continue
                    if limits["field_budget"] > 256:
                        limits["field_budget"] //= 2
                        continue
                    if limits["contract_field_budget"] > 256:
                        limits["contract_field_budget"] //= 2
                        continue
                    if limits["preflight_budget"] > 2_048:
                        limits["preflight_budget"] //= 2
                        continue
                    if limits["relation_budget"] > 512:
                        limits["relation_budget"] //= 2
                        continue
                    # All optional material is already at its minimum. This
                    # final response retains the exact snapshot and the
                    # resolver control scaffold while leaving no history rows.
                    limits.update({
                        "comment_limit": 0,
                        "event_limit": 0,
                        "run_limit": 0,
                        "text_budget": 128,
                        "value_budget": 128,
                        "field_budget": 128,
                        "contract_field_budget": 128,
                        "preflight_budget": 1_024,
                        "relation_budget": 256,
                    })
                # A normal SQLite task row cannot make the exact snapshot this
                # small response exceed the ceiling. Keep the final guard so
                # a future schema expansion fails closed instead of returning
                # an unbounded tool result.
                if response is None:
                    response = _build_resolver_response(**limits)
                if _show_json_bytes(response) >= KANBAN_SHOW_MAX_BYTES:
                    return tool_error(
                        "kanban_show: Resolver response cannot fit the safety ceiling"
                    )
            else:
                response = {
                    "task": _task_dict(task),
                    "work_contract": contract,
                    "epic": (
                        {
                            "id": epic_id,
                            "title": epic.title if epic is not None else epic_id,
                        }
                        if epic_id
                        else None
                    ),
                    "dependencies": parents,
                    "dependents": children,
                    "parents": parents,
                    "children": children,
                    "comments": [
                        {"author": c.author, "body": c.body,
                         "created_at": c.created_at}
                        for c in comments
                    ],
                    "events": [
                        {"id": e.id, "kind": e.kind, "payload": e.payload,
                         "created_at": e.created_at, "run_id": e.run_id}
                        for e in events[-50:]   # cap; full log via CLI
                    ],
                    "runs": [_run_dict(r) for r in runs],
                    # Also surface the worker's own context block so the
                    # agent can include it directly if it wants. This is
                    # the same string build_worker_context returns to the
                    # dispatcher at spawn time.
                    "worker_context": kb.build_worker_context(conn, tid),
                }
            if task.work_item_kind == "epic" and not resolver_view:
                response["members"] = kb.list_epic_members(conn, tid)
                response["progress"] = kb.epic_progress(conn, tid)
            return _show_serialized(response)
        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


@_kanban_handler("kanban_list")
def _handle_list(args: dict, **kw) -> str:
    """Task summaries with the same core filters as the CLI."""
    _require_orchestrator_tool("kanban_list")
    include_archived = _parse_bool_arg(args, "include_archived")
    limit = args.get("limit")
    try:
        limit = KANBAN_LIST_DEFAULT_LIMIT if limit is None else int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    _check(limit >= 1, "limit must be >= 1")
    _check(limit <= KANBAN_LIST_MAX_LIMIT, f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    with _board(args.get("board")) as (kb, conn):
        # Match CLI list: dependencies cleared since the last dispatcher tick
        # should be visible to orchestrators immediately.
        promoted = kb.recompute_ready(conn)
        # One extra row lets the output report truncation without dumping the board.
        rows = kb.list_tasks(
            conn, assignee=args.get("assignee"), status=args.get("status"),
            tenant=args.get("tenant"), include_archived=include_archived, limit=limit + 1)
        truncated = len(rows) > limit
        tasks = rows[:limit]
        return json.dumps({
            "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
            "count": len(tasks), "limit": limit, "truncated": truncated,
            "next_limit": (min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                           if truncated and limit < KANBAN_LIST_MAX_LIMIT else None),
            "promoted": promoted})


@_kanban_handler("kanban_complete")
def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    delegated_err = _reject_delegated_child_mutation("kanban_complete")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    for field in ("workflow_outcome",):
        value = args.get(field)
        if value is None:
            continue
        if not isinstance(value, dict):
            return tool_error(f"{field} must be an object/dict")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            return tool_error(
                f"metadata must be an object/dict, got {type(metadata).__name__}"
            )
        metadata[field] = value
    if summary:
        summary = redact_sensitive_text(str(summary), force=True)
    if result:
        result = redact_sensitive_text(str(result), force=True)
    if metadata is not None and isinstance(metadata, dict):
        meta_json = json.dumps(metadata)
        meta_json = redact_sensitive_text(meta_json, force=True)
        try:
            metadata = json.loads(meta_json)
        except json.JSONDecodeError:
            pass
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            board = kb._known_board_slug_for_connection(conn) or board
            # Goal-mode pre-completion judge gate (Issue #38367).
            # Prevent workers from bypassing the auxiliary judge by
            # calling kanban_complete before acceptance criteria are met.
            # Only enforce when a judge is actually reachable — see
            # _goal_judge_available for why an unavailable judge fails open.
            task = kb.get_task(conn, tid)
            _goal_gate("kanban_complete", task, tid, (summary or result or "").strip())

            try:
                if (
                    task
                    and task.workflow_template_id == "product"
                    and task.current_step_key == "release_measure"
                    and _product_workflow_enabled()
                    and not kb.has_unresolved_product_preflight(conn, tid)
                ):
                    release = kb.release_product_task(
                        conn,
                        tid,
                        board,
                        None,
                        None,
                        measurement_note=summary or result,
                        completion_metadata=metadata,
                        created_cards=created_cards,
                        expected_run_id=_worker_run_id(tid),
                    )
                    if not release.released:
                        return tool_error(
                            f"kanban_complete release blocked: {release.status}. "
                            "The task remains in release_measure."
                        )
                    run = kb.latest_run(conn, tid)
                    return _ok(task_id=tid, run_id=run.id if run else None)
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                    board=board,
                    product_role_assignees=_product_role_assignees_from_config(),
                    product_workflow_enabled=_product_workflow_enabled(),
                )
            except kb.ArtifactPreservationError as artifact_err:
                return tool_error(
                    f"kanban_complete could not preserve the declared artifacts: "
                    f"{artifact_err}. Your task is still in-flight and its "
                    f"scratch workspace was kept. Fix the artifact path or "
                    f"storage error, then retry kanban_complete with the same handoff."
                )
            except kb.ProductOutcomeError as outcome_err:
                qualifier = (
                    f" ({outcome_err.qualifier})"
                    if outcome_err.qualifier
                    else ""
                )
                return tool_error(
                    "kanban_complete blocked by canonical outcome validation: "
                    f"{outcome_err.code}{qualifier}. Your task is still in-flight "
                    "(no state change). Retry with a structured terminal outcome."
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            except kb.ReleaseEvidenceError as release_err:
                return tool_error(
                    "kanban_complete blocked by release evidence policy. "
                    f"Missing: {', '.join(release_err.missing)}. "
                    "The task remains in release_measure."
                )
            except kb.ProductWorkflowStateError as workflow_err:
                return tool_error(
                    "kanban_complete blocked by product workflow state: "
                    f"{workflow_err}. The task is still in-flight "
                    "(no state change)."
                )
            except kb.ProductProvenanceError as prov_err:
                missing = getattr(prov_err, "missing", None) or []
                missing_text = f" Missing: {', '.join(missing)}." if missing else ""
                return tool_error(
                    "kanban_complete blocked by product-board AI provenance "
                    f"policy for step {getattr(prov_err, 'step_key', 'unknown')}: "
                    f"{prov_err}.{missing_text} Your task is still in-flight "
                    "(no state change). Retry kanban_complete with "
                    "metadata.ai_provenance naming the AI that wrote/tested/"
                    "reviewed the work; review completions must name a "
                    "reviewer AI different from the writer AI."
                )
            if not ok:
                task = kb.get_task(conn, tid)
                if (
                    task is not None
                    and task.status == "running"
                    and task.current_step_key == "development"
                    and task.current_run_id == _worker_run_id(tid)
                ):
                    return tool_error(
                        "Development source handoff could not create the "
                        "required Git commit from the canonical workspace. "
                        "Keep source changes in HERMES_KANBAN_WORKSPACE and "
                        "retry; the task is still in-flight."
                    )
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_complete: {e}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


@_kanban_handler("kanban_block")
def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    delegated_err = _reject_delegated_child_mutation("kanban_block")
    if delegated_err:
        return delegated_err
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    reason = redact_sensitive_text(str(reason), force=True)
    kind = args.get("kind")
    attempted_resolutions_raw = args.get("attempted_resolutions")
    if attempted_resolutions_raw is not None and not isinstance(
        attempted_resolutions_raw, (str, list, tuple)
    ):
        return tool_error(
            "attempted_resolutions must be a list of short strings describing "
            "what you already tried before asking for human input"
        )
    attempted_resolutions = _normalize_attempted_resolutions(attempted_resolutions_raw)
    metadata = _stamp_worker_session_metadata(tid, None)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        if kind is not None and kind not in kb.VALID_BLOCK_KINDS:
            conn.close()
            return tool_error(
                f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)"
            )
        # Goal-mode block gate (Issue #38696, sibling of the kanban_complete
        # judge gate in #38367). kanban_block is a second exit path out of
        # the goal loop — run_kanban_goal_loop() treats ANY `blocked` status
        # as terminal, identically to `done`, regardless of kind. Without
        # this, a worker that learns kanban_complete is gated can just call
        # kanban_block(reason="anything") to escape the loop instead.
        # Restrict goal_mode tasks to the kinds that represent a genuine
        # external blocker the worker cannot resolve itself; `capability`
        # and `transient` (or an unset kind) route back through
        # kanban_complete, which the judge now gates.
        task = kb.get_task(conn, tid)
        if (
            _product_workflow_enabled()
            and kb.is_product_board(board=board)
            and kind in kb.PRODUCT_HUMAN_BLOCK_KINDS
            and not attempted_resolutions
        ):
            conn.close()
            return tool_error(
                "Product-board human-in-the-loop blocks require "
                "attempted_resolutions: list the concrete alternatives you "
                "already tried before asking Hermes/human for help. If this is "
                "only waiting on another card, use kind='dependency' instead."
            )
        if (
            task
            and task.goal_mode
            and kind not in _GOAL_MODE_BLOCK_ALLOWED_KINDS
        ):
            conn.close()
            return tool_error(
                f"goal_mode tasks can only block with kind in "
                f"{sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} (got {kind!r}). "
                f"If the task is actually finished or cannot proceed for "
                f"another reason, call kanban_complete instead — the "
                f"completion judge will evaluate it."
            )
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                kind=kind,
                attempted_resolutions=attempted_resolutions,
                metadata=metadata,
                expected_run_id=_worker_run_id(tid),
                board=board,
                human_escalation_assignee=_product_human_escalation_profile(
                    board, conn=conn
                ),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            # Tell the worker where the task actually landed so it doesn't
            # assume it's sitting in 'blocked' when routing sent it elsewhere.
            landed = kb.get_task(conn, tid)
            slack_subscribed = False
            if (
                landed
                and landed.status == "blocked"
                and _product_workflow_enabled()
                and kind in kb.PRODUCT_HUMAN_BLOCK_KINDS
            ):
                slack_subscribed = _maybe_subscribe_slack_on_product_human_block(
                    kb, conn, tid, board=board
                )
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "blocked",
                block_kind=kind,
                slack_subscribed=slack_subscribed,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


@_kanban_handler("kanban_request_review")
def _handle_request_review(args: dict, **kw) -> str:
    """Move implementation into the first-class review phase."""
    tid = _worker_guard("kanban_request_review", args)
    summary = _redact(_require_text(
        args, "summary", "summary is required — describe what was implemented and how it "
        "was verified so the reviewer has context"))
    metadata = args.get("metadata")
    _require_dict_metadata(metadata)
    if metadata is not None:
        metadata = _redact_metadata(metadata)
        _check(metadata is not None, "metadata could not be safely serialized")
    metadata = _stamp_worker_session_metadata(tid, metadata)
    # Reviewer is model-supplied free text stored durably on the event payload.
    reviewer = _redact_opt(args.get("reviewer") or None)
    with _board(args.get("board")) as (kb, conn):
        _goal_gate("kanban_request_review", kb.get_task(conn, tid), tid, summary)
        ok, fail_reason = kb.request_review(
            conn, tid, summary=summary, metadata=metadata, reviewer=reviewer,
            expected_run_id=_worker_run_id(tid), with_reason=True)
        _check(ok, f"could not request review for {tid}: "
                   f"{fail_reason or 'unknown id or not in running/ready'}")
        return _ok_landed(kb, conn, tid, "review")


@_kanban_handler("kanban_request_changes")
def _handle_request_changes(args: dict, **kw) -> str:
    """Return a reviewer-owned running task to its implementer."""
    tid = _worker_guard("kanban_request_changes", args)
    reason = _redact(
        _require_text(args, "reason", "reason is required — describe the changes needed"))
    with _board(args.get("board")) as (kb, conn):
        ok, detail = kb.request_changes(
            conn, tid, reason=reason, expected_run_id=_worker_run_id(tid))
        _check(ok, f"could not request changes for {tid}: {detail or 'invalid review state'}")
        return _ok_landed(kb, conn, tid, "ready", implementer=detail)


@_kanban_handler("kanban_heartbeat")
def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal liveness: extend the claim TTL AND record a heartbeat event.
    Without the claim half, a worker blocked in one long tool call would still
    be reclaimed by ``release_stale_claims``."""
    tid = _worker_guard("kanban_heartbeat", args)
    from hermes_cli import kanban_db_dispatch as kbd
    with _board(args.get("board")) as (kb, conn):
        # The dispatcher pins HERMES_KANBAN_CLAIM_LOCK at spawn; the default
        # claimer covers locally-driven workers that bypassed the dispatcher.
        kb.heartbeat_claim(conn, tid, claimer=os.environ.get("HERMES_KANBAN_CLAIM_LOCK"))
        ok = kbd.heartbeat_worker(
            conn, tid, note=args.get("note"), expected_run_id=_worker_run_id(tid))
        _check(ok, f"could not heartbeat {tid} (unknown id or not running)")
        return _ok(task_id=tid)


@_kanban_handler("kanban_comment")
def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    _reject_delegated_child_mutation("kanban_comment")
    tid = args.get("task_id")
    _check(tid, "task_id is required (use the current task id if that's what "
                "you mean — pulls from env but kept explicit here)")
    body = _redact(_require_text(args, "body"))
    # Author comes from the worker's runtime identity, never caller args: comments are
    # injected into future workers' system prompts, so an args["author"] override could
    # forge a directive from ``hermes-system``. Cross-task commenting stays unrestricted —
    # it is the handoff channel between tasks.
    # Comments are injected into the next worker's system prompt by ``build_worker_context`` as
    # ``**{author}** (timestamp): {body}`` — accepting an ``args["author"]`` override let a worker forge a
    # comment from an authoritative-looking name like ``hermes-system`` and poison the future-worker context
    # with what reads as a system directive. See #19713.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    with _board(args.get("board")) as (kb, conn):
        cid = kb.add_comment(conn, tid, author=author, body=str(body))
        return _ok(task_id=tid, comment_id=cid)


def _store_attachment(board, tid, filename, data, content_type) -> str:
    """Store via ``kanban_db.store_attachment_bytes`` (shared size cap, per-task
    dir, metadata row) so agent, dashboard, and CLI surfaces stay in lockstep."""
    with _board(board) as (kb, conn):
        att_id = kb.store_attachment_bytes(
            conn, tid, str(filename), data,
            content_type=content_type, uploaded_by="agent", board=board)
        return _ok(task_id=tid, attachment_id=att_id, size=len(data))


@_kanban_handler("kanban_attach")
def _handle_attach(args: dict, **kw) -> str:
    """Attach an inline (base64) file to a task."""
    tid = _worker_guard("kanban_attach", args)
    filename = _require_text(args, "filename")
    content_b64 = _require_text(args, "content_base64")
    import base64
    import binascii
    try:
        data = base64.b64decode(str(content_b64), validate=True)
    except (binascii.Error, ValueError) as e:
        raise _Reject(f"content_base64 is not valid base64: {e}")
    return _store_attachment(args.get("board"), tid, filename, data, args.get("content_type"))


_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) capped at ``max_bytes`` -> ``(data, content_type)``.
    Every hop is SSRF-checked (redirects followed manually) so a model-controlled URL, or a
    public host 302ing, cannot reach loopback/private/cloud-metadata ranges. ``ValueError``
    for bad scheme, blocked target, too many redirects, or a body over the cap (checked
    while streaming, so nothing oversize is buffered)."""
    from urllib.parse import urljoin, urlparse
    import httpx
    from tools.url_safety import is_safe_url
    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme {scheme!r}; only http/https are allowed")
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}")
        chunks: list[bytes] = []
        total = 0
        with httpx.stream("GET", current_url, headers={"User-Agent": "hermes-kanban/attach"},
                          timeout=30, follow_redirects=False) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError(f"redirect without Location header from {current_url}")
                current_url = urljoin(current_url, location)
                continue
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            for chunk in resp.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")


@_kanban_handler("kanban_attach_url")
def _handle_attach_url(args: dict, **kw) -> str:
    """Attach a file fetched server-side from an http(s) URL (shared size cap)."""
    from hermes_cli import kanban_db as kb
    tid = _worker_guard("kanban_attach_url", args)
    url = str(_require_text(args, "url")).strip()
    filename = args.get("filename") or args.get("title")
    if not filename or not str(filename).strip():
        # Derive a name from the URL path's leaf component.
        from urllib.parse import unquote, urlparse
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip() or "download"
    try:
        data, fetched_ct = _download_url_with_cap(url, kb.KANBAN_ATTACHMENT_MAX_BYTES)
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url download failed")
        return tool_error(f"kanban_attach_url: failed to fetch {url}: {e}")
    return _store_attachment(
        args.get("board"), tid, filename, data, args.get("content_type") or fetched_ct)


@_kanban_handler("kanban_attachments")
def _handle_attachments(args: dict, **kw) -> str:
    """List a task's attachments (read-only; no ownership restriction)."""
    tid = _require_task_id(args)
    with _board(args.get("board")) as (kb, conn):
        _existing_task(kb, conn, tid)
        return json.dumps({
            "ok": True, "task_id": tid,
            "attachments": [
                _fields(a, _ATTACHMENT_FIELDS) for a in kb.list_attachments(conn, tid)]})


@_kanban_handler("kanban_create")
def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    delegated_err = _reject_delegated_child_mutation("kanban_create")
    if delegated_err:
        return delegated_err
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    # Prefer the request-scoped api_server origin binding: HERMES_SESSION_ID
    # is clobbered with a subagent's internal id whenever a child agent is
    # constructed in-process (agent_init calls set_current_session_id), which
    # would stamp — and later wake — the wrong session.
    from tools.async_delegation import _current_origin_session_id

    session_id = (
        args.get("session_id")
        or _current_origin_session_id()
        or os.environ.get("HERMES_SESSION_ID")
    )
    priority = args.get("priority")
    # Resolve workspace. Workspace sharing is always explicit: omitted fields
    # mean a fresh scratch workspace, even when a dispatcher-spawned worker
    # creates the task. Reusing a parent's literal path would let a child
    # mutate review evidence or race the parent's checkout (#67567).
    #
    # Project identity is the one safe context to inherit implicitly. The DB
    # resolves a project-linked scratch request into a fresh per-task worktree,
    # preserving the repository/branch convention without sharing a checkout.
    workspace_kind = args.get("workspace_kind")
    workspace_path = args.get("workspace_path")
    project_id = args.get("project") or args.get("project_id")
    workflow_template_id = args.get("workflow_template_id")
    current_step_key = args.get("current_step_key") or args.get("step_key")
    source_policy = args.get("source_policy") or "none"
    if source_policy not in {"none", "required", "forbidden"}:
        return tool_error("source_policy must be one of: none, required, forbidden")
    project_source_task_id = None
    _inherit_project = workspace_kind is None and workspace_path is None
    if workspace_kind is None:
        workspace_kind = "scratch"
    triage = _parse_bool_arg(args, "triage")
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    initial_status = args.get("initial_status") or "running"
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    goal_mode = _parse_bool_arg(args, "goal_mode")
    goal_max_turns = args.get("goal_max_turns")
    model_override = args.get("model")
    provider_override = args.get("provider")
    if provider_override and not model_override:
        return tool_error("'provider' requires 'model' to be set as well")
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            from hermes_cli import kanban_intake

            metadata = kb.read_board_metadata(
                board or kb._board_slug_for_connection(conn)
            )
            if kanban_intake.qualification_required(metadata):
                if source_policy != "none":
                    return tool_error(
                        "non-none source policy is a Default-board execution contract"
                    )
                receipt = kanban_intake.submit_intake(
                    conn,
                    request={
                        "title": str(title).strip(),
                        "body": body,
                        "assignee": str(assignee),
                        "parents": list(parents),
                        "tenant": tenant,
                        "priority": int(priority) if priority is not None else 0,
                        "workspace_kind": str(workspace_kind),
                        "workspace_path": workspace_path,
                        "project_id": project_id,
                        "triage": triage,
                        "idempotency_key": idempotency_key,
                        "max_runtime_seconds": max_runtime_seconds,
                        "skills": list(skills) if skills is not None else [],
                        "goal_mode": goal_mode,
                        "goal_max_turns": goal_max_turns,
                        "initial_status": str(initial_status),
                        "workflow_template_id": workflow_template_id,
                        "current_step_key": current_step_key,
                    },
                    source="worker",
                    session_id=session_id,
                )
                return _ok(**receipt)
            # A project link is safe to inherit because ``create_task`` turns
            # it into a fresh per-task worktree. Never inherit the parent's
            # literal workspace kind/path; directory sharing must be explicit.
            if _inherit_project and project_id is None:
                _self_tid = os.environ.get("HERMES_KANBAN_TASK")
                if _self_tid:
                    _self_task = kb.get_task(conn, _self_tid)
                    if _self_task is not None and _self_task.project_id:
                        project_id = _self_task.project_id
                        project_source_task_id = _self_task.id
                        parent_is_product = (
                            _self_task.workflow_template_id == "product"
                            or bool(_self_task.current_step_key)
                        )
                        if parent_is_product:
                            if workflow_template_id is None:
                                workflow_template_id = "product"
                            if current_step_key is None:
                                current_step_key = "backlog"
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                project_id=project_id,
                project_source_task_id=project_source_task_id,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                model_override=model_override,
                provider_override=provider_override,
                goal_mode=goal_mode,
                goal_max_turns=(
                    int(goal_max_turns) if goal_max_turns is not None else None
                ),
                completion_contract=args.get("completion_contract"),
                initial_status=str(initial_status),
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
                board=board,
                workflow_template_id=workflow_template_id,
                current_step_key=current_step_key,
                source_commit_required=source_policy == "required",
                source_commit_forbidden=source_policy == "forbidden",
            )
            new_task = kb.get_task(conn, new_tid)
            subscribed = _maybe_auto_subscribe(conn, new_tid)
            return _ok(
                task_id=new_tid,
                status=new_task.status if new_task else None,
                workspace_kind=new_task.workspace_kind if new_task else None,
                workspace_path=new_task.workspace_path if new_task else None,
                project_id=new_task.project_id if new_task else None,
                subscribed=subscribed,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _resolve_notify_target() -> Optional[dict[str, Any]]:
    """``kanban_db.add_notify_sub`` kwargs for the calling session, or None (CLI/cron/tests).
    Gateway sessions: ``HERMES_SESSION_PLATFORM``/``CHAT_ID`` ContextVars. TUI/desktop:
    those are cleared but the subprocess inherits ``HERMES_SESSION_KEY`` -> ``platform="tui"``
    for the TUI poller. ``HERMES_SESSION_ID`` is deliberately NOT a fallback: it is set for
    every CLI/ACP invocation and would auto-subscribe every CLI run."""
    from gateway.session_context import get_session_env as env
    platform, chat_id = env("HERMES_SESSION_PLATFORM", ""), env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id:
        session_key = env("HERMES_SESSION_KEY", "") or os.environ.get("HERMES_SESSION_KEY", "")
        if not session_key:
            return None
        platform, chat_id = "tui", session_key
    chat_type = env("HERMES_SESSION_CHAT_TYPE", "") or None
    thread_id = env("HERMES_SESSION_THREAD_ID", "") or None
    message_id = env("HERMES_SESSION_MESSAGE_ID", "") or ""
    notifier_profile = env("HERMES_SESSION_PROFILE", "") or os.environ.get("HERMES_PROFILE")
    if not notifier_profile:
        try:
            from hermes_cli.profiles import get_active_profile_name
            notifier_profile = get_active_profile_name() or "default"
        except Exception:
            notifier_profile = "default"
    delivery_metadata: dict[str, Any] = {
        k: v for k, v in (("thread_id", thread_id), ("chat_type", chat_type)) if v}
    if (platform.lower() == "telegram" and thread_id
            and (chat_type or "").lower() in {"dm", "direct", "private"}):
        delivery_metadata["telegram_dm_topic_reply_fallback"] = True
        if str(thread_id) not in {"", "1"}:
            delivery_metadata["direct_messages_topic_id"] = str(thread_id)
        if message_id:
            delivery_metadata["telegram_reply_to_message_id"] = str(message_id)
    return dict(
        platform=platform, chat_id=chat_id, chat_type=chat_type, thread_id=thread_id,
        user_id=env("HERMES_SESSION_USER_ID", "") or None,
        user_id_alt=env("HERMES_SESSION_USER_ID_ALT", "") or None,
        notifier_profile=notifier_profile,
        delivery_mode="notify+wake" if platform != "tui" else None,
        delivery_metadata=delivery_metadata or None)


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Subscribe the calling session to completion/block events; True iff a row was
    written (surfaced as ``subscribed`` so an orchestrator can fall back to explicit
    ``kanban_notify-subscribe``). Gated by ``kanban.auto_subscribe_on_create`` (default
    True). Failures are logged and swallowed: bookkeeping must never fail kanban_create."""
    try:
        if not cfg_get(load_config(), "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        pass  # unreadable config keeps the user-friendly default (True)
    target = None
    try:
        target = _resolve_notify_target()
        if target is None:
            return False  # CLI / cron / test — no persistent channel
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_notify as _kbn
        _kbn.add_notify_sub(conn, task_id=task_id, **target)
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, target["platform"] if target else "", bool(target and target["chat_id"]))
        return False


@_kanban_handler("kanban_unblock")
def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task to ready, or todo while parents remain open."""
    _reject_delegated_child_mutation("kanban_unblock")
    _require_orchestrator_tool("kanban_unblock")
    tid = args.get("task_id")
    _check(tid, "task_id is required")
    tid = str(tid)
    _enforce_worker_task_ownership(tid)
    with _board(args.get("board")) as (kb, conn):
        _check(kb.unblock_task(conn, tid), f"could not unblock {tid} (not blocked or unknown)")
        return _ok(task_id=tid, **_fields(kb.get_task(conn, tid), ("status",)))


@_kanban_handler("kanban_link")
def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    delegated_err = _reject_delegated_child_mutation("kanban_link")
    if delegated_err:
        return delegated_err
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            from hermes_cli import kanban_intake

            if kanban_intake.qualification_required(
                kb.read_board_metadata(board or kb._board_slug_for_connection(conn))
            ):
                return tool_error(
                    "kanban_link: strict-board dependencies are owned by the Work Contract"
                )
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


def _product_workflow_cfg() -> dict:
    try:
        cfg = load_config()
        raw = cfg_get(cfg, "kanban", "product_workflow", default={})
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _product_workflow_enabled() -> bool:
    cfg = _product_workflow_cfg()
    return cfg.get("enabled", True) is not False


def _product_role_assignees_from_config() -> dict[str, str]:
    cfg = _product_workflow_cfg()
    raw = cfg.get("assignees") if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if str(v).strip()}


def _product_human_escalation_profile(
    board: Optional[str] = None,
    *,
    conn=None,
) -> str:
    """Prefer the connected product board's policy over local config."""
    try:
        from hermes_cli import kanban_db as kb

        active_board = board
        if active_board is None and conn is not None:
            active_board = kb._board_slug_for_connection(conn)
        if active_board is None:
            active_board = os.environ.get("HERMES_KANBAN_BOARD")
        meta = kb.product_board_metadata(active_board)
        workflow = meta.get("product_workflow") if isinstance(meta, dict) else None
        if isinstance(workflow, dict):
            profile = str(workflow.get("human_escalation_profile") or "").strip()
            if profile:
                return profile
    except Exception:
        logger.debug("could not read product-board escalation profile", exc_info=True)
    cfg = _product_workflow_cfg()
    return str(cfg.get("human_escalation_profile") or "default").strip() or "default"


def _normalize_attempted_resolutions(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _slack_escalation_channel_from_config() -> Optional[str]:
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    channel = cfg_get(
        cfg,
        "kanban", "product_workflow", "slack_escalation_channel",
        default="",
    )
    if channel:
        return str(channel).strip() or None
    for path in (
        ("gateway", "platforms", "slack", "home_channel"),
        ("platforms", "slack", "home_channel"),
        ("slack", "home_channel"),
    ):
        value = cfg_get(cfg, *path, default="")
        if value:
            return str(value).strip() or None
    allowed = cfg_get(cfg, "slack", "allowed_channels", default="")
    if isinstance(allowed, str):
        for item in allowed.split(","):
            item = item.strip()
            if item:
                return item
    return None


def _maybe_subscribe_slack_on_product_human_block(
    kb: Any,
    conn: Any,
    task_id: str,
    *,
    board: Optional[str] = None,
) -> bool:
    try:
        cfg = load_config()
        if cfg_get(
            cfg,
            "kanban", "product_workflow", "auto_subscribe_slack_on_human_block",
            default=True,
        ) is False:
            return False
        if not kb.is_product_board(board=board):
            return False
        channel = _slack_escalation_channel_from_config()
        if not channel:
            return False
        from hermes_cli import kanban_db_notify as kbn
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="slack",
            chat_id=channel,
            thread_id="",
            notifier_profile=os.environ.get("HERMES_PROFILE") or "default",
        )
        return True
    except Exception:
        logger.warning("slack auto-subscribe for product human block failed", exc_info=True)
        return False


def _require_configured_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Re-check configured orchestrator authority at execution time."""
    if _check_kanban_orchestrator_mode():
        return None
    return tool_error(
        f"{tool_name} requires a configured Kanban orchestrator profile "
        "outside dispatcher-worker and delegated-child contexts."
    )


def _review_target_git(workspace: Path, *args: str) -> str:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise ValueError("git executable is unavailable")
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
        raise ValueError(f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown git error").strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail[:300]}")
    return result.stdout or ""


def _bounded_review_diff_page(
    diff: str,
    offset: int,
) -> tuple[str, Optional[int], bool, list[int]]:
    """Return a bounded page where offsets address original diff lines."""
    lines = diff.splitlines(keepends=True)
    if offset > len(lines):
        raise ValueError("offset exceeds the pinned diff")

    page: list[str] = []
    truncated_lines: list[int] = []
    total_chars = 0
    line_index = offset
    line_limit = max(
        1,
        min(REVIEW_TARGET_MAX_LINE_CHARS, REVIEW_TARGET_PAGE_CHARS),
    )
    while (
        line_index < len(lines)
        and line_index - offset < REVIEW_TARGET_PAGE_LINES
    ):
        original = lines[line_index]
        rendered = original
        was_truncated = len(original) > line_limit
        if was_truncated:
            newline = "\n" if original.endswith("\n") else ""
            marker = (
                f"... [line {line_index} truncated from "
                f"{len(original)} chars]{newline}"
            )
            if len(marker) >= line_limit:
                rendered = marker[:line_limit]
            else:
                rendered = original[: line_limit - len(marker)] + marker

        if page and total_chars + len(rendered) > REVIEW_TARGET_PAGE_CHARS:
            break
        page.append(rendered)
        total_chars += len(rendered)
        if was_truncated:
            truncated_lines.append(line_index)
        line_index += 1

    complete = line_index >= len(lines)
    return (
        "".join(page),
        None if complete else line_index,
        complete,
        truncated_lines,
    )


@_kanban_handler('review_target')
def _handle_review_target(args: dict, **kw) -> str:
    """Return a bounded diff page for the current run's pinned commits."""
    if not _check_reviewer_mode():
        return tool_error(
            "review_target is restricted to a task-scoped reviewer profile"
        )
    offset = args.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return tool_error("review_target: offset must be a non-negative integer")
    task_id = os.environ.get("HERMES_KANBAN_TASK") or ""
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID") or ""
    try:
        run_id = int(raw_run_id)
    except ValueError:
        return tool_error("review_target: current reviewer run is missing")

    try:
        kb, conn = _connect()
        try:
            task = kb.get_task(conn, task_id)
            run = kb.get_run(conn, run_id)
            metadata = run.metadata if run and isinstance(run.metadata, dict) else {}
            review_shape = (
                (task.workflow_template_id, task.current_step_key, run.step_key)
                if task is not None and run is not None else None
            )
            product_review = review_shape == ("product", "review", "review")
            default_review = (
                review_shape in {(None, None, None), (None, "review", "review")}
                and task.assignee == "reviewer"
                and task.source_commit_forbidden
                and task.branch_name
                and metadata.get("review_branch") == task.branch_name
                and metadata.get("review_contract_kind") == "default"
            )
            if (
                task is None
                or task.current_run_id != run_id
                or not (product_review or default_review)
            ):
                return tool_error(
                    "review_target: task is not owned by the current reviewer run"
                )
            if (
                run is None
                or run.task_id != task_id
                or run.profile != "reviewer"
                or run.ended_at is not None
                or run.status != "running"
            ):
                return tool_error(
                    "review_target: task is not owned by the current reviewer run"
                )
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            if (
                not claim_lock
                or task.claim_lock != claim_lock
                or run.claim_lock != claim_lock
            ):
                return tool_error(
                    "review_target: task is not owned by the current reviewer claim"
                )
            base_sha = metadata.get("review_base_sha")
            head_sha = metadata.get("review_head_sha")
            if (
                not isinstance(base_sha, str)
                or not _FULL_GIT_SHA_RE.fullmatch(base_sha)
                or not isinstance(head_sha, str)
                or not _FULL_GIT_SHA_RE.fullmatch(head_sha)
            ):
                return tool_error(
                    "review_target: active run has no valid pinned review commits"
                )
            if not task.workspace_path:
                return tool_error("review_target: task workspace is missing")
            workspace = Path(task.workspace_path).expanduser().resolve(strict=True)
            if not workspace.is_dir():
                return tool_error("review_target: task workspace is not a directory")
            repo_root = Path(
                _review_target_git(
                    workspace, "rev-parse", "--show-toplevel"
                ).strip()
            ).resolve(strict=True)
            if repo_root != workspace:
                return tool_error(
                    "review_target: repository root does not match task workspace"
                )
            _review_target_git(workspace, "cat-file", "-e", f"{base_sha}^{{commit}}")
            _review_target_git(workspace, "cat-file", "-e", f"{head_sha}^{{commit}}")
            all_changed_files = [
                line
                for line in _review_target_git(
                    workspace,
                    "diff",
                    "--name-only",
                    base_sha,
                    head_sha,
                    "--",
                    ".",
                ).splitlines()
                if line
            ]
            all_binary_files = []
            for line in _review_target_git(
                workspace,
                "diff",
                "--numstat",
                base_sha,
                head_sha,
                "--",
                ".",
            ).splitlines():
                fields = line.split("\t", 2)
                if len(fields) == 3 and fields[:2] == ["-", "-"]:
                    all_binary_files.append(fields[2])
            changed_files = all_changed_files[:REVIEW_TARGET_FILE_LIST_LIMIT]
            binary_files = all_binary_files[:REVIEW_TARGET_FILE_LIST_LIMIT]
            diff = _review_target_git(
                workspace,
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--unified=3",
                base_sha,
                head_sha,
                "--",
                ".",
            )
            diff_page, next_offset, complete, truncated_lines = (
                _bounded_review_diff_page(diff, offset)
            )
            return json.dumps(
                {
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "changed_files": changed_files,
                    "changed_files_omitted": (
                        len(all_changed_files) - len(changed_files)
                    ),
                    "binary_files": binary_files,
                    "binary_files_omitted": (
                        len(all_binary_files) - len(binary_files)
                    ),
                    "diff": diff_page,
                    "truncated_lines": truncated_lines,
                    "next_offset": next_offset,
                    "complete": complete,
                }
            )
        finally:
            conn.close()
    except (OSError, ValueError) as exc:
        return tool_error(f"review_target: {exc}")
    except Exception as exc:
        logger.exception("review_target failed")
        return tool_error(f"review_target: {exc}")


@_kanban_handler('kanban_resolve')
def _handle_resolve(args: dict, **kw) -> str:
    """Apply one audited Resolver decision to the current preflight."""
    from hermes_cli import kanban_db as kb_module

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    resolver_profile = os.environ.get("HERMES_PROFILE") or ""
    if resolver_profile != "resolver":
        return tool_error("kanban_resolve is restricted to the resolver profile")

    allowed_fields = {
        "task_id", "board", "decision", "fault_domain", "diagnosis",
        "reason", "expected", "repair",
    }
    unexpected = sorted(set(args) - allowed_fields)
    if unexpected:
        return tool_error(
            "kanban_resolve: unexpected fields: " + ", ".join(unexpected)
        )

    board = args.get("board")
    request_fields = (
        "decision", "fault_domain", "diagnosis", "reason", "expected",
        "repair",
    )
    request = {field: args[field] for field in request_fields if field in args}
    resolver_model = (
        os.environ.get("HERMES_INFERENCE_MODEL")
        or os.environ.get("HERMES_MODEL")
        or None
    )
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.resolve_product_preflight(
                conn,
                tid,
                board=board or os.environ.get("HERMES_KANBAN_BOARD"),
                request=request,
                resolver_profile=resolver_profile,
                resolver_model=resolver_model,
            )
            if not ok:
                return tool_error(f"could not resolve preflight for {tid}")
            return _ok(task_id=tid, decision=request.get("decision"))
        finally:
            conn.close()
    except kb_module.TaskSnapshotConflict:
        return tool_error(
            "kanban_resolve conflict: task changed; refresh with kanban_show"
        )
    except ValueError as e:
        return tool_error(f"kanban_resolve: {e}")
    except Exception as e:
        logger.exception("kanban_resolve failed")
        return tool_error(f"kanban_resolve: {e}")


@_kanban_handler('work_inbox_show')
def _handle_work_inbox_show(args: dict, **kw) -> str:
    try:
        from hermes_cli import kanban_po_intake

        kb, conn = _connect(board=os.environ.get("HERMES_KANBAN_BOARD"))
        try:
            return json.dumps(
                kanban_po_intake.show_product_owner_intake(
                    conn, board=os.environ["HERMES_KANBAN_BOARD"]
                ),
                ensure_ascii=False,
                default=str,
            )
        finally:
            conn.close()
    except Exception as exc:
        return tool_error(f"work_inbox_show: {exc}")


@_kanban_handler('work_inbox_heartbeat')
def _handle_work_inbox_heartbeat(args: dict, **kw) -> str:
    try:
        from hermes_cli import kanban_po_intake

        kb, conn = _connect(board=os.environ.get("HERMES_KANBAN_BOARD"))
        try:
            return json.dumps(
                kanban_po_intake.heartbeat_product_owner_intake(
                    conn, note=args.get("note")
                )
            )
        finally:
            conn.close()
    except Exception as exc:
        return tool_error(f"work_inbox_heartbeat: {exc}")


@_kanban_handler('work_inbox_decide')
def _handle_work_inbox_decide(args: dict, **kw) -> str:
    try:
        from hermes_cli import kanban_po_intake

        kb, conn = _connect(board=os.environ.get("HERMES_KANBAN_BOARD"))
        try:
            return json.dumps(
                kanban_po_intake.decide_product_owner_intake(
                    conn,
                    board=os.environ["HERMES_KANBAN_BOARD"],
                    disposition=args.get("disposition"),
                    reason=args.get("reason"),
                    proposal=args.get("proposal"),
                    question=args.get("question"),
                ),
                ensure_ascii=False,
            )
        finally:
            conn.close()
    except Exception as exc:
        return tool_error(f"work_inbox_decide: {exc}")


@_kanban_handler('kanban_configure')
def _handle_configure(args: dict, **kw) -> str:
    """CAS-replace an eligible existing card's four execution fields."""
    from hermes_cli import kanban_db as kb_module

    delegated_err = _reject_delegated_child_mutation("kanban_configure")
    if delegated_err:
        return delegated_err
    guard = _require_configured_orchestrator_tool("kanban_configure")
    if guard:
        return guard

    required = {
        "task_id",
        "source_policy",
        "max_retries",
        "max_runtime_seconds",
        "goal_mode",
        "expected",
    }
    missing = sorted(required - set(args))
    if missing:
        return tool_error(
            f"kanban_configure: missing required argument(s): {', '.join(missing)}"
        )
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return tool_error("kanban_configure: task_id must be a non-empty string")
    board = args.get("board")

    try:
        kb, conn = _connect(board=board)
        try:
            from hermes_cli import kanban_intake

            active_board = board or kb._board_slug_for_connection(conn)
            if kanban_intake.qualification_required(
                kb.read_board_metadata(active_board)
            ):
                return tool_error(
                    "kanban_configure: strict-board execution configuration "
                    "is owned by the Work Contract"
                )
            if active_board != kb.DEFAULT_BOARD:
                return tool_error(
                    "kanban_configure: execution contracts can only be changed "
                    "on the Default board"
                )
            ok = kb.configure_task(
                conn,
                task_id.strip(),
                expected=args["expected"],
                source_policy=args["source_policy"],
                max_retries=args["max_retries"],
                max_runtime_seconds=args["max_runtime_seconds"],
                goal_mode=args["goal_mode"],
            )
            if not ok:
                return tool_error(f"kanban_configure: task {task_id} not found")
            task = kb.get_task(conn, task_id.strip())
            if task is None:
                return tool_error(
                    f"kanban_configure: task {task_id} disappeared after configuration"
                )
            return _ok(task_id=task.id, **kb.task_execution_contract(task))
        finally:
            conn.close()
    except kb_module.TaskSnapshotConflict:
        return tool_error(
            "kanban_configure conflict: task changed; refresh with kanban_show"
        )
    except (ValueError, RuntimeError) as exc:
        return tool_error(f"kanban_configure: {exc}")
    except Exception as exc:
        logger.exception("kanban_configure failed")
        return tool_error(f"kanban_configure: {exc}")


@_kanban_handler('kanban_unlink')
def _handle_unlink(args: dict, **kw) -> str:
    """CAS-remove one exact Default-board parent→child edge."""
    from hermes_cli import kanban_db as kb_module

    delegated_err = _reject_delegated_child_mutation("kanban_unlink")
    if delegated_err:
        return delegated_err
    guard = _require_configured_orchestrator_tool("kanban_unlink")
    if guard:
        return guard

    allowed = {"parent_id", "child_id", "expected", "board"}
    required = {"parent_id", "child_id", "expected"}
    missing = sorted(required - set(args))
    extra = sorted(set(args) - allowed)
    if missing:
        return tool_error(
            f"kanban_unlink: missing required argument(s): {', '.join(missing)}"
        )
    if extra:
        return tool_error(
            f"kanban_unlink: unsupported argument(s): {', '.join(extra)}"
        )
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not isinstance(parent_id, str) or not parent_id.strip():
        return tool_error("kanban_unlink: parent_id must be a non-empty string")
    if not isinstance(child_id, str) or not child_id.strip():
        return tool_error("kanban_unlink: child_id must be a non-empty string")
    board = args.get("board")

    try:
        kb, conn = _connect(board=board)
        try:
            from hermes_cli import kanban_intake

            active_board = kb._board_slug_for_connection(conn)
            if kanban_intake.qualification_required(
                kb.read_board_metadata(active_board)
            ):
                return tool_error(
                    "kanban_unlink: strict-board dependencies are owned by the Work Contract"
                )
            if active_board != kb.DEFAULT_BOARD:
                return tool_error(
                    "kanban_unlink: dependency edges can only be changed on the Default board"
                )
            removed = kb.unlink_tasks(
                conn,
                parent_id.strip(),
                child_id.strip(),
                expected=args["expected"],
            )
            if not removed:
                return tool_error(
                    f"kanban_unlink: edge {parent_id} -> {child_id} was not found"
                )
            child = kb.get_task(conn, child_id.strip())
            if child is None:
                return tool_error(
                    f"kanban_unlink: child task {child_id} disappeared after unlink"
                )
            return _ok(
                parent_id=parent_id.strip(),
                child_id=child_id.strip(),
                removed=True,
                status=child.status,
            )
        finally:
            conn.close()
    except kb_module.TaskSnapshotConflict:
        return tool_error(
            "kanban_unlink conflict: task changed; refresh with kanban_show"
        )
    except (ValueError, RuntimeError) as exc:
        return tool_error(f"kanban_unlink: {exc}")
    except Exception as exc:
        logger.exception("kanban_unlink failed")
        return tool_error(f"kanban_unlink: {exc}")


# --- Registration (the fork's capability gates remain authoritative) ---
_TOOLS = (
    ('work_inbox_show', WORK_INBOX_SHOW_SCHEMA, _handle_work_inbox_show, '📥', _check_work_inbox_mode),
    ('work_inbox_decide', WORK_INBOX_DECIDE_SCHEMA, _handle_work_inbox_decide, '✅', _check_work_inbox_mode),
    ('work_inbox_heartbeat', WORK_INBOX_HEARTBEAT_SCHEMA, _handle_work_inbox_heartbeat, '💓', _check_work_inbox_mode),
    ('kanban_show', KANBAN_SHOW_SCHEMA, _handle_show, '📋', _check_kanban_mode),
    ('review_target', REVIEW_TARGET_SCHEMA, _handle_review_target, '🔎', _check_reviewer_mode),
    ('kanban_list', KANBAN_LIST_SCHEMA, _handle_list, '📋', _check_kanban_orchestrator_mode),
    ('kanban_complete', KANBAN_COMPLETE_SCHEMA, _handle_complete, '✔', _check_ordinary_worker_mode),
    ('kanban_resolve', KANBAN_RESOLVE_SCHEMA, _handle_resolve, '🧭', _check_resolver_mode),
    ('kanban_block', KANBAN_BLOCK_SCHEMA, _handle_block, '⏸', _check_ordinary_worker_mode),
    ('kanban_request_review', KANBAN_REQUEST_REVIEW_SCHEMA, _handle_request_review, '👀', _check_ordinary_worker_mode),
    ('kanban_request_changes', KANBAN_REQUEST_CHANGES_SCHEMA, _handle_request_changes, '↩', _check_ordinary_worker_mode),
    ('kanban_heartbeat', KANBAN_HEARTBEAT_SCHEMA, _handle_heartbeat, '💓', _check_kanban_mode),
    ('kanban_comment', KANBAN_COMMENT_SCHEMA, _handle_comment, '💬', _check_kanban_mode),
    ('kanban_attach', KANBAN_ATTACH_SCHEMA, _handle_attach, '📎', _check_ordinary_worker_mode),
    ('kanban_attach_url', KANBAN_ATTACH_URL_SCHEMA, _handle_attach_url, '📎', _check_ordinary_worker_mode),
    ('kanban_attachments', KANBAN_ATTACHMENTS_SCHEMA, _handle_attachments, '📎', _check_ordinary_worker_mode),
    ('kanban_create', KANBAN_CREATE_SCHEMA, _handle_create, '➕', _check_ordinary_worker_mode),
    ('kanban_unblock', KANBAN_UNBLOCK_SCHEMA, _handle_unblock, '▶', _check_kanban_orchestrator_mode),
    ('kanban_configure', KANBAN_CONFIGURE_SCHEMA, _handle_configure, '⚙', _check_kanban_orchestrator_mode),
    ('kanban_unlink', KANBAN_UNLINK_SCHEMA, _handle_unlink, '🔓', _check_kanban_orchestrator_mode),
    ('kanban_link', KANBAN_LINK_SCHEMA, _handle_link, '🔗', _check_ordinary_worker_mode),
)

for _name, _sch, _handler, _emoji, _gate in _TOOLS:
    registry.register(name=_name, toolset="kanban", schema=_sch, handler=_handler,
                      emoji=_emoji, check_fn=_gate)
