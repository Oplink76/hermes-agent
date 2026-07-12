from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.upstream_sync_status import SyncStatus


def _write_status(
    path: Path,
    *,
    upstream_behind: int,
    sync_state: str = "PR_UPDATED",
) -> None:
    SyncStatus(
        schema_version=1,
        checked_at=datetime.now(timezone.utc).isoformat(),
        upstream_behind=upstream_behind,
        fork_behind=0,
        sync_state=sync_state,
        sync_pr_number=7,
        required_check="All required checks pass",
        fork_main_sha="a" * 40,
        installed_sha="a" * 40,
        escalation_fingerprint=None,
    ).write(path)


def _client() -> TestClient:
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return client


def test_installed_current_does_not_hide_upstream_backlog(
    tmp_path: Path, monkeypatch
) -> None:
    status_file = tmp_path / "sync-status.json"
    _write_status(status_file, upstream_behind=54)
    monkeypatch.setattr(web_server, "_upstream_sync_status_path", lambda: status_file)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 0)
    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path)

    payload = _client().get("/api/hermes/update/check?force=true").json()

    assert payload["behind"] == 0
    assert payload["fork_behind"] == 0
    assert payload["update_available"] is False
    assert payload["upstream_behind"] == 54
    assert payload["sync_state"] == "PR_UPDATED"
    assert payload["sync_pr_number"] == 7
    assert payload["sync_required_check"] == "All required checks pass"
    assert payload["installed_sha"] == "a" * 40


def test_missing_status_file_keeps_existing_update_contract(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        web_server,
        "_upstream_sync_status_path",
        lambda: tmp_path / "missing.json",
    )
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 3)
    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path)

    payload = _client().get("/api/hermes/update/check").json()

    assert payload["behind"] == 3
    assert payload["fork_behind"] == 3
    assert payload["update_available"] is True
    assert payload["upstream_behind"] is None
    assert payload["sync_state"] is None


def test_needs_ole_status_does_not_claim_upstream_is_syncing(
    tmp_path: Path, monkeypatch
) -> None:
    status_file = tmp_path / "sync-status.json"
    _write_status(status_file, upstream_behind=54, sync_state="NEEDS_OLE")
    monkeypatch.setattr(web_server, "_upstream_sync_status_path", lambda: status_file)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 0)

    payload = _client().get("/api/hermes/update/check").json()

    assert payload["message"] == (
        "Installed current · Official upstream sync needs attention"
    )
    assert "syncing" not in payload["message"]
    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["sync_update_blocked"] is True


def test_safe_rollback_suppresses_update_action_and_shows_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    status_file = tmp_path / "sync-status.json"
    _write_status(
        status_file,
        upstream_behind=2,
        sync_state="ROLLED_BACK_REVERTED",
    )
    monkeypatch.setattr(web_server, "_upstream_sync_status_path", lambda: status_file)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    monkeypatch.setattr(web_server, "detect_install_method", lambda root: "git")
    monkeypatch.setattr("hermes_cli.banner.check_for_updates", lambda: 1)

    payload = _client().get("/api/hermes/update/check").json()

    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["sync_update_blocked"] is True
    assert "recovery active after safe rollback" in payload["message"]


def test_needs_ole_blocks_direct_update_endpoint(
    tmp_path: Path, monkeypatch
) -> None:
    status_file = tmp_path / "sync-status.json"
    _write_status(status_file, upstream_behind=2, sync_state="NEEDS_OLE")
    monkeypatch.setattr(web_server, "_upstream_sync_status_path", lambda: status_file)
    monkeypatch.setattr(
        web_server, "_dashboard_local_update_managed_externally", lambda: False
    )
    spawned: list[object] = []
    monkeypatch.setattr(
        web_server,
        "_spawn_hermes_action",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    payload = _client().post("/api/hermes/update").json()

    assert payload["ok"] is False
    assert payload["error"] == "upstream_sync_update_blocked"
    assert spawned == []
