"""Command-line boundary for CloudAdvisor Hermes operations."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .command import CommandRunner, SubprocessCommandRunner
from .deploy import (
    DeployConfig,
    DeployRequest,
    GhReleaseVerifier,
    PreflightError,
    deploy as run_deploy,
)
from .runtime import (
    LaunchdService,
    LaunchdServiceController,
    RuntimeHealthChecker,
    RuntimeTarget,
)
from .snapshot import SnapshotCoordinator
from .sync import (
    CodexConflictResolver,
    SyncConfig,
    SyncResult,
    SyncState,
    run as run_sync,
)
from .sync_github import GhSyncGitHub


@dataclass(frozen=True)
class OperationsConfig:
    environment: str
    install_root: Path
    uid: int
    services: tuple[LaunchdService, ...]
    gateway_targets: tuple[RuntimeTarget, ...]
    deploy_config: DeployConfig
    repo_slug: str
    snapshot_root: Path
    hermes_homes: tuple[Path, ...]
    preservation_command: tuple[str, ...]


@dataclass(frozen=True)
class SyncPolicyConfig:
    receipt_root: Path
    required_check: str
    check_timeout_seconds: int
    poll_interval_seconds: int
    resolver_backend: str
    reviewer_backend: str


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.expanduser().read_text(encoding="utf-8-sig")) or {}
    if not isinstance(payload, dict):
        raise ValueError("operations config must contain a YAML mapping")
    return payload


def _mapping(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"operations config must contain a '{name}' mapping")
    return value


def _path(value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    return Path(value).expanduser().resolve(strict=False)


def _command(value: object, *, field: str, required: bool = True) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(f"{field} must be a non-empty string list")
    return tuple(value)


def load_operations_config(path: Path) -> OperationsConfig:
    raw = _load_yaml(path)
    runtime = _mapping(raw, "runtime")
    deploy = _mapping(raw, "deploy")
    environment = raw.get("environment")
    if environment not in {"production", "recovery_canary"}:
        raise ValueError("environment must be 'production' or 'recovery_canary'")

    install_root = _path(runtime.get("install_root"), field="runtime.install_root")
    uid = int(runtime.get("uid", os.getuid() if hasattr(os, "getuid") else 0))

    service_rows = runtime.get("services")
    if not isinstance(service_rows, list) or not service_rows:
        raise ValueError("runtime.services must contain at least one service")
    services = []
    for index, row in enumerate(service_rows):
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            raise ValueError(f"runtime.services[{index}] is invalid")
        services.append(
            LaunchdService(
                label=row["label"],
                plist_path=_path(
                    row.get("plist"), field=f"runtime.services[{index}].plist"
                ),
            )
        )

    gateway_rows = runtime.get("gateways")
    if not isinstance(gateway_rows, list) or not gateway_rows:
        raise ValueError("runtime.gateways must contain at least one gateway")
    gateway_targets = []
    for index, row in enumerate(gateway_rows):
        if not isinstance(row, dict) or not isinstance(row.get("profile"), str):
            raise ValueError(f"runtime.gateways[{index}] is invalid")
        gateway_targets.append(
            RuntimeTarget(
                profile=row["profile"],
                hermes_home=_path(
                    row.get("hermes_home"),
                    field=f"runtime.gateways[{index}].hermes_home",
                ),
                plist_path=_path(
                    row.get("plist"), field=f"runtime.gateways[{index}].plist"
                ),
            )
        )

    homes_value = deploy.get("hermes_homes")
    if not isinstance(homes_value, list) or not homes_value:
        raise ValueError("deploy.hermes_homes must contain at least one path")
    hermes_homes = tuple(
        _path(value, field="deploy.hermes_homes") for value in homes_value
    )
    postinstall_value = deploy.get("postinstall_commands", [])
    if not isinstance(postinstall_value, list):
        raise ValueError("deploy.postinstall_commands must be a list")
    postinstall_commands = tuple(
        _command(value, field=f"deploy.postinstall_commands[{index}]")
        for index, value in enumerate(postinstall_value)
    )
    uv_extras_value = deploy.get("uv_extras")
    if not isinstance(uv_extras_value, list) or not uv_extras_value or any(
        not isinstance(value, str) or not value.strip() for value in uv_extras_value
    ):
        raise ValueError("deploy.uv_extras must contain non-empty strings")
    uv_extras = tuple(value.strip() for value in uv_extras_value)
    required_fields = ("origin", "repo_slug", "record_root", "snapshot_root")
    missing = [field for field in required_fields if not deploy.get(field)]
    if missing:
        raise ValueError(f"deploy config is missing required fields: {missing}")

    deploy_config = DeployConfig(
        install_root=install_root,
        origin=str(deploy["origin"]),
        record_root=_path(deploy["record_root"], field="deploy.record_root"),
        lock_path=(
            _path(deploy["lock_path"], field="deploy.lock_path")
            if deploy.get("lock_path")
            else None
        ),
        required_approver=str(deploy.get("required_approver", "Ole Ørum-Petersen")),
        required_check=str(deploy.get("required_check", "All required checks pass")),
        uv_extras=uv_extras,
        postinstall_commands=postinstall_commands,
    )
    return OperationsConfig(
        environment=environment,
        install_root=install_root,
        uid=uid,
        services=tuple(services),
        gateway_targets=tuple(gateway_targets),
        deploy_config=deploy_config,
        repo_slug=str(deploy["repo_slug"]),
        snapshot_root=_path(deploy["snapshot_root"], field="deploy.snapshot_root"),
        hermes_homes=hermes_homes,
        preservation_command=_command(
            deploy.get("preservation_command"),
            field="deploy.preservation_command",
        ),
    )


def load_sync_config(path: Path) -> SyncConfig:
    raw = _load_yaml(path)
    values = raw.get("sync")
    if not isinstance(values, dict):
        raise ValueError("operations config must contain a 'sync' mapping")
    required = {
        "repo",
        "worktree",
        "origin",
        "upstream",
        "candidate_branch",
        "repo_slug",
    }
    missing = sorted(required - set(values))
    if missing:
        raise ValueError(f"sync config is missing required fields: {missing}")
    lock_path = values.get("lock_path")
    kwargs = {
        "repo": Path(values["repo"]).expanduser().resolve(strict=False),
        "worktree": Path(values["worktree"]).expanduser().resolve(strict=False),
        "origin": str(values["origin"]),
        "upstream": str(values["upstream"]),
        "candidate_branch": str(values["candidate_branch"]),
        "repo_slug": str(values["repo_slug"]),
    }
    if lock_path is not None:
        kwargs["lock_path"] = Path(lock_path).expanduser().resolve(strict=False)
    return SyncConfig(**kwargs)


def load_sync_policy_config(path: Path) -> SyncPolicyConfig:
    values = _mapping(_load_yaml(path), "sync")
    required = {
        "receipt_root",
        "required_check",
        "check_timeout_seconds",
        "poll_interval_seconds",
        "resolver_backend",
        "reviewer_backend",
    }
    missing = sorted(required - set(values))
    if missing:
        raise ValueError(f"sync policy config is missing required fields: {missing}")

    def positive_integer(name: str) -> int:
        value = values[name]
        if type(value) is not int or value <= 0:
            raise ValueError(f"sync.{name} must be a positive integer")
        return value

    def backend(name: str) -> str:
        value = values[name]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"sync.{name} must be a normalized backend id")
        return value

    required_check = values["required_check"]
    if not isinstance(required_check, str) or not required_check.strip():
        raise ValueError("sync.required_check must be a non-empty string")
    resolver_backend = backend("resolver_backend")
    reviewer_backend = backend("reviewer_backend")
    if resolver_backend.casefold() == reviewer_backend.casefold():
        raise ValueError("sync reviewer backend must be independent")
    return SyncPolicyConfig(
        receipt_root=_path(values["receipt_root"], field="sync.receipt_root"),
        required_check=required_check,
        check_timeout_seconds=positive_integer("check_timeout_seconds"),
        poll_interval_seconds=positive_integer("poll_interval_seconds"),
        resolver_backend=resolver_backend,
        reviewer_backend=reviewer_backend,
    )


def load_conflict_resolver(path: Path) -> CodexConflictResolver | None:
    values = _mapping(_load_yaml(path), "sync")
    resolver = values.get("conflict_resolver")
    if resolver is None:
        return None
    if not isinstance(resolver, dict):
        raise ValueError("sync.conflict_resolver must be a mapping")
    prompt = resolver.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("sync.conflict_resolver.prompt must be a string")
    return CodexConflictResolver(
        executable=_path(
            resolver.get("codex_executable"),
            field="sync.conflict_resolver.codex_executable",
        ),
        prompt=prompt,
    )


def _sync_payload(result: SyncResult) -> dict[str, object]:
    return {
        "state": result.state.value,
        "base_sha": result.base_sha,
        "upstream_sha": result.upstream_sha,
        "candidate_sha": result.candidate_sha,
        "pr_number": result.pr_number,
        "checks": [asdict(check) for check in result.checks],
        "risk": result.risk,
        "changed_files": list(result.changed_files),
        "transitions": [state.value for state in result.transitions],
    }


def _health_payload(expected_sha: str, report) -> dict[str, object]:
    return {
        "expected_sha": expected_sha,
        "healthy": report.healthy,
        "checks": [asdict(check) for check in report.checks],
    }


def current_checkout_sha(root: Path, runner: CommandRunner) -> str:
    completed = runner.run(["git", "rev-parse", "HEAD"], cwd=root, timeout=30)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"could not read current install SHA: {detail}")
    sha = (completed.stdout or "").strip()
    if not sha:
        raise RuntimeError("current install SHA is empty")
    return sha


def _runtime_adapters(
    config: OperationsConfig,
    runner: CommandRunner,
    *,
    injection: str | None = None,
) -> tuple[LaunchdServiceController, RuntimeHealthChecker]:
    controller = LaunchdServiceController(
        services=config.services,
        install_root=config.install_root,
        uid=config.uid,
        runner=runner,
    )
    health = RuntimeHealthChecker(
        controller=controller,
        gateway_targets=config.gateway_targets,
        install_root=config.install_root,
        uid=config.uid,
        runner=runner,
        inject_failure=injection,
    )
    return controller, health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser(
        "sync", help="prepare or update the upstream PR"
    )
    sync_parser.add_argument("--config", type=Path, required=True)
    health_parser = subparsers.add_parser(
        "health", help="check configured services against an approved SHA"
    )
    health_parser.add_argument("--config", type=Path, required=True)
    health_parser.add_argument("--sha", required=True)
    deploy_parser = subparsers.add_parser(
        "deploy", help="deploy an approved PR merge SHA with rollback"
    )
    deploy_parser.add_argument("--config", type=Path, required=True)
    deploy_parser.add_argument("--sha", required=True)
    deploy_parser.add_argument("--pr-number", type=int, required=True)
    deploy_parser.add_argument("--approval-record", type=Path, required=True)
    deploy_parser.add_argument("--actor", required=True)
    deploy_parser.add_argument(
        "--inject-health-failure",
        choices=("after_restart",),
    )
    args = parser.parse_args(argv)

    if args.command == "sync":
        config = load_sync_config(args.config)
        runner = SubprocessCommandRunner()
        github = GhSyncGitHub(config.repo_slug, runner, config.repo)
        resolver = load_conflict_resolver(args.config)
        if resolver is None:
            result = run_sync(config, runner=runner, github=github)
        else:
            result = run_sync(
                config,
                runner=runner,
                github=github,
                resolver=resolver,
            )
        print(json.dumps(_sync_payload(result), indent=2, sort_keys=True))
        if result.state in {SyncState.NO_CHANGE, SyncState.PR_UPDATED}:
            return 0
        if result.state is SyncState.LOCKED:
            return 75
        if result.state is SyncState.VERIFY_FAILED:
            return 3
        return 2
    if args.command == "health":
        config = load_operations_config(args.config)
        runner = SubprocessCommandRunner()
        controller, health = _runtime_adapters(config, runner)
        report = health.check(
            expected_sha=args.sha,
            services=tuple(service.label for service in config.services),
        )
        print(json.dumps(_health_payload(args.sha, report), indent=2, sort_keys=True))
        return 0 if report.healthy else 3
    if args.command == "deploy":
        config = load_operations_config(args.config)
        runner = SubprocessCommandRunner()
        if args.inject_health_failure:
            current_sha = current_checkout_sha(config.install_root, runner)
            if config.environment != "recovery_canary" and args.sha != current_sha:
                print(
                    json.dumps(
                        {
                            "status": "rejected",
                            "error": (
                                "failure injection requires environment=recovery_canary "
                                "or a target SHA equal to the current install SHA"
                            ),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 2
        services, health = _runtime_adapters(
            config,
            runner,
            injection=args.inject_health_failure,
        )
        snapshots = SnapshotCoordinator(
            install_root=config.install_root,
            hermes_homes=config.hermes_homes,
            snapshot_root=config.snapshot_root,
            preservation_command=config.preservation_command,
            runner=runner,
        )
        github = GhReleaseVerifier(
            repo_slug=config.repo_slug,
            required_check=config.deploy_config.required_check,
            runner=runner,
            cwd=config.install_root,
        )
        request = DeployRequest(
            sha=args.sha,
            pr_number=args.pr_number,
            approval_record=args.approval_record,
            actor=args.actor,
        )
        try:
            record = run_deploy(
                request,
                config=config.deploy_config,
                runner=runner,
                github=github,
                snapshots=snapshots,
                services=services,
                health=health,
            )
        except (PreflightError, FileNotFoundError, ValueError, RuntimeError) as exc:
            print(
                json.dumps(
                    {"status": "preflight_failed", "error": str(exc)},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
        print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
        return 0 if record.status == "deployed" else 4
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
