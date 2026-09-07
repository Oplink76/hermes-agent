"""Embedded kanban dispatcher: settings resolution and per-tick board work.

``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher`` owns the loop,
the singleton lock and the health telemetry; everything that only needs the
``kanban_db`` module and the resolved settings lives here.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from gateway.kanban_watchers_common import _board_slugs, _positive_int_setting, logger


def _process_pending_qualification_intakes(
    _kb,
    conn,
    *,
    board: str,
    per_tick: int,
    qualify=None,
    failure_limit: Optional[int] = None,
) -> dict[str, int]:
    """Process a bounded pending-intake batch before normal dispatch."""

    route_directly = qualify is None
    if route_directly:
        from hermes_cli.kanban_po_intake import route_pending_intake

    recover = getattr(_kb, "recover_stale_qualification_intakes", None)
    if callable(recover):
        recover(
            conn,
            failure_limit=(
                int(failure_limit)
                if failure_limit is not None
                else int(getattr(_kb, "DEFAULT_FAILURE_LIMIT", 2))
            ),
        )
    limit = max(1, int(per_tick))
    pending = _kb.list_qualification_intakes(conn, status="pending")[:limit]
    counts = {"attempted": 0, "qualified": 0, "rejected": 0, "failed": 0}
    for intake in pending:
        intake_id = str(intake["id"])
        counts["attempted"] += 1
        try:
            if route_directly:
                result = route_pending_intake(conn, board=board, intake=intake)
            else:
                result = qualify(conn, board=board, intake_id=intake_id)
        except Exception:
            counts["failed"] += 1
            logger.exception(
                "kanban qualification [%s]: infrastructure failure for %s; "
                "leaving intake pending for the next sweep",
                board,
                intake_id,
            )
            continue
        status = str(result.get("status") or "")
        if status in {"qualified", "overridden"}:
            counts["qualified"] += 1
        elif status == "rejected":
            counts["rejected"] += 1
    return counts


def _qualify_then_dispatch(
    _kb,
    conn,
    slug: str,
    *,
    qualification_per_tick: int,
    qualify=None,
    **dispatch_kwargs,
):
    """Materialize governed intake before the board can dispatch it."""

    _process_pending_qualification_intakes(
        _kb,
        conn,
        board=slug,
        per_tick=qualification_per_tick,
        qualify=qualify,
        failure_limit=dispatch_kwargs.get("failure_limit"),
    )
    return _dispatch_once_then_reconcile(
        _kb, conn, slug, **dispatch_kwargs
    )


def _dispatch_once_then_reconcile(_kb, conn, slug: str, **dispatch_kwargs) -> "Optional[object]":
    """Run one dispatch_once tick, then — for handoff_v2 boards only — run
    ``reconcile()`` on the same connection immediately after.

    ``dispatch_once`` already drives the v2 happy path live (it spawns ready
    v2 cards via claim-CAS and recovers crashed/stale workers), so the piece
    genuinely missing from the live gateway is ``reconcile()``'s
    story->epic integration step and its own bounded recovery pass. This is
    the seam that wires that in (CR4 / Codex P1-P2).

    ``reconcile`` is called with ``spawn_ready=False`` (Codex re-review P1):
    ``dispatch_once`` already spawns all ready v2 cards live, capped by
    ``max_spawn`` / ``max_in_progress`` / per-profile caps / the respawn
    guard / the failure limit. ``reconcile()``'s own ready-spawn step has
    none of that cap awareness, so running it uncapped right after
    ``dispatch_once`` let the tick spawn past the live concurrency cap (2
    ready cards + max_spawn=1 spawned 2, not 1). ``dispatch_once`` remains
    the tick's sole capped spawn owner; reconcile here only recovers
    dead-worker cards and integrates finished stories. A re-idled or
    stranded ready card is still picked up (capped) by ``dispatch_once`` on
    its next tick.

    Legacy (non-v2) boards: ``reconcile`` is never called; this is
    ``dispatch_once`` unchanged.

    Returns ``dispatch_once``'s result unchanged — reconcile does not
    participate in the tick's return/telemetry contract, it's a
    side-effecting safety net layered on top.

    Defensive: an exception raised by ``reconcile`` is logged and swallowed
    here so it can never break the tick or clobber the already-computed
    dispatch_once result. Exceptions from ``dispatch_once`` itself are not
    caught here — they propagate to the caller's existing corrupt-DB /
    generic-exception handling, unchanged.
    """
    result = _kb.dispatch_once(conn, board=slug, **dispatch_kwargs)
    try:
        meta = _kb.product_board_metadata(slug)
        if _kb._handoff_v2_enabled(meta):
            _kb.reconcile(conn, board=slug, spawn_ready=False)
    except Exception:
        logger.exception(
            "kanban dispatcher: reconcile failed on board %s (safety net; "
            "dispatch_once result unaffected)",
            slug,
        )
    return result


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbd():
    from hermes_cli import kanban_db_dispatch
    return kanban_db_dispatch

_CORRUPT_DB_MARKERS = ("file is not a database", "database disk image is malformed")


@dataclass
class _DispatcherSettings:
    """``kanban.*`` dispatch settings, read once at boot (restart to apply)."""

    interval: float
    max_spawn: Any
    max_in_progress: Optional[int]
    failure_limit: int
    stale_timeout_seconds: int
    reconcile_orphans: bool
    default_assignee: Optional[str]
    max_in_progress_per_profile: Optional[int]
    qualification_per_tick: int = 3


def _resolve_dispatcher_settings(kanban_cfg: dict, kb: Any) -> _DispatcherSettings:
    """Parse and log the dispatcher settings in their established order."""
    try:
        interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
    except (ValueError, TypeError):
        logger.warning("kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                       kanban_cfg.get("dispatch_interval_seconds"))
        interval = 60.0
    interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

    qualification_per_tick = _positive_int_setting(kanban_cfg, "qualification_per_tick") or 3

    max_spawn = kanban_cfg.get("max_spawn")
    if max_spawn is not None:
        logger.info("kanban dispatcher: max_spawn=%s", max_spawn)

    # Cap simultaneously running tasks so slow workers don't pile up and time
    # out. Explicit config wins; otherwise a memory-derived default (unbounded
    # fan-out swap-thrashes small hosts), or None where total memory can't be read.
    max_in_progress = _positive_int_setting(kanban_cfg, "max_in_progress")
    effective_max_in_progress = _kbd().resolve_max_in_progress(max_in_progress)
    if max_in_progress is None and effective_max_in_progress is not None:
        logger.info(
            "kanban dispatcher: kanban.max_in_progress unset; using "
            "memory-derived default max_in_progress=%d "
            "(set kanban.max_in_progress in config.yaml to override)",
            effective_max_in_progress,
        )

    raw_failure_limit = kanban_cfg.get("failure_limit", kb.DEFAULT_FAILURE_LIMIT)
    try:
        failure_limit = int(raw_failure_limit)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    if failure_limit < 1:
        logger.warning("kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT

    # 0 disables stale detection.
    raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
    try:
        stale_timeout_seconds = int(raw_stale or 0)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                       "disabling stale detection", raw_stale)
        stale_timeout_seconds = 0

    # Fallback profile for tasks created without an assignee (e.g. via the
    # dashboard). Empty (the schema default) keeps skipping them.
    # When set, the dispatcher applies it to unassigned ready tasks instead of skipping them indefinitely
    # (#27145). Empty string (the schema default) means "no fallback, keep skipping" — backward-compatible
    # with existing installs.
    default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
    if default_assignee:
        logger.info("kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                    "will route to this profile)", default_assignee)

    return _DispatcherSettings(
        interval=interval,
        max_spawn=max_spawn,
        max_in_progress=effective_max_in_progress,
        failure_limit=failure_limit,
        stale_timeout_seconds=stale_timeout_seconds,
        # Requeue 'running' cards with broken claim bookkeeping (zombie-card
        # reconciliation); false keeps orphans frozen for manual forensics.
        reconcile_orphans=bool(kanban_cfg.get("reconcile_orphans", True)),
        default_assignee=default_assignee,
        # Per-profile concurrency cap: no single profile's local model / API
        # quota / browser pool gets overwhelmed by a fan-out.
        max_in_progress_per_profile=_positive_int_setting(kanban_cfg, "max_in_progress_per_profile"),
        qualification_per_tick=qualification_per_tick,
    )


class _KanbanDispatcher:
    """Per-tick board work for the embedded dispatcher (runs in worker threads).

    Boards are enumerated every tick so a board created mid-run is picked up
    without a restart. Corrupt-looking board DBs are quarantined per
    fingerprint and retried after ``CORRUPT_BOARD_RETRY_AFTER_SECONDS``:
    transient WAL/open races can look like "malformed" for one tick.
    """

    CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300

    def __init__(self, kb: Any, settings: _DispatcherSettings) -> None:
        self.kb = kb
        self.settings = settings
        self.disabled_corrupt_boards: dict[str, tuple[tuple[str, int | None, int | None], float]] = {}

    def _board_slugs(self) -> list:
        return _board_slugs(self.kb)

    def board_db_fingerprint(self, slug: str) -> tuple[str, int | None, int | None]:
        path = self.kb.kanban_db_path(slug)
        try:
            resolved = str(path.expanduser().resolve())
        except Exception:
            resolved = str(path)
        try:
            stat = path.stat()
        except OSError:
            return (resolved, None, None)
        return (resolved, stat.st_mtime_ns, stat.st_size)

    def is_corrupt_board_db_error(self, exc: Exception) -> bool:
        if isinstance(exc, _kbc().KanbanDbCorruptError):
            return True
        return isinstance(exc, sqlite3.DatabaseError) and any(m in str(exc).lower() for m in _CORRUPT_DB_MARKERS)

    def _quarantine_lifted(self, slug: str, fingerprint: tuple) -> bool:
        """Return False while *slug* stays quarantined; lift (and log) otherwise."""
        disabled_entry = self.disabled_corrupt_boards.get(slug)
        if disabled_entry is None:
            return True
        disabled_fingerprint, disabled_at = disabled_entry
        age = time.monotonic() - disabled_at
        if disabled_fingerprint == fingerprint and age < self.CORRUPT_BOARD_RETRY_AFTER_SECONDS:
            return False
        if disabled_fingerprint == fingerprint:
            logger.info("kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch", slug, age)
        else:
            logger.info("kanban dispatcher: board %s database changed; retrying dispatch", slug)
        self.disabled_corrupt_boards.pop(slug, None)
        return True

    def tick_once_for_board(self, slug: str) -> Optional[object]:
        """Run one dispatch_once for a specific board.

        The per-board DB is opened explicitly so boards never share a
        connection or claim across each other.
        """
        conn = None
        fingerprint = self.board_db_fingerprint(slug)
        if not self._quarantine_lifted(slug, fingerprint):
            return None
        kwargs = {k: v for k, v in asdict(self.settings).items() if k != "interval"}
        try:
            # No explicit init_db(): connect() runs the migration once per
            # process (see the matching note in the notifier collector).
            conn = _kbc().connect(board=slug)
            return _qualify_then_dispatch(self.kb, conn, slug, **kwargs)
        except Exception as exc:
            if self.is_corrupt_board_db_error(exc):
                self.disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                logger.error(
                    "kanban dispatcher: board %s database %s is not a valid "
                    "SQLite database; pausing dispatch for this board until "
                    "the file changes, the gateway restarts, or the "
                    "quarantine timer expires. Move or restore the file, "
                    "then run `hermes kanban init` if you need a fresh board.",
                    slug, fingerprint[0],
                )
                return None
            logger.exception("kanban dispatcher: tick failed on board %s", slug)
            return None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    def tick_once(self) -> list[tuple[str, Optional[object]]]:
        """Run one dispatch_once per board. Returns (slug, result) pairs."""
        return [(slug, self.tick_once_for_board(slug)) for slug in self._board_slugs()]

    def ready_nonempty(self) -> bool:
        """Is there a ready+assigned+unclaimed task on ANY board the dispatcher would spawn for?

        Control-plane lanes (e.g. ``orion-cc``) are pulled by terminals via
        ``claim_task`` and never spawnable — a queue full of those is
        "correctly idle", not "stuck". The review column is probed only when
        review dispatch is on (same gate as the dispatcher): a task waiting
        for a human reviewer is idle, not stuck.
        """
        kbd = _kbd()
        _review_probe = kbd.review_dispatch_enabled()
        for slug in self._board_slugs():
            conn = None
            try:
                conn = _kbc().connect(board=slug)
                if kbd.has_spawnable_ready(conn) or (_review_probe and kbd.has_spawnable_review(conn)):
                    return True
            except Exception:
                continue
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
        return False

    def auto_decompose_tick(self, auto_decompose_per_tick: int) -> int:
        """Auto-decompose up to N triage tasks across all boards into ready workgraphs.

        Runs before dispatch fans out; the per-tick cap keeps a bulk triage
        load from burst-spending the aux LLM. Returns the number decomposed.
        """
        try:
            from hermes_cli import kanban_decompose as _decomp
        except Exception as exc:  # pragma: no cover
            logger.warning("kanban auto-decompose: import failed (%s); skipping", exc)
            return 0
        attempted = 0
        successes = 0
        for slug in self._board_slugs():
            if attempted >= auto_decompose_per_tick:
                break
            # Pin the board via env for the call: the decomposer connects
            # with no board kwarg (same pattern as the dashboard specify endpoint).
            prev_env = os.environ.get("HERMES_KANBAN_BOARD")
            try:
                os.environ["HERMES_KANBAN_BOARD"] = slug
                try:
                    triage_ids = _decomp.list_triage_ids()
                except Exception as exc:
                    logger.debug("kanban auto-decompose: list_triage_ids failed on board %s (%s)", slug, exc)
                    triage_ids = []
                for tid in triage_ids:
                    if attempted >= auto_decompose_per_tick:
                        break
                    attempted += 1
                    successes += self._decompose_one(_decomp, slug, tid)
            finally:
                if prev_env is None:
                    os.environ.pop("HERMES_KANBAN_BOARD", None)
                else:
                    os.environ["HERMES_KANBAN_BOARD"] = prev_env
        return successes

    @staticmethod
    def _decompose_one(_decomp: Any, slug: str, tid: str) -> int:
        """Decompose one triage task; returns 1 on success, 0 otherwise."""
        try:
            outcome = _decomp.decompose_task(tid, author="auto-decomposer")
        except Exception:
            logger.exception("kanban auto-decompose: decompose_task crashed on %s", tid)
            return 0
        if not outcome.ok:
            # Common no-op reasons (no aux client) must not spam logs every tick.
            logger.debug("kanban auto-decompose [%s]: %s skipped: %s", slug, tid, outcome.reason)
            return 0
        if outcome.fanout and outcome.child_ids:
            logger.info("kanban auto-decompose [%s]: %s → %d children", slug, tid, len(outcome.child_ids))
        else:
            logger.info("kanban auto-decompose [%s]: %s → single task (no fanout)", slug, tid)
        return 1


def _log_spawn_results(results: Optional[list]) -> bool:
    """Log per-board spawn summaries; returns whether any board spawned."""
    any_spawned = False
    for slug, res in (results or []):
        if res is not None and getattr(res, "spawned", None):
            any_spawned = True
            # Quiet by default: an idle gateway stays silent.
            logger.info(
                "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                slug, len(res.spawned), res.reclaimed,
                len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                res.promoted,
                len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
            )
    return any_spawned
