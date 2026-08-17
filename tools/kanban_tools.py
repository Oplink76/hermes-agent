"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the ``kanban`` toolset for
orchestrator work. A normal ``hermes chat`` session still sees **zero**
kanban tools in its schema unless configured.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from hermes_cli.goals import judge_goal
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

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


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _is_delegated_child_context() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context

        return is_delegated_child_context()
    except Exception:
        return False


def _is_dispatcher_owned_worker() -> bool:
    """False for delegate_task children AND for cron jobs fired in-process from
    a worker — i.e. whenever HERMES_KANBAN_* is present but not ours."""
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        return is_dispatcher_owned_worker_context()
    except Exception:
        return True


def _reject_delegated_child_mutation(tool_name: str) -> Optional[str]:
    """Deny Kanban mutations from delegate_task children.

    A delegate_task child runs in the same process as its parent, so stale or
    inherited HERMES_KANBAN_* env vars are not proof of dispatcher ownership.
    The child may summarize findings to its parent, but it must not complete,
    block, heartbeat, comment, create, link, or unblock board tasks directly.
    """
    if not _is_delegated_child_context():
        return None
    return tool_error(
        f"{tool_name} refused: delegate_task child agents are not Kanban "
        "run owners. Return findings to the parent agent; the dispatcher "
        "worker or an explicitly configured Kanban orchestrator must perform "
        "board mutations."
    )


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

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    if _is_delegated_child_context():
        return None
    if not _is_dispatcher_owned_worker():
        # A cron job fired in-process from a worker must never inherit the
        # worker's task id as an implicit default.
        return None
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.
    """
    from hermes_cli import kanban_db as kb
    return kb, kb.connect(board=board)


_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input"})


def _goal_judge_available() -> bool:
    """True when an auxiliary client is configured for the goal judge.

    ``judge_goal`` is fail-open at the source: when no auxiliary model can
    be reached it returns a ``"continue"`` verdict that is indistinguishable
    from a real "not done yet" judgment. The completion gate must not treat
    that as a rejection, or an unconfigured/degraded auxiliary model would
    wedge every ``goal_mode`` worker (it could never close its own task).

    So we probe availability first and only enforce the gate when a judge is
    actually reachable. This mirrors the same client lookup ``judge_goal``
    performs internally.
    """
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        return False
    return client is not None and bool(model)


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
        kb.add_notify_sub(
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


def _goal_mode_handoff_rejection(task, evidence: str) -> Optional[str]:
    """Return a rejection reason when a goal-mode terminal handoff is premature."""
    if not task or not task.goal_mode or not _goal_judge_available():
        return None
    verdict = "done"
    reason = ""
    try:
        verdict, reason, _, _, _ = judge_goal(
            goal=f"{task.title}\n\n{task.body or ''}".strip(),
            last_response=evidence.strip(),
        )
    except Exception as judge_exc:
        # Keep the existing fail-open semantics: an unavailable/broken
        # auxiliary judge must not permanently wedge goal-mode work.
        logger.warning(
            "goal judge check failed, allowing lifecycle handoff: %s",
            judge_exc,
            exc_info=True,
        )
    return reason if verdict != "done" else None


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per-process; runtime activity
#     can tick on every chunk/tool result and we don't need that resolution.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - No durable note on these auto-heartbeats; that's reserved for the
#     explicit tool which carries a model-supplied note.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env() -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True if a write was attempted (whether or not it succeeded);
    False if the call was skipped (not a kanban worker, rate-limited, or
    swallowed exception). The boolean is informational — callers should
    not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited via the module-level ``_auto_heartbeat_last_attempt``
    timestamp (monotonic clock); not thread-safe in the strict sense, but
    the worst case is one extra DB write per race, which is harmless.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                kb.heartbeat_worker(conn, tid, note=None, expected_run_id=run_id)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


# Live operator-note injection: poll the worker's task for new comments and
# fold them into the running agent via the OUT-OF-BAND steer channel, so a user
# can "talk to" a running kanban task without the block → comment → unblock
# dance (or a restart). Rate-limited on its own (tighter than the 60s heartbeat
# so notes land within a few seconds), watermarked per task id.
_COMMENT_POLL_MIN_INTERVAL_SECONDS = 6.0
_comment_poll_last_attempt: float = 0.0
# task_id -> highest comment id already seen (seeded on first poll so history
# already present in build_worker_context isn't re-injected).
_comment_watermark: dict[str, int] = {}


def inject_new_comments_from_env(agent: Any) -> bool:
    """Fold new operator comments on the current worker's task into ``agent``.

    Best-effort and self-gating: no-op unless this process is a kanban worker
    (``HERMES_KANBAN_TASK`` set) and ``agent`` exposes ``steer``. Returns True
    if a steer was injected, else False. Never raises into the agent loop.

    The first poll only *seeds* the watermark to the newest existing comment —
    those are already in the worker's context — so only comments added after
    the run started are injected. The worker's own authored comments (matched
    by ``HERMES_PROFILE``) are skipped to avoid echoing itself.
    """
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid or agent is None or not hasattr(agent, "steer"):
        return False
    global _comment_poll_last_attempt
    import time as _time
    now = _time.monotonic()
    if (now - _comment_poll_last_attempt) < _COMMENT_POLL_MIN_INTERVAL_SECONDS:
        return False
    _comment_poll_last_attempt = now

    seen = _comment_watermark.get(tid)
    try:
        kb, conn = _connect()
        try:
            rows = kb.list_comments_after(conn, tid, after_id=seen or 0)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        logger.debug("comment-inject: bridge failed", exc_info=True)
        return False

    if seen is None:
        # First poll for this task: seed past the existing thread, inject nothing.
        _comment_watermark[tid] = max((c.id for c in rows), default=0)
        return False
    if not rows:
        return False

    # Advance the watermark past everything we just read (including our own
    # notes) so nothing is re-injected next poll.
    _comment_watermark[tid] = max(c.id for c in rows)

    own = (os.environ.get("HERMES_PROFILE") or "").strip()
    fresh = [c for c in rows if (c.author or "").strip() != own and (c.body or "").strip()]
    if not fresh:
        return False

    lines = [f"- {c.author or 'operator'}: {c.body.strip()}" for c in fresh]
    note = (
        "New note"
        + ("s" if len(fresh) > 1 else "")
        + " on your kanban task from the operator (delivered mid-run). "
        + "Take it into account for the work you're doing right now:\n"
        + "\n".join(lines)
    )
    try:
        return bool(agent.steer(note))
    except Exception:
        logger.debug("comment-inject: steer failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _require_configured_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Re-check configured orchestrator authority at execution time."""
    if _check_kanban_orchestrator_mode():
        return None
    return tool_error(
        f"{tool_name} requires a configured Kanban orchestrator profile "
        "outside dispatcher-worker and delegated-child contexts."
    )


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


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

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


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


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
            # Goal-mode pre-completion judge gate (Issue #38367).
            # Prevent workers from bypassing the auxiliary judge by
            # calling kanban_complete before acceptance criteria are met.
            # Only enforce when a judge is actually reachable — see
            # _goal_judge_available for why an unavailable judge fails open.
            task = kb.get_task(conn, tid)
            rejection = _goal_mode_handoff_rejection(
                task,
                (summary or result or "").strip(),
            )
            if rejection is not None:
                return tool_error(
                    f"Goal completion rejected by judge: {rejection}. "
                    f"To proceed, either: (1) provide explicit acceptance "
                    f"evidence in your summary matching the task's criteria, "
                    f"or (2) create continuation tasks with parents=[{tid}] "
                    f"and keep this task alive."
                )

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


def _handle_request_review(args: dict, **kw) -> str:
    """Move implementation into the first-class review phase."""
    delegated_err = _reject_delegated_child_mutation("kanban_request_review")
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
    if not summary or not str(summary).strip():
        return tool_error(
            "summary is required — describe what was implemented and how it "
            "was verified so the reviewer has context"
        )
    summary = redact_sensitive_text(str(summary), force=True)
    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    if metadata is not None:
        metadata_json = redact_sensitive_text(json.dumps(metadata), force=True)
        try:
            metadata = json.loads(metadata_json)
        except json.JSONDecodeError:
            return tool_error("metadata could not be safely serialized")
    metadata = _stamp_worker_session_metadata(tid, metadata)
    reviewer = args.get("reviewer") or None
    if reviewer:
        # Model-supplied free text stored durably on the event payload —
        # redact like summary / kanban_block's reason.
        reviewer = redact_sensitive_text(str(reviewer), force=True)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            rejection = _goal_mode_handoff_rejection(task, summary)
            if rejection is not None:
                return tool_error(
                    f"Goal review handoff rejected by judge: {rejection}. "
                    "Provide acceptance evidence matching the card before "
                    "requesting review."
                )
            ok, fail_reason = kb.request_review(
                conn, tid,
                summary=summary,
                metadata=metadata,
                reviewer=reviewer,
                expected_run_id=_worker_run_id(tid),
                with_reason=True,
            )
            if not ok:
                detail = fail_reason or "unknown id or not in running/ready"
                return tool_error(
                    f"could not request review for {tid}: {detail}"
                )
            run = kb.latest_run(conn, tid)
            landed = kb.get_task(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "review",
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_request_review: {e}")
    except Exception as e:
        logger.exception("kanban_request_review failed")
        return tool_error(f"kanban_request_review: {e}")


def _handle_request_changes(args: dict, **kw) -> str:
    """Return a reviewer-owned running task to its implementer."""
    delegated_err = _reject_delegated_child_mutation("kanban_request_changes")
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
        return tool_error("reason is required — describe the changes needed")
    reason = redact_sensitive_text(str(reason), force=True)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok, detail = kb.request_changes(
                conn,
                tid,
                reason=reason,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not request changes for {tid}: {detail or 'invalid review state'}"
                )
            landed = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "ready",
                implementer=detail,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_request_changes: {e}")
    except Exception as e:
        logger.exception("kanban_request_changes failed")
        return tool_error(f"kanban_request_changes: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    delegated_err = _reject_delegated_child_mutation("kanban_heartbeat")
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
    note = args.get("note")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


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


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    delegated_err = _reject_delegated_child_mutation("kanban_comment")
    if delegated_err:
        return delegated_err
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    body = redact_sensitive_text(str(body), force=True)
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _handle_attach(args: dict, **kw) -> str:
    """Attach an inline (base64) file to a task.

    Mirrors the dashboard's upload endpoint for the agent surface: decode
    the payload, enforce the shared size cap, write it under the per-task
    attachments dir, and record the metadata row — all via
    ``kanban_db.store_attachment_bytes`` so the three surfaces stay in lockstep.
    """
    from hermes_cli import kanban_db as kb

    delegated_err = _reject_delegated_child_mutation("kanban_attach")
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
    filename = args.get("filename")
    if not filename or not str(filename).strip():
        return tool_error("filename is required")
    content_b64 = args.get("content_base64")
    if not content_b64 or not str(content_b64).strip():
        return tool_error("content_base64 is required")
    import base64
    import binascii
    try:
        data = base64.b64decode(str(content_b64), validate=True)
    except (binascii.Error, ValueError) as e:
        return tool_error(f"content_base64 is not valid base64: {e}")
    content_type = args.get("content_type")
    board = args.get("board")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach: {e}")
    except Exception as e:
        logger.exception("kanban_attach failed")
        return tool_error(f"kanban_attach: {e}")


_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) with SSRF guarding, capped at ``max_bytes``.

    Every hop — the initial URL and each redirect target — is validated with
    ``tools.url_safety.is_safe_url`` before it is fetched, so a
    model-controlled URL (or a public host 302ing to one) cannot reach
    loopback, private/CGNAT ranges, or cloud metadata endpoints. Redirects
    are followed manually (``follow_redirects=False``) so each Location is
    re-checked, mirroring ``tools.skills_hub._guarded_http_get``.

    Returns ``(data, content_type)``. Raises ``ValueError`` for a non-http(s)
    scheme, an SSRF-blocked target, too many redirects, or a body that
    overruns the cap (the caller maps it to a clean tool error). Reads in
    chunks so an oversize response is rejected without buffering the whole
    thing.
    """
    from urllib.parse import urljoin, urlparse

    import httpx

    from tools.url_safety import is_safe_url

    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"unsupported URL scheme {scheme!r}; only http/https are allowed"
            )
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}"
            )
        chunks: list[bytes] = []
        total = 0
        with httpx.stream(
            "GET",
            current_url,
            headers={"User-Agent": "hermes-kanban/attach"},
            timeout=30,
            follow_redirects=False,
        ) as resp:
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
                    raise ValueError(
                        f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit"
                    )
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")


def _handle_attach_url(args: dict, **kw) -> str:
    """Attach a file fetched server-side from a URL.

    The agent passes a URL; Hermes downloads it (with the shared size cap)
    and stores it as a real attachment. Useful when the agent has a link
    rather than the bytes. Only http/https URLs are accepted.
    """
    from hermes_cli import kanban_db as kb

    delegated_err = _reject_delegated_child_mutation("kanban_attach_url")
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
    url = args.get("url")
    if not url or not str(url).strip():
        return tool_error("url is required")
    url = str(url).strip()
    filename = args.get("filename") or args.get("title")
    if not filename or not str(filename).strip():
        # Derive a name from the URL path's leaf component.
        from urllib.parse import unquote, urlparse
        leaf = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
        filename = leaf or "download"
    content_type = args.get("content_type")
    board = args.get("board")
    try:
        data, fetched_ct = _download_url_with_cap(url, kb.KANBAN_ATTACHMENT_MAX_BYTES)
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url download failed")
        return tool_error(f"kanban_attach_url: failed to fetch {url}: {e}")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type or fetched_ct,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach_url: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url failed")
        return tool_error(f"kanban_attach_url: {e}")


def _handle_attachments(args: dict, **kw) -> str:
    """List a task's attachments (read-only; no ownership restriction)."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            if kb.get_task(conn, tid) is None:
                return tool_error(f"task {tid} not found")
            atts = kb.list_attachments(conn, tid)
            return json.dumps({
                "ok": True,
                "task_id": tid,
                "attachments": [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "content_type": a.content_type,
                        "size": a.size,
                        "uploaded_by": a.uploaded_by,
                        "stored_path": a.stored_path,
                        "created_at": a.created_at,
                    }
                    for a in atts
                ],
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_attachments: {e}")
    except Exception as e:
        logger.exception("kanban_attachments failed")
        return tool_error(f"kanban_attachments: {e}")


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
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
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
    goal_mode, goal_bool_error = _parse_bool_arg(args, "goal_mode")
    if goal_bool_error:
        return tool_error(goal_bool_error)
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


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Auto-subscribe the calling session to task completion / block events.

    Returns True if a subscription row was written, False otherwise (no
    session context, config gate disabled, or best-effort failure). The
    caller surfaces this in the ``subscribed`` field of the kanban_create
    response so an orchestrator can decide whether to fall back to an
    explicit ``kanban_notify-subscribe`` or to polling.

    Gated by ``kanban.auto_subscribe_on_create`` in config.yaml (default
    True). Disable to mirror pre-feature behaviour, e.g. when the
    originating user/chat opted out via the per-platform notification
    toggle (see ``hermes dashboard``).

    Subscription paths:

    - **Gateway** (telegram/discord/slack/etc): ``HERMES_SESSION_PLATFORM``,
      ``HERMES_SESSION_CHAT_ID``, and ``HERMES_SESSION_CHAT_TYPE`` are set in
      ContextVars by the messaging gateway before agent dispatch. The
      notification poller already keys off these, so we just register a row.

    - **TUI** (herm desktop / herm TUI): the platform/chat_id ContextVars
      are intentionally cleared (TUI is a single-channel local UI, not
      a multi-tenant chat surface), but the agent subprocess inherits
      ``HERMES_SESSION_KEY`` from the parent session. We subscribe with
      ``platform="tui"`` and ``chat_id=<key>``; the TUI notification
      poller (``tui_gateway/server.py``) reads ``kanban_notify_subs``
      for these rows and posts the completion message into the running
      session.

    - **CLI / cron / test / unattached**: no persistent delivery channel,
      no-op.

    Failure mode: any exception inside the function is logged at WARNING
    with the offending exception + diagnostic env vars and swallowed.
    We never want a notification bookkeeping failure to fail the
    kanban_create that the agent is mid-conversation about.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # If config can't load we still default to True — this is the
        # user-friendly behaviour that mirrors the pre-gate implementation.
        pass

    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            # TUI / desktop fallback: platform/chat_id ContextVars are
            # cleared for TUI sessions, but the parent process exports
            # HERMES_SESSION_KEY into the subprocess env. Treat that
            # as a "tui" subscription so the TUI notification poller
            # (tui_gateway/server.py) can pick it up.
            #
            # HERMES_SESSION_ID is intentionally NOT a fallback here:
            # it is set by ACP / the agent subprocess for telemetry
            # regardless of whether the parent is a TUI or a CLI, so
            # treating it as a notification target would auto-subscribe
            # every CLI invocation, which is exactly the over-eager
            # behaviour that got #19718 reverted upstream. The TUI
            # poller keys on HERMES_SESSION_KEY.
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return False  # CLI / cron / test — no persistent channel
            platform = "tui"
            chat_id = session_key
        is_gateway_session = platform != "tui"
        chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "") or None
        delivery_mode = "notify+wake" if is_gateway_session else None
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        user_id_alt = get_session_env("HERMES_SESSION_USER_ID_ALT", "") or None
        message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "") or ""
        notifier_profile = (
            get_session_env("HERMES_SESSION_PROFILE", "")
            or os.environ.get("HERMES_PROFILE")
        )
        if not notifier_profile:
            try:
                from hermes_cli.profiles import get_active_profile_name
                notifier_profile = get_active_profile_name() or "default"
            except Exception:
                notifier_profile = "default"
        delivery_metadata: dict[str, Any] = {}
        if thread_id:
            delivery_metadata["thread_id"] = thread_id
        if chat_type:
            delivery_metadata["chat_type"] = chat_type
        if (
            platform.lower() == "telegram"
            and thread_id
            and (chat_type or "").lower() in {"dm", "direct", "private"}
        ):
            delivery_metadata["telegram_dm_topic_reply_fallback"] = True
            if str(thread_id) not in {"", "1"}:
                delivery_metadata["direct_messages_topic_id"] = str(thread_id)
            if message_id:
                delivery_metadata["telegram_reply_to_message_id"] = str(message_id)

        # Lazy-import to keep the module-level dependency light
        from hermes_cli import kanban_db as _kb
        _kb.add_notify_sub(
            conn, task_id=task_id,
            platform=platform, chat_id=chat_id,
            thread_id=thread_id, user_id=user_id, user_id_alt=user_id_alt,
            chat_type=chat_type,
            notifier_profile=notifier_profile,
            delivery_mode=delivery_mode,
            delivery_metadata=delivery_metadata or None,
        )
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, platform, bool(chat_id),
        )
        return False


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task to ready, or todo while parents remain open."""
    delegated_err = _reject_delegated_child_mutation("kanban_unblock")
    if delegated_err:
        return delegated_err
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            task = kb.get_task(conn, str(tid))
            return _ok(task_id=str(tid), status=task.status if task else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


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


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

REVIEW_TARGET_SCHEMA = {
    "name": "review_target",
    "description": (
        "Read the immutable Git diff pinned for the current Reviewer run. "
        "Returns fixed base/head commit SHAs, changed and binary files, and one "
        "bounded diff-line page with explicit overlong-line truncation. Continue "
        "with next_offset until complete is true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Diff-line offset into the pinned diff (default 0).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Surfaced to downstream "
                    "workers alongside ``summary``. On product boards, "
                    "Development/Test/Review completions must include "
                    "``ai_provenance``: Development needs "
                    "{\"writer\": {\"agent\": \"claude-code\"}}; Test needs "
                    "{\"tester\": {\"agent\": \"hermes\", \"result\": \"passed\"}}; "
                    "Review needs {\"reviewer\": {\"agent\": \"codex\"}, "
                    "\"writer\": {\"agent\": \"claude-code\"}} and reviewer "
                    "must differ from writer. Include branch/worktree/commit "
                    "when available."
                ),
            },
            "workflow_outcome": {
                "type": "object",
                "description": (
                    "Structured product test/review outcome. Rejections require "
                    "verdict, target_step, and non-empty findings."
                ),
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["passed", "approved", "changes_requested", "architecture_invalid"],
                    },
                    "target_step": {
                        "type": "string",
                        "enum": ["architecture", "development"],
                    },
                    "findings": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["verdict"],
                "additionalProperties": False,
                "allOf": [
                    {
                        "if": {
                            "properties": {
                                "verdict": {
                                    "enum": ["changes_requested", "architecture_invalid"]
                                }
                            }
                        },
                        "then": {"required": ["target_step", "findings"]},
                    },
                    {
                        "if": {
                            "properties": {
                                "verdict": {"enum": ["passed", "approved"]}
                            }
                        },
                        "then": {
                            "not": {
                                "anyOf": [
                                    {"required": ["target_step"]},
                                    {"required": ["findings"]},
                                ]
                            }
                        },
                    },
                ],
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk at completion. Files inside a managed scratch "
                    "workspace are copied to durable task attachments before "
                    "cleanup; a missing declared scratch artifact keeps the "
                    "task in-flight so you can fix the path and retry."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}


_KANBAN_UNLINK_EXPECTED_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "title": {"type": "string"},
        "assignee": {"type": ["string", "null"]},
        "current_step_key": {"type": ["string", "null"]},
        "current_run_id": {"type": ["integer", "null"]},
    },
    "required": [
        "status",
        "title",
        "assignee",
        "current_step_key",
        "current_run_id",
    ],
    "additionalProperties": False,
}


KANBAN_UNLINK_SCHEMA = {
    "name": "kanban_unlink",
    "description": (
        "Atomically remove one exact Default-board parent→child dependency edge "
        "using the child's fresh five-field lifecycle snapshot. Only that child "
        "is reconsidered for readiness. Configured-orchestrator-only; call "
        "kanban_show immediately first and copy the child's current lifecycle "
        "fields into expected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {
                "type": "string",
                "minLength": 1,
                "description": "Existing parent task id for the exact edge.",
            },
            "child_id": {
                "type": "string",
                "minLength": 1,
                "description": "Existing child task id for the exact edge.",
            },
            "expected": _KANBAN_UNLINK_EXPECTED_SCHEMA,
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id", "expected"],
        "additionalProperties": False,
    },
}


_KANBAN_CONFIGURE_EXPECTED_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "title": {"type": "string"},
        "assignee": {"type": ["string", "null"]},
        "current_step_key": {"type": ["string", "null"]},
        "current_run_id": {"type": ["integer", "null"]},
        "source_policy": {
            "type": "string",
            "enum": ["none", "required", "forbidden"],
        },
        "max_retries": {"type": ["integer", "null"], "minimum": 1},
        "max_runtime_seconds": {
            "type": ["integer", "null"],
            "minimum": 1,
        },
        "goal_mode": {"type": "boolean"},
    },
    "required": [
        "status",
        "title",
        "assignee",
        "current_step_key",
        "current_run_id",
        "source_policy",
        "max_retries",
        "max_runtime_seconds",
        "goal_mode",
    ],
    "additionalProperties": False,
}

KANBAN_CONFIGURE_SCHEMA = {
    "name": "kanban_configure",
    "description": (
        "Atomically replace source_policy, max_retries, max_runtime_seconds, "
        "and goal_mode on one eligible existing Default-board card. "
        "Configured-orchestrator-only. Call kanban_show immediately first and "
        "copy the nine current lifecycle/execution fields into expected. "
        "Refuses stale, active, terminal, delegated, worker, and strict-board calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Existing task id."},
            "source_policy": {
                "type": "string",
                "enum": ["none", "required", "forbidden"],
            },
            "max_retries": {"type": ["integer", "null"], "minimum": 1},
            "max_runtime_seconds": {
                "type": ["integer", "null"],
                "minimum": 1,
            },
            "goal_mode": {"type": "boolean"},
            "expected": _KANBAN_CONFIGURE_EXPECTED_SCHEMA,
            "board": _board_schema_prop(),
        },
        "required": [
            "task_id",
            "source_policy",
            "max_retries",
            "max_runtime_seconds",
            "goal_mode",
            "expected",
        ],
        "additionalProperties": False,
    },
}


KANBAN_RESOLVE_SCHEMA = {
    "name": "kanban_resolve",
    "description": (
        "Resolve the current Hermes product-workflow preflight using one "
        "audited, compare-and-swap decision. Resolver-only. Read the task "
        "with kanban_show immediately before calling and copy the complete "
        "task/preflight snapshot into expected. Conflicts never retry "
        "automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
            "decision": {
                "type": "string",
                "enum": ["resume", "repair", "escalate"],
            },
            "fault_domain": {
                "type": "string",
                "enum": ["task_state", "framework"],
            },
            "diagnosis": {"type": "string"},
            "reason": {"type": "string"},
            "expected": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "integer"},
                    "preflight_event_id": {"type": "integer"},
                    "status": {"type": "string"},
                    "phase": {"type": ["string", "null"]},
                    "assignee": {"type": ["string", "null"]},
                    "project_id": {"type": ["string", "null"]},
                    "workflow_template_id": {"type": ["string", "null"]},
                    "workspace_kind": {"type": "string"},
                    "workspace_path": {"type": ["string", "null"]},
                    "branch_name": {"type": ["string", "null"]},
                    "running": {"type": "boolean"},
                    "blocked": {"type": "boolean"},
                },
                "required": [
                    "run_id", "preflight_event_id", "status", "phase",
                    "assignee", "project_id", "workflow_template_id",
                    "workspace_kind", "workspace_path", "branch_name",
                    "running", "blocked",
                ],
                "additionalProperties": False,
            },
            "repair": {
                "type": "object",
                "properties": {
                    "workflow": {
                        "type": "object",
                        "properties": {
                            "phase": {
                                "type": "string",
                                "enum": [
                                    "backlog", "architecture", "development",
                                    "test", "review",
                                ],
                            },
                            "assignee": {"type": "string"},
                            "project_id": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    "adopt_handoff_sha": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "required": [
            "task_id", "decision", "fault_domain", "diagnosis", "reason",
            "expected",
        ],
        "additionalProperties": False,
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Stop work on this task and route it according to WHY you're stuck. "
        "Set ``kind`` to say which: 'dependency' (waiting on another task — "
        "goes to todo and auto-resumes when that task finishes, no human "
        "needed), 'needs_input' (you need a human decision/answer), "
        "'capability' (a hard wall: no access, missing credentials, an action "
        "no agent can do), or 'transient' (a flaky failure that may clear). "
        "For product-board human/capability blocks, you must include "
        "``attempted_resolutions`` describing the concrete alternatives you "
        "already tried; Hermes will take the first resolution pass before any "
        "Slack human escalation. ``reason`` is shown to the human on the board. "
        "If a task keeps getting unblocked and re-blocked for the same reason, "
        "it is auto-escalated to triage. Use for genuine blockers only — don't "
        "block on things you can resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered or what stopped you, in one or "
                    "two sentences. Don't paste the whole conversation; the "
                    "human has the board and can ask follow-ups via comments."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["dependency", "needs_input", "capability", "transient"],
                "description": (
                    "Why you're blocked. 'dependency' waits in todo and "
                    "resumes automatically; the others surface to a human. "
                    "Omit only if none apply."
                ),
            },
            "attempted_resolutions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For product-board human-in-the-loop blocks: the concrete "
                    "things you already tried before asking for help. Required "
                    "for needs_input/capability/legacy human blocks on product "
                    "boards. Examples: checked docs, searched repo, tried "
                    "fallback API, asked another agent via comment."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
}

KANBAN_REQUEST_REVIEW_SCHEMA = {
    "name": "kanban_request_review",
    "description": (
        "Hand the task off for review: implementation, self-review, and "
        "verification are complete and you want a human (or reviewer) to "
        "look before it is marked done. Moves the task to the 'review' "
        "column and notifies the subscriber. Unlike ``kanban_block`` this is "
        "NOT a blocker — it never counts toward unblock-loop detection, so a "
        "task can cycle through review across follow-ups without ever being "
        "falsely escalated to triage. Use this instead of blocking with a "
        "free-form 'review-required:' reason."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "What was implemented and how it was verified, in one or "
                    "two sentences — shown to the reviewer. Don't paste "
                    "the whole diff; the reviewer has the board and the PR."
                ),
            },
            "reviewer": {
                "type": "string",
                "description": (
                    "Optional reviewer profile. When provided, the task is "
                    "reassigned to that profile before review dispatch."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Optional structured handoff facts for the reviewer, such "
                    "as changed_files, tests_run, commit, or decisions."
                ),
                "additionalProperties": True,
            },
            "board": _board_schema_prop(),
        },
        "required": ["summary"],
    },
}

KANBAN_REQUEST_CHANGES_SCHEMA = {
    "name": "kanban_request_changes",
    "description": (
        "Reviewer verdict: return the current review run to the original "
        "implementer with concrete required changes. This closes the review "
        "run, reapplies parent dependency gating, and requeues the task without "
        "using block-loop accounting. Only use from a task claimed from the "
        "review column; use kanban_block only for a genuine external blocker."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "Specific, actionable changes the implementer must make "
                    "before requesting another review."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_ATTACH_SCHEMA = {
    "name": "kanban_attach",
    "description": (
        "Attach a file to a task by passing its bytes inline (base64). "
        "Use for genuine file artifacts the next worker or a human should "
        "be able to download — generated reports, images, exports. The "
        "file is stored as a real attachment (not a comment link) under "
        "the task's attachments dir, capped at 25 MB. Prefer "
        "kanban_attach_url when you only have a URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "filename": {
                "type": "string",
                "description": (
                    "File name to store it under (e.g. 'report.pdf'). "
                    "Directory components are stripped; only the leaf is kept."
                ),
            },
            "content_base64": {
                "type": "string",
                "description": "The file contents, base64-encoded. Max 25 MB decoded.",
            },
            "content_type": {
                "type": "string",
                "description": "Optional MIME type (e.g. 'application/pdf').",
            },
            "board": _board_schema_prop(),
        },
        "required": ["filename", "content_base64"],
    },
}

KANBAN_ATTACH_URL_SCHEMA = {
    "name": "kanban_attach_url",
    "description": (
        "Attach a file to a task by URL — Hermes downloads it server-side "
        "and stores it as a real attachment (capped at 25 MB). Use when "
        "you have a link rather than the bytes. Only http/https URLs are "
        "accepted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "url": {
                "type": "string",
                "description": "http(s) URL to fetch and store.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional name to store it under. Defaults to the URL "
                    "path's leaf component."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "Optional MIME type override. Defaults to the "
                    "Content-Type the server returns."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["url"],
    },
}

KANBAN_ATTACHMENTS_SCHEMA = {
    "name": "kanban_attachments",
    "description": (
        "List the files attached to a task: id, filename, content_type, "
        "size, who uploaded it, and the absolute on-disk path you can read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project id or slug to link the task to. When "
                    "set, the task becomes a git worktree under the project's "
                    "primary repo with a deterministic branch (project slug + "
                    "task id), instead of a random branch."
                ),
            },
            "workflow_template_id": {
                "type": "string",
                "description": (
                    "Optional workflow template id to stamp at creation time. "
                    "Use 'product' for Kanban V2 product-board cards."
                ),
            },
            "current_step_key": {
                "type": "string",
                "description": (
                    "Optional workflow step key to stamp at creation time. "
                    "Use 'backlog' for new product user-story/work cards unless "
                    "a later approved flow intentionally targets another step."
                ),
            },
            "source_policy": {
                "type": "string",
                "enum": ["none", "required", "forbidden"],
                "description": "Default-board execution contract for source commits.",
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker. The kanban lifecycle is already injected "
                    "automatically; use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "goal_mode": {
                "type": "boolean",
                "description": (
                    "Run the dispatched worker in a goal loop. When true, "
                    "after each turn an auxiliary judge checks the worker's "
                    "response against this card's title/body; if the work "
                    "isn't done and budget remains, the worker keeps going "
                    "in the same session until the judge agrees it's "
                    "complete (or the goal-turn budget is exhausted, which "
                    "blocks the task for human review). Use this for "
                    "open-ended cards where one shot rarely finishes the "
                    "work. Defaults to false (classic single-shot worker)."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "description": (
                    "Turn budget for goal_mode workers. Caps how many "
                    "continuation turns the worker may take before the task "
                    "is blocked for review. Ignored unless goal_mode is "
                    "true. Defaults to the goal-engine default (20)."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Pin the dispatched worker to this model instead of "
                    "the assignee profile's configured model. Use the "
                    "exact model name the target provider expects. Omit "
                    "to use the profile default."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Provider the 'model' belongs to (e.g. 'openrouter', "
                    "'anthropic', 'nous'). Set this whenever the model "
                    "is not from the assignee profile's configured "
                    "provider — a model name alone is resolved against "
                    "the profile's provider and will fail if it belongs "
                    "to a different one. Requires 'model'."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Unblock a Kanban task. It moves to ready when all parents are done, "
        "or todo while any parent remains open. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to move to ready or parent-gated todo.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}

WORK_INBOX_SHOW_SCHEMA = {
    "name": "work_inbox_show",
    "description": "Read the exact claimed Work Inbox intake and authoritative context.",
    "parameters": {"type": "object", "properties": {}},
}

WORK_INBOX_HEARTBEAT_SCHEMA = {
    "name": "work_inbox_heartbeat",
    "description": "Renew the exact Product Owner intake claim.",
    "parameters": {
        "type": "object",
        "properties": {"note": {"type": "string"}},
    },
}

_WORK_INBOX_STRING_LIST_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
}

WORK_INBOX_PROPOSAL_SCHEMA = {
    "type": "object",
    "description": (
        "Complete semantic Product Decision. Hermes supplies trusted PO evidence, "
        "entry routing, skipped-phase evidence, and issuer identity."
    ),
    "properties": {
        "work": {
            "type": "object",
            "properties": {
                "item_kind": {"type": "string", "enum": ["card", "epic"]},
                "work_type": {"type": "string"},
                "title": {"type": "string"},
                "outcome": {"type": "string"},
                "scope": _WORK_INBOX_STRING_LIST_SCHEMA,
                "out_of_scope": _WORK_INBOX_STRING_LIST_SCHEMA,
            },
            "required": [
                "item_kind",
                "work_type",
                "title",
                "outcome",
                "scope",
                "out_of_scope",
            ],
            "additionalProperties": False,
        },
        "routing": {
            "type": "object",
            "properties": {
                "entry_phase": {
                    "type": ["string", "null"],
                    "description": (
                        "Use null for an Epic; Hermes replaces card routing "
                        "with the board Architecture phase."
                    ),
                },
                "assignee": {
                    "type": ["string", "null"],
                    "description": (
                        "Use null for an Epic; Hermes supplies the card assignee."
                    ),
                },
                "epic_id": {"type": ["string", "null"]},
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "entry_phase",
                "assignee",
                "epic_id",
                "dependencies",
            ],
            "additionalProperties": False,
        },
        "handover": {
            "type": "object",
            "properties": {
                "deliverables": _WORK_INBOX_STRING_LIST_SCHEMA,
                "required_evidence": _WORK_INBOX_STRING_LIST_SCHEMA,
                "done_when": _WORK_INBOX_STRING_LIST_SCHEMA,
                "next_phase": {"type": ["string", "null"]},
                "next_role": {"type": ["string", "null"]},
            },
            "required": [
                "deliverables",
                "required_evidence",
                "done_when",
                "next_phase",
                "next_role",
            ],
            "additionalProperties": False,
        },
        "rules": {
            "type": "object",
            "properties": {
                "allowed": _WORK_INBOX_STRING_LIST_SCHEMA,
                "forbidden": _WORK_INBOX_STRING_LIST_SCHEMA,
            },
            "required": ["allowed", "forbidden"],
            "additionalProperties": False,
        },
        "sizing": {
            "type": "object",
            "description": (
                "Independent Product Owner sizing. The configured budget is "
                "the Development profile's agent.max_turns; provide one "
                "estimate for a card or one per Epic story."
            ),
            "properties": {
                "rationale": {"type": "string"},
                "configured_iteration_budget": {
                    "type": "integer",
                    "minimum": 1,
                },
                "estimated_turns": {"type": "integer", "minimum": 1},
                "card_estimates": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                },
                "fits_budget": {"type": "boolean"},
            },
            "required": [
                "rationale",
                "configured_iteration_budget",
                "estimated_turns",
                "fits_budget",
            ],
            "additionalProperties": False,
        },
        "requirement_feasibility": {
            "type": "object",
            "description": (
                "Auditable achievability gate. Every binding required-evidence "
                "or Epic story done-when item must appear exactly once under "
                "achievable_requirements with a concrete basis. Current-state "
                "Test findings that cannot yet be achieved belong in "
                "deferred_findings and must not remain binding."
            ),
            "properties": {
                "rationale": {"type": "string"},
                "achievable_requirements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "requirement": {"type": "string"},
                            "basis": _WORK_INBOX_STRING_LIST_SCHEMA,
                        },
                        "required": ["requirement", "basis"],
                        "additionalProperties": False,
                    },
                },
                "deferred_findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding": {"type": "string"},
                            "reason": {"type": "string"},
                            "enabling_dependency": {"type": "string"},
                        },
                        "required": [
                            "finding",
                            "reason",
                            "enabling_dependency",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "rationale",
                "achievable_requirements",
                "deferred_findings",
            ],
            "additionalProperties": False,
        },
        "classification": _WORK_INBOX_STRING_LIST_SCHEMA,
        "stories": {
            "type": "array",
            "description": "Empty for a card; required decomposition for an Epic.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "outcome": {"type": "string"},
                    "scope": _WORK_INBOX_STRING_LIST_SCHEMA,
                    "out_of_scope": _WORK_INBOX_STRING_LIST_SCHEMA,
                    "done_when": _WORK_INBOX_STRING_LIST_SCHEMA,
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                    },
                },
                "required": [
                    "title",
                    "outcome",
                    "scope",
                    "out_of_scope",
                    "done_when",
                    "depends_on",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "work",
        "routing",
        "handover",
        "rules",
        "sizing",
        "requirement_feasibility",
        "classification",
        "stories",
    ],
    "additionalProperties": False,
}

WORK_INBOX_DECIDE_SCHEMA = {
    "name": "work_inbox_decide",
    "description": (
        "Finish the Product Owner assessment. Accepted proposals are validated, "
        "signed, and materialized by Hermes; this tool does not grant direct card authority."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "disposition": {
                "type": "string",
                "enum": ["accepted", "needs_clarification", "rejected"],
            },
            "reason": {"type": "string"},
            "question": {"type": "string"},
            "proposal": WORK_INBOX_PROPOSAL_SCHEMA,
        },
        "required": ["disposition", "reason"],
    },
}

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="work_inbox_show",
    toolset="kanban",
    schema=WORK_INBOX_SHOW_SCHEMA,
    handler=_handle_work_inbox_show,
    check_fn=_check_work_inbox_mode,
    emoji="📥",
)

registry.register(
    name="work_inbox_decide",
    toolset="kanban",
    schema=WORK_INBOX_DECIDE_SCHEMA,
    handler=_handle_work_inbox_decide,
    check_fn=_check_work_inbox_mode,
    emoji="✅",
)

registry.register(
    name="work_inbox_heartbeat",
    toolset="kanban",
    schema=WORK_INBOX_HEARTBEAT_SCHEMA,
    handler=_handle_work_inbox_heartbeat,
    check_fn=_check_work_inbox_mode,
    emoji="💓",
)

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="review_target",
    toolset="kanban",
    schema=REVIEW_TARGET_SCHEMA,
    handler=_handle_review_target,
    check_fn=_check_reviewer_mode,
    emoji="🔎",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_ordinary_worker_mode,
    emoji="✔",
)

registry.register(
    name="kanban_resolve",
    toolset="kanban",
    schema=KANBAN_RESOLVE_SCHEMA,
    handler=_handle_resolve,
    check_fn=_check_resolver_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_ordinary_worker_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_request_review",
    toolset="kanban",
    schema=KANBAN_REQUEST_REVIEW_SCHEMA,
    handler=_handle_request_review,
    # Fork invariant: request_review/request_changes are lifecycle exits, so
    # they belong to the ordinary-worker surface. Upstream gates them on
    # ``_check_kanban_mode`` because it has no privileged Resolver; here that
    # would hand the read-only Resolver two mutation tools.
    check_fn=_check_ordinary_worker_mode,
    emoji="👀",
)

registry.register(
    name="kanban_request_changes",
    toolset="kanban",
    schema=KANBAN_REQUEST_CHANGES_SCHEMA,
    handler=_handle_request_changes,
    check_fn=_check_ordinary_worker_mode,
    emoji="↩",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_attach",
    toolset="kanban",
    schema=KANBAN_ATTACH_SCHEMA,
    handler=_handle_attach,
    check_fn=_check_ordinary_worker_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attach_url",
    toolset="kanban",
    schema=KANBAN_ATTACH_URL_SCHEMA,
    handler=_handle_attach_url,
    check_fn=_check_ordinary_worker_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attachments",
    toolset="kanban",
    schema=KANBAN_ATTACHMENTS_SCHEMA,
    handler=_handle_attachments,
    check_fn=_check_ordinary_worker_mode,
    emoji="📎",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_ordinary_worker_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_configure",
    toolset="kanban",
    schema=KANBAN_CONFIGURE_SCHEMA,
    handler=_handle_configure,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="⚙",
)

registry.register(
    name="kanban_unlink",
    toolset="kanban",
    schema=KANBAN_UNLINK_SCHEMA,
    handler=_handle_unlink,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="🔓",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_ordinary_worker_mode,
    emoji="🔗",
)
