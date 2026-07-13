"""Read-only production activation preflight for autonomous sync."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .command import CommandRunner, SubprocessCommandRunner


@dataclass(frozen=True)
class SyncPreflightReport:
    ok: bool
    checks: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": self.checks}


def _executable(command: str, *, name: str, which: Callable[[str], str | None]) -> Path:
    raw = Path(command).expanduser()
    candidate = raw.resolve(strict=False) if raw.is_absolute() else None
    if candidate is None:
        resolved = which(command)
        candidate = Path(resolved).resolve(strict=False) if resolved else None
    if candidate is None or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(f"{name} executable is unavailable")
    return candidate


def _git_output(runner: CommandRunner, repo: Path, argv: list[str], name: str) -> str:
    completed = runner.run(argv, cwd=repo, timeout=30)
    value = (completed.stdout or "").strip()
    if completed.returncode != 0 or not value:
        raise ValueError(f"could not validate {name}")
    return value


def _matches_github_repo(url: str, slug: str) -> bool:
    normalized = url.strip().removesuffix(".git").rstrip("/")
    return normalized in {
        f"https://github.com/{slug}",
        f"git@github.com:{slug}",
        f"ssh://git@github.com/{slug}",
    }


def _validate_state_paths(install_root: Path, policy) -> str:
    recovery = install_root.parent / "recovery"
    expected = {
        "receipt_root": recovery / "sync-receipts",
        "status_file": recovery / "sync-status.json",
        "notification_store": recovery / "sync-notifications.json",
    }
    for name, path in expected.items():
        if Path(getattr(policy, name)).resolve(strict=False) != path.resolve(strict=False):
            raise ValueError(f"sync.{name} is not the canonical production path")
    if not recovery.is_dir() or recovery.is_symlink() or not os.access(recovery, os.W_OK):
        raise ValueError("Hermes recovery root is unavailable or unsafe")
    receipt = expected["receipt_root"]
    if receipt.exists() and (not receipt.is_dir() or receipt.is_symlink()):
        raise ValueError("sync receipt root is unavailable or unsafe")
    for name in ("status_file", "notification_store"):
        path = expected[name]
        if path.exists() and (not path.is_file() or path.is_symlink()):
            raise ValueError(f"sync {name} is unavailable or unsafe")
    return (
        f"state={recovery}; decisions={receipt / 'decision-packets'}; "
        f"details={receipt / 'decision-details'}"
    )


def _load_and_validate_config(
    config: Path, command_runner: CommandRunner
) -> tuple[Any, Any, Any, Any, Any]:
    from .cli import (
        _validate_autonomous_runtime_scope,
        load_conflict_resolver,
        load_conflict_reviewer,
        load_operations_config,
        load_sync_config,
        load_sync_policy_config,
    )

    sync = load_sync_config(config)
    policy = load_sync_policy_config(config)
    operations = load_operations_config(config)
    _validate_autonomous_runtime_scope(operations)
    if sync.repo_slug != operations.repo_slug or sync.repo_slug != "Oplink76/hermes-agent":
        raise ValueError("sync repository identity is not canonical")
    if sync.repo != operations.install_root:
        raise ValueError("sync repository and install root differ")
    if sync.origin != operations.deploy_config.origin or sync.origin != "origin":
        raise ValueError("origin identities differ")
    if sync.upstream != "upstream":
        raise ValueError("official upstream identity is not canonical")
    if policy.required_check != operations.deploy_config.required_check:
        raise ValueError("sync and deploy required checks differ")
    if not policy.delivery_command:
        raise ValueError("sync direct delivery command is missing")
    resolver = load_conflict_resolver(config)
    if resolver is None:
        raise ValueError("sync conflict resolver is missing")
    reviewer = load_conflict_reviewer(config, command_runner)
    return sync, policy, operations, resolver, reviewer


def _validate_repo_remotes(sync: Any, runner: CommandRunner) -> None:
    root = Path(
        _git_output(
            runner,
            sync.repo,
            ["git", "rev-parse", "--show-toplevel"],
            "Git repository",
        )
    )
    if root.resolve(strict=False) != sync.repo.resolve(strict=False):
        raise ValueError("configured sync repository is not the Git root")
    origin_url = _git_output(
        runner,
        sync.repo,
        ["git", "remote", "get-url", sync.origin],
        "origin remote",
    )
    upstream_url = _git_output(
        runner,
        sync.repo,
        ["git", "remote", "get-url", sync.upstream],
        "official upstream remote",
    )
    if not _matches_github_repo(origin_url, "Oplink76/hermes-agent"):
        raise ValueError("fork origin remote is not canonical")
    if not _matches_github_repo(upstream_url, "NousResearch/hermes-agent"):
        raise ValueError("official upstream remote is not canonical")


def _tool_checks(
    *,
    resolver: Any,
    reviewer: Any,
    policy: Any,
    operations: Any,
    which: Callable[[str], str | None],
) -> dict[str, str]:
    return {
        "gh": str(_executable("gh", name="GitHub CLI", which=which)),
        "codex": str(_executable(str(resolver.executable), name="Codex", which=which)),
        "claude": str(
            _executable(str(reviewer.executable), name="Claude", which=which)
        ),
        "delivery": str(
            _executable(policy.delivery_command[0], name="delivery", which=which)
        ),
        "preservation": str(
            _executable(
                operations.preservation_command[0],
                name="Package 1 preservation",
                which=which,
            )
        ),
    }


def run_sync_preflight(
    config_path: Path,
    *,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> SyncPreflightReport:
    """Validate activation dependencies without locks, writes, or service calls."""
    config = Path(config_path).expanduser().resolve(strict=True)
    if config.is_symlink() or not config.is_file():
        raise ValueError("operations config must be a regular file")
    command_runner = runner or SubprocessCommandRunner()
    sync, policy, operations, resolver, reviewer = _load_and_validate_config(
        config, command_runner
    )
    _validate_repo_remotes(sync, command_runner)

    checks = {
        "config": str(config),
        "runtime_scope": "default Hermes gateway and dashboard only",
        "repo_identity": sync.repo_slug,
        "git_remotes": "canonical fork origin and official upstream",
        "state_paths": _validate_state_paths(operations.install_root, policy),
    }
    checks.update(
        _tool_checks(
            resolver=resolver,
            reviewer=reviewer,
            policy=policy,
            operations=operations,
            which=which,
        )
    )
    return SyncPreflightReport(ok=True, checks=checks)
