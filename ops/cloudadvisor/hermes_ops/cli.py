"""Command-line boundary for CloudAdvisor Hermes operations."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from .command import CommandRunner, SubprocessCommandRunner
from .decision_packet import publish_escalation_decision_packet
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
from .sync_controller import (
    AutonomousSyncConfig,
    AutonomousSyncResult,
    AutonomousSyncState,
    run_autonomous_sync,
)
from .sync_review import ClaudeConflictReviewer
from .sync_remediation import (
    BoundedSyncRemediator,
    CodexCandidateRemediator,
    GhActionsRemediator,
)
from .sync_status import (
    SyncDecisionOutbox,
    SyncStatus,
    SyncStatusContext,
    status_from_result,
)
from .sync_preflight import run_sync_preflight


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
    status_file: Path
    notification_store: Path
    required_check: str
    check_timeout_seconds: int
    poll_interval_seconds: int
    resolver_backend: str
    reviewer_backend: str
    delivery_command: tuple[str, ...] = ()


_AUTONOMOUS_SERVICE_LABELS = frozenset(
    {"ai.hermes.gateway", "com.cloudadvisor.hermes-dashboard"}
)
_AUTONOMOUS_GATEWAY_PROFILES = frozenset({"default"})


def _validate_autonomous_runtime_scope(config: OperationsConfig) -> None:
    labels = tuple(service.label for service in config.services)
    profiles = tuple(target.profile for target in config.gateway_targets)
    if len(labels) != len(set(labels)) or set(labels) != _AUTONOMOUS_SERVICE_LABELS:
        raise ValueError(
            "sync-auto runtime.services must be exactly the approved Hermes "
            "gateway and dashboard labels"
        )
    if (
        len(profiles) != len(set(profiles))
        or set(profiles) != _AUTONOMOUS_GATEWAY_PROFILES
    ):
        raise ValueError(
            "sync-auto runtime.gateways must contain only the default profile"
        )


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
        repo_slug=str(deploy["repo_slug"]),
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
        "status_file",
        "notification_store",
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
    if resolver_backend != "codex" or reviewer_backend != "claude":
        raise ValueError("sync backends must be canonical codex and claude")
    receipt_root = _path(values["receipt_root"], field="sync.receipt_root")
    status_file = _path(values["status_file"], field="sync.status_file")
    notification_store = _path(
        values["notification_store"], field="sync.notification_store"
    )
    if status_file == notification_store:
        raise ValueError("sync.status_file and sync.notification_store must differ")
    delivery_command = _command(
        values.get("delivery_command"),
        field="sync.delivery_command",
        required=False,
    )
    forbidden_delivery_fragments = ("xoxb-", "xapp-", "token=", "password=")
    if any(
        "\0" in argument
        or "\n" in argument
        or "\r" in argument
        or any(fragment in argument.casefold() for fragment in forbidden_delivery_fragments)
        for argument in delivery_command
    ):
        raise ValueError("sync.delivery_command must not contain credentials")
    return SyncPolicyConfig(
        receipt_root=receipt_root,
        status_file=status_file,
        notification_store=notification_store,
        required_check=required_check,
        check_timeout_seconds=positive_integer("check_timeout_seconds"),
        poll_interval_seconds=positive_integer("poll_interval_seconds"),
        resolver_backend=resolver_backend,
        reviewer_backend=reviewer_backend,
        delivery_command=delivery_command,
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


def load_conflict_reviewer(
    path: Path,
    runner: CommandRunner,
) -> ClaudeConflictReviewer:
    values = _mapping(_load_yaml(path), "sync")
    reviewer = values.get("conflict_reviewer")
    if not isinstance(reviewer, dict):
        raise ValueError("sync.conflict_reviewer must be a mapping")
    policy = load_sync_policy_config(path)
    if policy.reviewer_backend.casefold() != "claude":
        raise ValueError("the configured conflict reviewer backend is not Claude")
    return ClaudeConflictReviewer(
        executable=_path(
            reviewer.get("claude_executable"),
            field="sync.conflict_reviewer.claude_executable",
        ),
        runner=runner,
        resolver_backend=policy.resolver_backend,
        reviewer_backend=policy.reviewer_backend,
        evidence_dir=policy.receipt_root / "resolutions",
    )


def _sync_remediator(
    sync_config: SyncConfig,
    policy: SyncPolicyConfig,
    resolver: CodexConflictResolver,
    github: GhSyncGitHub,
    runner: CommandRunner,
) -> BoundedSyncRemediator:
    return BoundedSyncRemediator(
        actions=GhActionsRemediator(
            repo_slug=sync_config.repo_slug,
            required_check=policy.required_check,
            runner=runner,
            cwd=sync_config.repo,
            gh_executable=github.gh_executable,
        ),
        candidate=CodexCandidateRemediator(
            config=sync_config,
            runner=runner,
            executable=resolver.executable,
            prompt=resolver.prompt,
        ),
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


def _autonomous_sync_payload(
    result: AutonomousSyncResult,
    *,
    status: SyncStatus | None,
    decision_packet_path: Path | None,
    decision_packet_sha256: str | None,
    decision_idempotency_key: str | None,
    decision_details_path: Path | None,
    repo_slug: str,
) -> dict[str, object]:
    return {
        "state": result.state.value,
        "candidate_sha": result.candidate_sha,
        "pr_number": result.pr_number,
        "merge_sha": result.merge_sha,
        "deployed_sha": result.deployed_sha,
        "fork_main_sha": status.fork_main_sha if status else result.fork_main_sha,
        "installed_sha": status.installed_sha if status else result.installed_sha,
        "needs_ole": result.needs_ole,
        "reason": result.reason,
        "reason_code": result.reason_code,
        "failed_gate": result.failed_gate,
        "repo_slug": repo_slug,
        "affected_files": list(result.affected_files),
        "rollback_state": result.rollback_state,
        "rollback_sha": result.rollback_sha,
        "revert_state": result.revert_state,
        "revert_sha": result.revert_sha,
        "details_artifact": (
            str(decision_details_path) if decision_details_path else None
        ),
        "checked_at": status.checked_at if status else None,
        "upstream_behind": status.upstream_behind if status else None,
        "fork_behind": status.fork_behind if status else None,
        "sync_required_check": status.required_check if status else None,
        "notify_ole": result.notify_ole,
        "escalation_fingerprint": (
            status.escalation_fingerprint if status else None
        ),
        "decision_packet_path": (
            str(decision_packet_path) if decision_packet_path else None
        ),
        "decision_packet_sha256": decision_packet_sha256,
        "decision_idempotency_key": decision_idempotency_key,
    }


def _publish_sync_outcome(
    result: AutonomousSyncResult,
    *,
    sync_config: SyncConfig,
    policy: SyncPolicyConfig,
    operations: OperationsConfig,
    runner: CommandRunner,
) -> tuple[SyncStatus, bool, Path | None, str | None, str | None, Path | None]:
    status = status_from_result(
        result,
        context=SyncStatusContext(
            sync=sync_config,
            install_root=operations.install_root,
            required_check=policy.required_check,
        ),
        runner=runner,
    )
    status.write(policy.status_file)
    outbox = SyncDecisionOutbox(policy.notification_store)
    notify_ole = False
    decision_packet_path = None
    decision_packet_sha256 = None
    decision_idempotency_key = None
    decision_details_path = None
    if status.escalation_fingerprint is not None:
        packet = publish_escalation_decision_packet(
            result,
            fingerprint=status.escalation_fingerprint,
            trusted_root=policy.receipt_root,
            repo_slug=sync_config.repo_slug,
        )
        decision_packet_path = packet.path
        decision_packet_sha256 = packet.sha256
        decision_details_path = packet.details_path
        notify_ole = outbox.stage(
            fingerprint=status.escalation_fingerprint,
            packet_path=packet.path,
            packet_sha256=packet.sha256,
        )
        decision_idempotency_key = outbox.load().idempotency_key
    elif result.state in {
        AutonomousSyncState.NO_CHANGE,
        AutonomousSyncState.DEPLOYED,
        AutonomousSyncState.ROLLED_BACK_REVERTED,
    }:
        outbox.clear_resolved()
    return (
        status,
        notify_ole,
        decision_packet_path,
        decision_packet_sha256,
        decision_idempotency_key,
        decision_details_path,
    )


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


def _sync_deploy_fn(config: OperationsConfig, runner: CommandRunner):
    services, health = _runtime_adapters(config, runner)
    snapshots = SnapshotCoordinator(
        install_root=config.install_root,
        hermes_homes=config.hermes_homes,
        snapshot_root=config.snapshot_root,
        preservation_command=config.preservation_command,
        runner=runner,
    )
    release = GhReleaseVerifier(
        repo_slug=config.repo_slug,
        required_check=config.deploy_config.required_check,
        runner=runner,
        cwd=config.install_root,
    )

    def deploy_sync(receipt: Path, sha: str, pr_number: int):
        return run_deploy(
            DeployRequest(
                sha=sha,
                pr_number=pr_number,
                actor="hermes-upstream-sync",
                authority_kind="automated_sync",
                authority_record=receipt,
            ),
            config=config.deploy_config,
            runner=runner,
            github=release,
            snapshots=snapshots,
            services=services,
            health=health,
        )

    return deploy_sync


def _sync_runtime_verify_fn(config: OperationsConfig, runner: CommandRunner):
    services, health = _runtime_adapters(config, runner)

    def verify_runtime(expected_sha: str) -> bool:
        report = health.check(
            expected_sha=expected_sha,
            services=services.running_services(),
            identity_required=True,
            apply_injection=False,
        )
        return report.healthy

    return verify_runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser(
        "sync", help="prepare or update the upstream PR"
    )
    sync_parser.add_argument("--config", type=Path, required=True)
    sync_auto_parser = subparsers.add_parser(
        "sync-auto", help="converge upstream through protected merge and deployment"
    )
    sync_auto_parser.add_argument("--config", type=Path, required=True)
    sync_auto_parser.add_argument(
        "--preflight",
        action="store_true",
        help="validate activation dependencies without changing state",
    )
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
    deploy_sync_parser = subparsers.add_parser(
        "deploy-sync", help="deploy an attested automated-sync merge SHA"
    )
    deploy_sync_parser.add_argument("--config", type=Path, required=True)
    deploy_sync_parser.add_argument("--sha", required=True)
    deploy_sync_parser.add_argument("--pr-number", type=int, required=True)
    deploy_sync_parser.add_argument("--sync-receipt", type=Path, required=True)
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
    if args.command == "sync-auto":
        if args.preflight:
            try:
                report = run_sync_preflight(args.config)
            except (OSError, RuntimeError, ValueError) as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
                return 2
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
            return 0
        sync_config = load_sync_config(args.config)
        policy = load_sync_policy_config(args.config)
        operations = load_operations_config(args.config)
        _validate_autonomous_runtime_scope(operations)
        if not policy.delivery_command:
            raise ValueError("sync-auto requires a configured direct delivery command")
        if policy.required_check != operations.deploy_config.required_check:
            raise ValueError(
                "sync and deploy required_check settings must be identical"
            )
        operations = replace(
            operations,
            deploy_config=replace(
                operations.deploy_config,
                sync_receipt_root=policy.receipt_root,
            ),
        )
        runner = SubprocessCommandRunner()
        resolver = load_conflict_resolver(args.config)
        if resolver is None:
            raise ValueError("sync-auto requires the configured conflict resolver")
        reviewer = load_conflict_reviewer(args.config, runner)
        github = GhSyncGitHub(
            sync_config.repo_slug,
            runner,
            sync_config.repo,
            required_check=policy.required_check,
        )
        published_status: SyncStatus | None = None
        published_packet_path: Path | None = None
        published_packet_sha256: str | None = None
        published_idempotency_key: str | None = None
        published_details_path: Path | None = None

        def publish_outcome(outcome: AutonomousSyncResult) -> bool:
            nonlocal published_idempotency_key
            nonlocal published_details_path
            nonlocal published_packet_path, published_packet_sha256, published_status
            (
                published_status,
                notify_ole,
                published_packet_path,
                published_packet_sha256,
                published_idempotency_key,
                published_details_path,
            ) = _publish_sync_outcome(
                outcome,
                sync_config=sync_config,
                policy=policy,
                operations=operations,
                runner=runner,
            )
            return notify_ole

        result = run_autonomous_sync(
            AutonomousSyncConfig(
                sync=sync_config,
                deploy=operations.deploy_config,
                receipt_root=policy.receipt_root,
                required_check=policy.required_check,
                check_timeout_seconds=policy.check_timeout_seconds,
                poll_interval_seconds=policy.poll_interval_seconds,
                resolver_backend=policy.resolver_backend,
            ),
            runner=runner,
            github=github,
            resolver=resolver,
            reviewer=reviewer,
            remediator=_sync_remediator(
                sync_config, policy, resolver, github, runner
            ),
            deploy_fn=_sync_deploy_fn(operations, runner),
            verify_runtime_fn=_sync_runtime_verify_fn(operations, runner),
            publish_outcome=publish_outcome,
        )
        print(
            json.dumps(
                _autonomous_sync_payload(
                    result,
                    status=published_status,
                    decision_packet_path=published_packet_path,
                    decision_packet_sha256=published_packet_sha256,
                    decision_idempotency_key=published_idempotency_key,
                    decision_details_path=published_details_path,
                    repo_slug=sync_config.repo_slug,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        if result.state in {
            AutonomousSyncState.NO_CHANGE,
            AutonomousSyncState.DEPLOYED,
            AutonomousSyncState.ROLLED_BACK_REVERTED,
        }:
            return 0
        if result.state in {
            AutonomousSyncState.LOCKED,
            AutonomousSyncState.PENDING_REFRESH,
        }:
            return 75
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
    if args.command in {"deploy", "deploy-sync"}:
        config = load_operations_config(args.config)
        if args.command == "deploy-sync":
            sync_policy = load_sync_policy_config(args.config)
            if sync_policy.required_check != config.deploy_config.required_check:
                raise ValueError(
                    "sync and deploy required_check settings must be identical"
                )
            config = replace(
                config,
                deploy_config=replace(
                    config.deploy_config,
                    sync_receipt_root=sync_policy.receipt_root,
                ),
            )
        runner = SubprocessCommandRunner()
        inject_health_failure = getattr(args, "inject_health_failure", None)
        if inject_health_failure:
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
            injection=inject_health_failure,
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
        if args.command == "deploy":
            request = DeployRequest(
                sha=args.sha,
                pr_number=args.pr_number,
                approval_record=args.approval_record,
                actor=args.actor,
            )
        else:
            request = DeployRequest(
                sha=args.sha,
                pr_number=args.pr_number,
                actor="hermes-upstream-sync",
                authority_kind="automated_sync",
                authority_record=args.sync_receipt,
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
