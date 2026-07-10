from __future__ import annotations

import json
import plistlib
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ops.cloudadvisor.hermes_ops.runtime import (
    RuntimeTarget,
    inventory,
    repoint_runtime_files,
)


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]]):
        self.responses = responses
        self.calls: list[Call] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        key = tuple(argv)
        self.calls.append(Call(key, Path(cwd)))
        returncode, stdout, stderr = self.responses.get(
            key,
            (1, "", f"unexpected command: {key}"),
        )
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class PlutilRunner:
    def __init__(self, *, fail_name: str | None = None):
        self.fail_name = fail_name
        self.calls: list[Call] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        self.calls.append(Call(tuple(argv), Path(cwd)))
        assert argv[:2] == ["plutil", "-lint"]
        failed = self.fail_name and self.fail_name in Path(argv[2]).name
        return subprocess.CompletedProcess(
            argv,
            1 if failed else 0,
            "",
            "invalid plist" if failed else "",
        )


def _runtime_fixture(tmp_path: Path):
    install_root = tmp_path / "hermes-agent"
    venv_python = install_root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(Path(sys.executable).resolve())

    home = tmp_path / "profiles" / "tradingastrid"
    manifest_path = home / "runtime" / "gateway.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {
        "source_sha": "deployed-sha",
        "executable": str(venv_python.resolve()),
        "python_version": sys.version.split()[0],
        "pid": 4321,
        "ppid": 1,
        "profile": "tradingastrid",
        "started_at": "2026-07-10T09:30:00+00:00",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    label = "ai.hermes.gateway-tradingastrid"
    plist_path = tmp_path / f"{label}.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": label,
                "ProgramArguments": [
                    str(venv_python),
                    "-m",
                    "hermes_cli.main",
                    "gateway",
                    "run",
                ],
                "EnvironmentVariables": {
                    "HERMES_HOME": str(home),
                    "VIRTUAL_ENV": str(install_root / ".venv"),
                },
            },
            handle,
        )

    target = RuntimeTarget(
        profile="tradingastrid",
        hermes_home=home,
        plist_path=plist_path,
    )
    responses = {
        ("git", "rev-parse", "HEAD"): (0, "deployed-sha\n", ""),
        ("launchctl", "print", f"gui/501/{label}"): (
            0,
            "state = running\n\tpid = 4321\n",
            "",
        ),
        ("ps", "-p", "4321", "-o", "command="): (
            0,
            f"{venv_python} -m hermes_cli.main gateway run\n",
            "",
        ),
    }
    return install_root, target, manifest_path, responses


def _checks(observation) -> dict[str, bool]:
    return {check.name: check.passed for check in observation.checks}


def test_inventory_is_healthy_only_when_all_runtime_identities_agree(tmp_path: Path):
    install_root, target, _, responses = _runtime_fixture(tmp_path)
    runner = FakeRunner(responses)

    observations = inventory(
        [target],
        install_root=install_root,
        uid=501,
        runner=runner,
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.healthy is True
    assert observation.launchd_label == "ai.hermes.gateway-tradingastrid"
    assert observation.launchd_pid == 4321
    assert observation.manifest_pid == 4321
    assert observation.process_command.endswith("hermes_cli.main gateway run")
    assert observation.expected_sha == "deployed-sha"
    assert all(_checks(observation).values())


def test_inventory_reports_every_identity_mismatch(tmp_path: Path):
    install_root, target, manifest_path, responses = _runtime_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({
        "source_sha": "wrong-sha",
        "executable": "/old/venv/bin/python",
        "pid": 9999,
        "profile": "wrong-profile",
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    observation = inventory(
        [target],
        install_root=install_root,
        uid=501,
        runner=FakeRunner(responses),
    )[0]

    checks = _checks(observation)
    assert observation.healthy is False
    assert checks["manifest_pid_matches_launchd"] is False
    assert checks["manifest_uses_expected_python"] is False
    assert checks["manifest_profile_matches"] is False
    assert checks["manifest_sha_matches_deploy"] is False


def test_inventory_is_unhealthy_when_launchd_or_manifest_is_missing(tmp_path: Path):
    install_root, target, manifest_path, responses = _runtime_fixture(tmp_path)
    manifest_path.unlink()
    label = "ai.hermes.gateway-tradingastrid"
    responses[("launchctl", "print", f"gui/501/{label}")] = (
        113,
        "",
        "Could not find service",
    )

    observation = inventory(
        [target],
        install_root=install_root,
        uid=501,
        runner=FakeRunner(responses),
    )[0]

    checks = _checks(observation)
    assert observation.healthy is False
    assert checks["launchd_owns_process"] is False
    assert checks["runtime_manifest_present"] is False


def _write_service_plist(path: Path, label: str, executable: Path, home: Path) -> None:
    with path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": label,
                "ProgramArguments": [str(executable), "gateway", "run"],
                "EnvironmentVariables": {
                    "HERMES_HOME": str(home),
                    "PATH": f"{executable.parent}:/usr/bin:/bin",
                    "VIRTUAL_ENV": str(executable.parent.parent),
                },
            },
            handle,
        )


def test_repoint_runtime_files_backs_up_then_moves_wrapper_and_plists_to_dot_venv(
    tmp_path: Path,
):
    install_root = tmp_path / "hermes-agent"
    old_hermes = install_root / "venv" / "bin" / "hermes"
    new_hermes = install_root / ".venv" / "bin" / "hermes"
    new_hermes.parent.mkdir(parents=True)
    new_hermes.write_text("new entry point\n", encoding="utf-8")
    wrapper = tmp_path / "bin" / "hermes"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\nexec "{old_hermes}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    gateway_plist = launch_agents / "ai.hermes.gateway.plist"
    dashboard_plist = launch_agents / "com.cloudadvisor.hermes-dashboard.plist"
    _write_service_plist(
        gateway_plist, "ai.hermes.gateway", old_hermes, tmp_path / "home"
    )
    _write_service_plist(
        dashboard_plist,
        "com.cloudadvisor.hermes-dashboard",
        old_hermes,
        tmp_path / "home",
    )
    runner = PlutilRunner()
    backup_dir = tmp_path / "rollback"

    result = repoint_runtime_files(
        install_root=install_root,
        wrapper_path=wrapper,
        plist_paths=[gateway_plist, dashboard_plist],
        backup_dir=backup_dir,
        runner=runner,
    )

    assert result.backup_dir == backup_dir
    assert result.changed_files == (wrapper, gateway_plist, dashboard_plist)
    assert str(new_hermes) in wrapper.read_text(encoding="utf-8")
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755
    for plist_path in (gateway_plist, dashboard_plist):
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        assert plist["ProgramArguments"][0] == str(new_hermes)
        assert str(install_root / ".venv") in plist["EnvironmentVariables"]["PATH"]
        assert plist["EnvironmentVariables"]["VIRTUAL_ENV"] == str(
            install_root / ".venv"
        )
        assert (backup_dir / plist_path.name).exists()
    assert str(old_hermes) in (backup_dir / "hermes.wrapper").read_text(
        encoding="utf-8"
    )
    assert len(runner.calls) == 2


def test_repoint_runtime_files_does_not_mutate_when_any_plist_fails_validation(
    tmp_path: Path,
):
    install_root = tmp_path / "hermes-agent"
    old_hermes = install_root / "venv" / "bin" / "hermes"
    new_hermes = install_root / ".venv" / "bin" / "hermes"
    new_hermes.parent.mkdir(parents=True)
    new_hermes.write_text("new entry point\n", encoding="utf-8")
    wrapper = tmp_path / "hermes"
    wrapper.write_text(f'exec "{old_hermes}" "$@"\n', encoding="utf-8")
    plist_path = tmp_path / "bad-service.plist"
    _write_service_plist(plist_path, "ai.hermes.gateway", old_hermes, tmp_path / "home")
    wrapper_before = wrapper.read_bytes()
    plist_before = plist_path.read_bytes()

    try:
        repoint_runtime_files(
            install_root=install_root,
            wrapper_path=wrapper,
            plist_paths=[plist_path],
            backup_dir=tmp_path / "rollback",
            runner=PlutilRunner(fail_name="bad-service"),
        )
    except RuntimeError as exc:
        assert "invalid plist" in str(exc)
    else:
        raise AssertionError("expected plist validation to fail")

    assert wrapper.read_bytes() == wrapper_before
    assert plist_path.read_bytes() == plist_before
