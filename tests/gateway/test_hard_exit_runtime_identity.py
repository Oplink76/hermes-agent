"""The hard-exit funnel must perform cleanup that atexit cannot perform."""
import pytest


@pytest.mark.parametrize("identity_cleanup_fails", [False, True])
def test_hard_exit_releases_runtime_identity_and_locks(monkeypatch, identity_cleanup_fails):
    from gateway import run

    events = []

    def remove_identity():
        events.append("identity")
        if identity_cleanup_fails:
            raise OSError("test cleanup failure")

    monkeypatch.setattr("gateway.runtime_identity.remove_runtime_identity", remove_identity)
    monkeypatch.setattr("gateway.status.remove_pid_file", lambda: events.append("pid"))
    monkeypatch.setattr("gateway.status.release_gateway_runtime_lock", lambda: events.append("lock"))
    monkeypatch.setattr("gateway.lifecycle_ledger.mark_exited", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_logging.drain_log_queue", lambda **kw: None)
    monkeypatch.setattr(run.os, "_exit", lambda code: events.append("exit"))
    run._exit_after_graceful_shutdown(0)
    assert events == ["identity", "pid", "lock", "exit"]
