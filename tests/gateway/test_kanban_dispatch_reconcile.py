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

    def reconcile(conn, *, board, spawn_ready=True):
        calls.append(("reconcile", conn, board, spawn_ready))
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
    assert reconcile_call == ("reconcile", conn, "epics", False), (
        "reconcile must run on the SAME conn dispatch_once used, for the same "
        "board, with spawn_ready=False -- dispatch_once is the tick's sole "
        "capped spawn owner (Codex re-review P1)"
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


# ---------------------------------------------------------------------------
# Real-kanban_db regression test (Codex re-review P1): the gateway tick must
# never spawn past max_spawn.
#
# ``dispatch_once`` honors ``max_spawn`` (a live concurrency cap), but
# ``reconcile()``'s ready-spawn step (step 2) spawned EVERY ready+assigned
# card with no cap awareness. Wiring reconcile() in after dispatch_once
# (CR4) therefore let the tick over-spawn past max_spawn: on a v2 board with
# 2 ready cards and max_spawn=1, dispatch_once spawns 1 (honoring the cap),
# then reconcile spawned the 2nd.
#
# The CR4 test above uses a fully mocked ``kb`` module (asserts call order
# only) and cannot see this -- it never runs the real dispatch_once/reconcile
# interaction with caps. This test uses the REAL ``hermes_cli.kanban_db``
# (real dispatch_once + real reconcile) with only the process-spawn side
# effect faked out, per Codex's explicit ask.
# ---------------------------------------------------------------------------

def _v2_product_board(name: str) -> None:
    """Create a product-preset board with the ``handoff_v2`` opt-in flag set.

    Mirrors ``tests/hermes_cli/test_kanban_db.py``'s helper of the same
    name -- duplicated here (rather than imported) to keep this file's
    real-kanban_db test self-contained and independent of that file's
    module-level fixtures.
    """
    import json as _json

    from hermes_cli import kanban_db as _kb

    _kb.create_board(name, name="V2 Board", preset="product")
    meta_path = _kb.board_metadata_path(name)
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
    meta.setdefault("product_workflow", {})["handoff_v2"] = True
    meta_path.write_text(_json.dumps(meta), encoding="utf-8")


def test_gateway_tick_real_kanban_db_never_spawns_past_max_spawn(monkeypatch):
    """Regression: with 2 ready+assigned cards and max_spawn=1, the gateway
    tick (dispatch_once then reconcile) must spawn exactly ONE card, not
    two. Without the fix (reconcile's ready-spawn step running unconditionally
    after dispatch_once), this spawns 2.
    """
    from hermes_cli import kanban_db as real_kb
    from hermes_cli import profiles

    board = "v2-gateway-max-spawn"
    _v2_product_board(board)

    # The profile-existence gate only applies inside dispatch_once's own
    # ready-loop (not reconcile's _spawn_one_v2); patch it so dispatch_once
    # actually spawns its one permitted card instead of bucketing it as
    # non-spawnable.
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)

    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    # Both dispatch_once and reconcile fall back to this module-level
    # default spawner whenever no explicit spawn_fn is supplied -- patching
    # it here means the fake applies uniformly to whichever of the two
    # actually ends up spawning a given card, without depending on whether
    # the gateway wrapper threads a spawn_fn through to reconcile.
    monkeypatch.setattr(real_kb, "_default_spawn", fake_spawn)

    with real_kb.connect(board=board) as conn:
        t1 = real_kb.create_task(conn, title="Story 1", assignee="developer")
        t2 = real_kb.create_task(conn, title="Story 2", assignee="developer")

        _dispatch_once_then_reconcile(real_kb, conn, board, max_spawn=1)

        running = [
            tid for tid in (t1, t2)
            if real_kb.get_task(conn, tid).status == "running"
        ]

    assert len(running) == 1, (
        f"expected exactly ONE card running under max_spawn=1, got {running} "
        f"running (spawns recorded: {spawns})"
    )
    assert len(spawns) == 1
