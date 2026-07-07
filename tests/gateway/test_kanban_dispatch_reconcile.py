"""Tests for wiring reconcile() into the live gateway dispatch tick (CR4).

``spawn_after_handoff``/``reconcile`` exist as ``hermes_cli.kanban_db`` library
functions with their own tests, but the running gateway's per-board dispatch
tick only ever called ``dispatch_once`` — story->epic integration and the
bounded v2 safety net never ran live. ``dispatch_once`` already drives v2
spawn + crash recovery on its own (ready-queue claim-CAS, stale-worker
reclaim), so the genuinely missing piece is ``reconcile()``'s story
integration pass; this wires that in for handoff_v2 boards only, immediately
after ``dispatch_once`` on the same connection.

``_tick_once_for_board`` is a closure nested inside the giant
``_kanban_dispatcher_watcher`` coroutine (config load, singleton file lock,
sleep loop) and isn't reachable from a test without a lot of unrelated setup.
Following the existing precedent in this file (``_resolve_auto_decompose_settings``,
covered by ``test_kanban_auto_decompose_live.py``), the actual seam —
"run dispatch_once, then reconcile for v2 boards, defensively" — is lifted to
a small module-level helper that the closure calls, and tested directly here.
"""

from __future__ import annotations

from types import SimpleNamespace

from gateway.kanban_watchers import _dispatch_once_then_reconcile


def _fake_kb(*, v2_enabled: bool, dispatch_result=object(), reconcile_side_effect=None):
    """Build a stand-in for the ``hermes_cli.kanban_db`` module used inside
    the dispatcher closure, recording call order via a shared list."""
    calls: list[tuple] = []

    def dispatch_once(conn, *, board, **kwargs):
        calls.append(("dispatch_once", conn, board))
        return dispatch_result

    def product_board_metadata(slug):
        calls.append(("product_board_metadata", slug))
        return {"handoff_v2": v2_enabled}

    def _handoff_v2_enabled(meta):
        return v2_enabled

    def reconcile(conn, *, board):
        calls.append(("reconcile", conn, board))
        if reconcile_side_effect is not None:
            raise reconcile_side_effect

    kb = SimpleNamespace(
        dispatch_once=dispatch_once,
        product_board_metadata=product_board_metadata,
        _handoff_v2_enabled=_handoff_v2_enabled,
        reconcile=reconcile,
    )
    return kb, calls


def test_v2_board_calls_reconcile_after_dispatch_once():
    kb, calls = _fake_kb(v2_enabled=True)
    conn = object()

    result = _dispatch_once_then_reconcile(kb, conn, "epics", max_spawn=5)

    kinds = [c[0] for c in calls]
    assert kinds.index("dispatch_once") < kinds.index("reconcile"), (
        "reconcile must run after dispatch_once, not before/instead"
    )
    reconcile_call = next(c for c in calls if c[0] == "reconcile")
    assert reconcile_call == ("reconcile", conn, "epics"), (
        "reconcile must run on the SAME conn dispatch_once used, for the same board"
    )


def test_legacy_board_never_calls_reconcile():
    kb, calls = _fake_kb(v2_enabled=False)
    conn = object()

    _dispatch_once_then_reconcile(kb, conn, "legacy-board", max_spawn=5)

    kinds = [c[0] for c in calls]
    assert "dispatch_once" in kinds
    assert "reconcile" not in kinds, "legacy boards must only call dispatch_once"


def test_reconcile_exception_does_not_break_tick_or_result():
    sentinel_result = SimpleNamespace(spawned=3)
    kb, calls = _fake_kb(
        v2_enabled=True,
        dispatch_result=sentinel_result,
        reconcile_side_effect=RuntimeError("reconcile blew up"),
    )
    conn = object()

    # Must not raise.
    result = _dispatch_once_then_reconcile(kb, conn, "epics", max_spawn=5)

    assert result is sentinel_result, (
        "dispatch_once's result must still be returned even if reconcile raises"
    )
    assert any(c[0] == "reconcile" for c in calls), "reconcile should still have been attempted"


def test_dispatch_once_result_returned_unchanged_for_v2_board():
    sentinel_result = SimpleNamespace(spawned=1)
    kb, _calls = _fake_kb(v2_enabled=True, dispatch_result=sentinel_result)
    conn = object()

    result = _dispatch_once_then_reconcile(kb, conn, "epics", max_spawn=5)

    assert result is sentinel_result
