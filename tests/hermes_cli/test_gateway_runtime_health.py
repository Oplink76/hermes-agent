from datetime import datetime, timedelta, timezone

from hermes_cli.gateway import _runtime_health_lines
from hermes_cli import doctor_platform as doctor


def _iso_age(seconds_ago: float) -> str:
    """ISO-8601 UTC timestamp ``seconds_ago`` in the past (drives _marker_is_stale)."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


_STALE_LINE_PREFIX = "⚠ Stale gateway_state.json:"


def _stale_lines(lines):
    return [ln for ln in lines if ln.startswith(_STALE_LINE_PREFIX)]


def test_runtime_health_lines_flags_stale_running_with_dead_pid(monkeypatch):
    """Stale updated_at + dead PID + 'running' -> contradiction line, no draining line."""
    from gateway import status as status_mod

    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "pid": 4242,
            "start_time": 111,
            "updated_at": _iso_age(600),  # well past the 120s TTL -> stale
            "active_agents": 0,
        },
    )
    # Recorded PID is gone (ungraceful kill); no real process is touched.
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: None)

    lines = _runtime_health_lines()

    stale = _stale_lines(lines)
    assert len(stale) == 1, lines
    assert "recorded state 'running'" in stale[0]
    assert "recorded process is gone" in stale[0]
    # The misleading live-state summary must be suppressed.
    assert not any("draining" in ln.lower() for ln in lines), lines


def test_runtime_health_lines_include_fatal_platform_and_startup_reason(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "startup_failed",
            "exit_reason": "telegram conflict",
            "platforms": {
                "telegram": {
                    "state": "fatal",
                    "error_message": "another poller is active",
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ telegram: another poller is active" in lines
    assert "⚠ Last startup issue: telegram conflict" in lines


def test_runtime_health_lines_include_observable_process_identity(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {},
            "runtime_identity": {
                "source_sha": "1234567890abcdef",
                "executable": "/opt/hermes/.venv/bin/python",
                "python_version": "3.12.13",
                "pid": 4321,
                "ppid": 1,
                "profile": "tradingastrid",
                "started_at": "2026-07-10T09:30:00+00:00",
            },
        },
    )

    lines = _runtime_health_lines()

    assert (
        "Runtime: profile=tradingastrid pid=4321 sha=1234567890ab "
        "python=3.12.13 executable=/opt/hermes/.venv/bin/python"
    ) in lines


def test_doctor_reports_runtime_identity_for_the_live_gateway(monkeypatch):
    events = []
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kwargs: 4321)
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "runtime_identity": {
                "source_sha": "1234567890abcdef",
                "executable": "/opt/hermes/.venv/bin/python",
                "python_version": "3.12.13",
                "pid": 4321,
                "profile": "tradingastrid",
            }
        },
    )
    monkeypatch.setattr(
        doctor, "_section", lambda title: events.append(("section", title))
    )
    monkeypatch.setattr(
        doctor,
        "check_ok",
        lambda text, detail="": events.append(("ok", text, detail)),
    )
    monkeypatch.setattr(
        doctor, "check_info", lambda text: events.append(("info", text))
    )
    monkeypatch.setattr(
        doctor,
        "check_warn",
        lambda text, detail="": events.append(("warn", text, detail)),
    )
    finding = doctor._check_gateway_runtime_identity(False)
    issues = finding.manual_issues

    assert issues == []
    assert ("section", "Gateway Runtime Identity") in events
    assert any(
        event[:2] == ("ok", "Runtime identity matches live PID 4321")
        for event in events
    )
    assert any("sha=1234567890ab" in event[1] for event in events if event[0] == "info")


def test_doctor_warns_when_a_live_gateway_has_no_runtime_identity(monkeypatch):
    warnings = []
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kwargs: 4321)
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {"gateway_state": "running"},
    )
    monkeypatch.setattr(doctor, "_section", lambda title: None)
    monkeypatch.setattr(
        doctor,
        "check_warn",
        lambda text, detail="": warnings.append((text, detail)),
    )
    finding = doctor._check_gateway_runtime_identity(False)
    issues = finding.manual_issues

    assert warnings
    assert issues == ["Restart the gateway so it publishes runtime identity"]


def test_runtime_status_running_pid_validates_live_gateway_record(monkeypatch):
    from gateway import status as status_mod

    runtime = {
        "pid": 12345,
        "kind": "hermes-gateway",
        "argv": ["/opt/hermes/hermes_cli/main.py", "gateway", "run", "--replace"],
        "start_time": None,
        "gateway_state": "running",
    }
    monkeypatch.setattr(status_mod, "_pid_exists", lambda pid: pid == 12345)
    monkeypatch.setattr(status_mod, "_get_process_start_time", lambda pid: None)
    monkeypatch.setattr(status_mod, "_looks_like_gateway_process", lambda pid: False)

    assert status_mod.get_runtime_status_running_pid(runtime) == 12345


