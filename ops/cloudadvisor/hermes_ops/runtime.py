"""Inventory and guarded file reconciliation for installed Hermes runtimes."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .command import CommandRunner
from .health import (
    HealthCheck,
    HealthReport,
    combine_health_reports,
    evaluate_runtime_health,
)


@dataclass(frozen=True)
class RuntimeTarget:
    profile: str
    hermes_home: Path
    plist_path: Path


@dataclass(frozen=True)
class RuntimeCheck:
    name: str
    passed: bool
    expected: object = None
    actual: object = None


@dataclass(frozen=True)
class RuntimeObservation:
    profile: str
    hermes_home: Path
    plist_path: Path
    launchd_label: str | None
    launchd_pid: int | None
    manifest_pid: int | None
    process_command: str | None
    process_executable: str | None
    expected_sha: str | None
    healthy: bool
    checks: tuple[RuntimeCheck, ...]


@dataclass(frozen=True)
class RuntimeRepointResult:
    backup_dir: Path
    changed_files: tuple[Path, ...]


@dataclass(frozen=True)
class LaunchdService:
    label: str
    plist_path: Path


@dataclass(frozen=True)
class ServiceObservation:
    label: str
    loaded: bool
    pid: int | None
    command: str | None
    plist_executable: str | None
    plist_uses_dot_venv: bool
    process_uses_dot_venv: bool

    @property
    def healthy(self) -> bool:
        return bool(
            self.loaded
            and self.pid is not None
            and self.command
            and self.plist_uses_dot_venv
            and self.process_uses_dot_venv
        )


def _service_health_check(
    label: str,
    observation: ServiceObservation | None,
) -> HealthCheck:
    passed = bool(observation and observation.healthy)
    return HealthCheck(
        name=f"service:{label}",
        passed=passed,
        detail=(
            "launchd owner, plist, command, and .venv agree"
            if passed
            else "service missing or launchd, plist, command, and .venv disagree"
        ),
    )


class LaunchdServiceController:
    """Stop/start only the explicitly configured launchd service set."""

    def __init__(
        self,
        *,
        services: Iterable[LaunchdService],
        install_root: Path,
        uid: int,
        runner: CommandRunner,
        stop_timeout_seconds: float = 30,
        poll_interval_seconds: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.services = {service.label: service for service in services}
        self.install_root = Path(install_root).expanduser().resolve(strict=False)
        self.uid = int(uid)
        self.runner = runner
        self.stop_timeout_seconds = max(0.0, float(stop_timeout_seconds))
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.clock = clock
        self.sleeper = sleeper

    def _domain_target(self, label: str) -> str:
        return f"gui/{self.uid}/{label}"

    def _required(self, argv: list[str]) -> None:
        completed = self.runner.run(argv, cwd=self.install_root, timeout=120)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"command failed ({' '.join(argv)}): {detail}")

    def inventory(self) -> tuple[ServiceObservation, ...]:
        observations = []
        for service in self.services.values():
            plist = _load_plist(service.plist_path)
            arguments = plist.get("ProgramArguments") if plist else None
            arguments = arguments if isinstance(arguments, list) else []
            plist_executable = arguments[0] if arguments else None
            launchd_result = self.runner.run(
                ["launchctl", "print", self._domain_target(service.label)],
                cwd=self.install_root,
                timeout=30,
            )
            loaded = launchd_result.returncode == 0
            launchd_output = (
                (launchd_result.stdout or "").strip() or None if loaded else None
            )
            pid = _launchd_pid(launchd_output)
            command = None
            if pid is not None:
                command = _completed_stdout(
                    self.runner,
                    ["ps", "-p", str(pid), "-o", "command="],
                    cwd=self.install_root,
                )
            approved_executable = _managed_python_executable(
                plist_executable, self.install_root
            )
            process_executable = _lexical_path(_command_executable(command))
            observations.append(
                ServiceObservation(
                    label=service.label,
                    loaded=loaded,
                    pid=pid,
                    command=command,
                    plist_executable=(
                        str(plist_executable) if plist_executable is not None else None
                    ),
                    plist_uses_dot_venv=approved_executable is not None,
                    process_uses_dot_venv=(
                        approved_executable is not None
                        and process_executable == approved_executable
                    ),
                )
            )
        return tuple(observations)

    def loaded_services(self) -> tuple[str, ...]:
        return tuple(
            observation.label for observation in self.inventory() if observation.loaded
        )

    def running_services(self) -> tuple[str, ...]:
        return tuple(
            observation.label
            for observation in self.inventory()
            if observation.pid is not None
        )

    def stop(self, services: tuple[str, ...]) -> None:
        for label in services:
            if label not in self.services:
                raise RuntimeError(f"refusing to stop unconfigured service: {label}")
            self._required(["launchctl", "bootout", self._domain_target(label)])
            deadline = self.clock() + self.stop_timeout_seconds
            while True:
                completed = self.runner.run(
                    ["launchctl", "print", self._domain_target(label)],
                    cwd=self.install_root,
                    timeout=30,
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout or "").strip()
                    if (
                        completed.returncode == 113
                        or "could not find service" in detail.lower()
                    ):
                        break
                    raise RuntimeError(
                        f"could not verify launchd unload for {label}: {detail}"
                    )
                now = self.clock()
                if now >= deadline:
                    raise RuntimeError(
                        f"timed out waiting for launchd service to unload: {label}"
                    )
                self.sleeper(min(self.poll_interval_seconds, deadline - now))

    def start(self, services: tuple[str, ...]) -> None:
        for label in services:
            service = self.services.get(label)
            if service is None:
                raise RuntimeError(f"refusing to start unconfigured service: {label}")
            self._required(["plutil", "-lint", str(service.plist_path)])
            self._required([
                "launchctl",
                "bootstrap",
                f"gui/{self.uid}",
                str(service.plist_path),
            ])
            self._required(["launchctl", "kickstart", self._domain_target(label)])


class RuntimeHealthChecker:
    """Production deploy health matrix for launchd services and gateways."""

    def __init__(
        self,
        *,
        controller: LaunchdServiceController,
        gateway_targets: Iterable[RuntimeTarget],
        install_root: Path,
        uid: int,
        runner: CommandRunner,
        inject_failure: str | None = None,
        timeout_seconds: float = 30,
        poll_interval_seconds: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.controller = controller
        self.gateway_targets = tuple(gateway_targets)
        self.install_root = Path(install_root)
        self.uid = int(uid)
        self.runner = runner
        self.inject_failure = inject_failure
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.clock = clock
        self.sleeper = sleeper

    def check(
        self,
        *,
        expected_sha: str,
        services: tuple[str, ...],
        identity_required: bool = True,
        apply_injection: bool = True,
    ) -> HealthReport:
        deadline = self.clock() + self.timeout_seconds
        while True:
            report = self._check_once(
                expected_sha=expected_sha,
                services=services,
                identity_required=identity_required,
            )
            now = self.clock()
            if report.healthy or now >= deadline:
                break
            self.sleeper(min(self.poll_interval_seconds, deadline - now))

        pending_injection = self.inject_failure
        self.inject_failure = None
        if apply_injection and pending_injection == "after_restart":
            report = combine_health_reports(
                report,
                HealthReport(
                    checks=(
                        HealthCheck(
                            "injected:after_restart",
                            False,
                            "recovery canary failure injection",
                        ),
                    )
                ),
            )
        return report

    def _check_once(
        self,
        *,
        expected_sha: str,
        services: tuple[str, ...],
        identity_required: bool,
    ) -> HealthReport:
        expected_services = set(services)
        service_observations = {
            observation.label: observation
            for observation in self.controller.inventory()
        }
        service_report = HealthReport(
            checks=tuple(
                _service_health_check(label, service_observations.get(label))
                for label in sorted(expected_services)
            )
        )

        configuration_report = HealthReport(
            checks=(
                HealthCheck(
                    "runtime:gateway_targets_configured",
                    bool(self.gateway_targets),
                    (
                        "gateway targets configured"
                        if self.gateway_targets
                        else "no gateway runtime target configured"
                    ),
                ),
            )
        )
        gateway_observations = inventory(
            self.gateway_targets,
            install_root=self.install_root,
            expected_sha=expected_sha,
            uid=self.uid,
            runner=self.runner,
            identity_required=identity_required,
        )
        runtime_report = evaluate_runtime_health(
            gateway_observations,
            expected_profiles=(target.profile for target in self.gateway_targets),
            identity_required=identity_required,
        )
        return combine_health_reports(
            service_report,
            configuration_report,
            runtime_report,
        )


def _completed_stdout(
    runner: CommandRunner,
    argv: list[str],
    *,
    cwd: Path,
) -> str | None:
    completed = runner.run(argv, cwd=cwd, timeout=30)
    if completed.returncode != 0:
        return None
    output = (completed.stdout or "").strip()
    return output or None


def _load_plist(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return None
    return payload if isinstance(payload, dict) else None


def _load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _integer(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _launchd_pid(output: str | None) -> int | None:
    if not output:
        return None
    match = re.search(r"(?:^|\s)pid\s*=\s*(\d+)(?:\s|$)", output)
    return int(match.group(1)) if match else None


def _command_executable(command: str | None) -> str | None:
    if not command:
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else None


def _resolved(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return str(Path(value).expanduser().resolve(strict=False))


def _lexical_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(os.path.abspath(os.path.expanduser(value)))


def _managed_python_executables(install_root: Path) -> tuple[Path, ...]:
    """The only trusted interpreter identities for an install: ``bin/python``
    directly under a top-level ``.venv`` or ``venv`` directory.

    A sibling executable (``python3``, ``pip``) or any other descendant
    nested under the venv root is never trusted, however plausible its
    location - identity is exact, not lexical containment.
    """
    root = Path(install_root).expanduser().resolve(strict=False)
    return tuple(
        root / venv_name / "bin" / "python" for venv_name in (".venv", "venv")
    )


def _managed_python_executable(
    value: object,
    install_root: Path,
) -> Path | None:
    candidate = _lexical_path(value)
    if candidate in _managed_python_executables(install_root):
        return candidate
    return None


def _check(
    name: str,
    passed: bool,
    *,
    expected: object = None,
    actual: object = None,
) -> RuntimeCheck:
    return RuntimeCheck(name, bool(passed), expected, actual)


def inventory(
    targets: Iterable[RuntimeTarget],
    *,
    install_root: Path,
    expected_sha: str,
    uid: int,
    runner: CommandRunner,
    identity_required: bool = True,
) -> tuple[RuntimeObservation, ...]:
    """Compare launchd, plist, process, Git, and runtime-manifest identity."""
    root = Path(install_root).expanduser().resolve(strict=False)
    expected_sha = str(expected_sha).strip()
    if not expected_sha:
        raise ValueError("expected_sha must come from an immutable deployment record")
    observations: list[RuntimeObservation] = []

    for target in targets:
        home = Path(target.hermes_home).expanduser().resolve(strict=False)
        plist_path = Path(target.plist_path).expanduser().resolve(strict=False)
        plist = _load_plist(plist_path)
        label = plist.get("Label") if plist else None
        label = label if isinstance(label, str) and label else None
        arguments = plist.get("ProgramArguments") if plist else None
        arguments = arguments if isinstance(arguments, list) else []
        plist_executable = arguments[0] if arguments else None
        expected_python = _managed_python_executable(plist_executable, root)
        expected_python_resolved = (
            str(expected_python.resolve(strict=False))
            if expected_python is not None
            else None
        )
        environment = plist.get("EnvironmentVariables") if plist else None
        environment = environment if isinstance(environment, dict) else {}
        plist_home = _resolved(environment.get("HERMES_HOME"))

        launchd_output = None
        if label:
            launchd_output = _completed_stdout(
                runner,
                ["launchctl", "print", f"gui/{uid}/{label}"],
                cwd=root,
            )
        launchd_pid = _launchd_pid(launchd_output)

        process_command = None
        if launchd_pid is not None:
            process_command = _completed_stdout(
                runner,
                ["ps", "-p", str(launchd_pid), "-o", "command="],
                cwd=root,
            )
        process_executable = _command_executable(process_command)

        manifest = _load_manifest(home / "runtime" / "gateway.json")
        manifest_pid = _integer(manifest.get("pid")) if manifest else None
        manifest_executable = (
            _resolved(manifest.get("executable")) if manifest else None
        )
        manifest_profile = manifest.get("profile") if manifest else None
        manifest_sha = manifest.get("source_sha") if manifest else None

        checks = (
            _check("plist_present", plist is not None, actual=str(plist_path)),
            _check("launchd_label_present", label is not None, actual=label),
            _check(
                "plist_profile_home_matches",
                plist_home == str(home),
                expected=str(home),
                actual=plist_home,
            ),
            _check(
                "plist_uses_dot_venv",
                expected_python is not None,
                expected=tuple(str(path) for path in _managed_python_executables(root)),
                actual=plist_executable,
            ),
            _check(
                "launchd_owns_process",
                launchd_pid is not None,
                actual=launchd_pid,
            ),
            _check("runtime_manifest_present", manifest is not None),
            _check(
                "manifest_pid_matches_launchd",
                manifest_pid is not None and manifest_pid == launchd_pid,
                expected=launchd_pid,
                actual=manifest_pid,
            ),
            _check(
                "manifest_uses_expected_python",
                expected_python_resolved is not None
                and manifest_executable == expected_python_resolved,
                expected=expected_python_resolved,
                actual=manifest_executable,
            ),
            _check(
                "manifest_profile_matches",
                manifest_profile == target.profile,
                expected=target.profile,
                actual=manifest_profile,
            ),
            _check(
                "manifest_sha_matches_deploy",
                expected_sha is not None and manifest_sha == expected_sha,
                expected=expected_sha,
                actual=manifest_sha,
            ),
            _check(
                "process_command_readable",
                process_command is not None,
                actual=process_command,
            ),
            _check(
                "process_uses_expected_python",
                expected_python is not None
                and _lexical_path(process_executable) == expected_python,
                expected=str(expected_python) if expected_python is not None else None,
                actual=process_executable,
            ),
        )
        required_checks = (
            checks
            if identity_required
            else tuple(
                check
                for check in checks
                if check.name != "runtime_manifest_present"
                and not check.name.startswith("manifest_")
            )
        )
        observations.append(
            RuntimeObservation(
                profile=target.profile,
                hermes_home=home,
                plist_path=plist_path,
                launchd_label=label,
                launchd_pid=launchd_pid,
                manifest_pid=manifest_pid,
                process_command=process_command,
                process_executable=process_executable,
                expected_sha=expected_sha,
                healthy=all(check.passed for check in required_checks),
                checks=checks,
            )
        )

    return tuple(observations)


def _replace_path(value: object, old: str, new: str) -> object:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace_path(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_path(item, old, new) for key, item in value.items()}
    return value


def _temporary_file(path: Path, data: bytes, mode: int) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def repoint_runtime_files(
    *,
    install_root: Path,
    wrapper_path: Path,
    plist_paths: Iterable[Path],
    backup_dir: Path,
    runner: CommandRunner,
) -> RuntimeRepointResult:
    """Back up and repoint operational entry points without loading services."""
    root = Path(install_root).expanduser().resolve(strict=False)
    wrapper = Path(wrapper_path).expanduser().resolve(strict=False)
    plists = tuple(
        Path(path).expanduser().resolve(strict=False) for path in plist_paths
    )
    rollback = Path(backup_dir).expanduser().resolve(strict=False)
    old_venv = str(root / "venv")
    new_venv = str(root / ".venv")
    new_hermes = root / ".venv" / "bin" / "hermes"
    if not new_hermes.exists():
        raise FileNotFoundError(f"expected Hermes entry point is missing: {new_hermes}")
    if not wrapper.is_file():
        raise FileNotFoundError(f"Hermes wrapper is missing: {wrapper}")
    missing_plists = [path for path in plists if not path.is_file()]
    if missing_plists:
        raise FileNotFoundError(f"LaunchAgent plist is missing: {missing_plists[0]}")

    rollback.mkdir(parents=True, exist_ok=False)
    shutil.copy2(wrapper, rollback / "hermes.wrapper")
    for plist_path in plists:
        shutil.copy2(plist_path, rollback / plist_path.name)

    wrapper_data = (
        "#!/usr/bin/env bash\n"
        "unset PYTHONPATH\n"
        "unset PYTHONHOME\n"
        f'exec "{new_hermes}" "$@"\n'
    ).encode("utf-8")
    wrapper_changed = wrapper.read_bytes() != wrapper_data
    temporary_files: dict[Path, Path] = {}
    changed_plists: list[Path] = []

    try:
        if wrapper_changed:
            temporary_files[wrapper] = _temporary_file(wrapper, wrapper_data, 0o755)

        for plist_path in plists:
            plist = _load_plist(plist_path)
            if plist is None:
                raise RuntimeError(f"could not parse LaunchAgent plist: {plist_path}")
            updated = _replace_path(plist, old_venv, new_venv)
            if updated != plist:
                data = plistlib.dumps(updated, fmt=plistlib.FMT_XML, sort_keys=False)
                candidate = _temporary_file(plist_path, data, 0o644)
                temporary_files[plist_path] = candidate
                changed_plists.append(plist_path)
            else:
                candidate = plist_path

            completed = runner.run(
                ["plutil", "-lint", str(candidate)],
                cwd=root,
                timeout=30,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(
                    f"plist validation failed for {plist_path}: "
                    f"{detail or 'plutil returned non-zero'}"
                )

        changed_files = ([wrapper] if wrapper_changed else []) + changed_plists
        for destination in changed_files:
            os.replace(temporary_files[destination], destination)
            temporary_files.pop(destination, None)
        return RuntimeRepointResult(
            backup_dir=rollback,
            changed_files=tuple(changed_files),
        )
    finally:
        for temporary in temporary_files.values():
            temporary.unlink(missing_ok=True)
