"""Strict, attention-only cron boundary for CloudAdvisor Hermes operations."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .decision_packet import load_escalation_decision_packet
from hermes_cli.upstream_sync_status import SyncNotificationState


_QUIET_STATES = {
    "NO_CHANGE": 0,
    "DEPLOYED": 0,
    "ROLLED_BACK_REVERTED": 0,
    "PENDING_REFRESH": 75,
    "LOCKED": 75,
}
_KNOWN_STATES = _QUIET_STATES.keys() | {"NEEDS_OLE", "REFRESH_REQUIRED"}
_SYNC_FIELDS = {
    "state",
    "candidate_sha",
    "pr_number",
    "merge_sha",
    "deployed_sha",
    "fork_main_sha",
    "installed_sha",
    "needs_ole",
    "reason",
    "checked_at",
    "upstream_behind",
    "fork_behind",
    "sync_required_check",
    "notify_ole",
    "escalation_fingerprint",
    "decision_packet_path",
}


@dataclass(frozen=True)
class CronWrapperConfig:
    python: Path
    install_root: Path
    operations_config: Path
    trusted_root: Path
    delivery_store: Path


def _load_exact_object(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid sync-auto JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _SYNC_FIELDS:
        raise ValueError("sync-auto JSON fields do not match the wrapper schema")
    state = payload.get("state")
    if not isinstance(state, str) or state not in _KNOWN_STATES:
        raise ValueError("sync-auto state is invalid")
    if type(payload.get("notify_ole")) is not bool:
        raise ValueError("sync-auto notify_ole must be boolean")
    if type(payload.get("needs_ole")) is not bool:
        raise ValueError("sync-auto needs_ole must be boolean")
    for field in ("candidate_sha", "merge_sha", "deployed_sha", "fork_main_sha", "installed_sha"):
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"sync-auto {field} is invalid")
    pr_number = payload.get("pr_number")
    if pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
        raise ValueError("sync-auto pr_number is invalid")
    for field in ("upstream_behind", "fork_behind"):
        value = payload.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"sync-auto {field} is invalid")
    for field in (
        "reason",
        "checked_at",
        "sync_required_check",
        "escalation_fingerprint",
        "decision_packet_path",
    ):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"sync-auto {field} is invalid")
    return payload


def _matches_packet(payload: dict[str, object], packet) -> bool:
    return all(
        payload.get(field) == getattr(packet, field)
        for field in (
            "state",
            "candidate_sha",
            "pr_number",
            "merge_sha",
            "fork_main_sha",
            "installed_sha",
        )
    ) and payload.get("escalation_fingerprint") == packet.escalation_fingerprint


def run_sync_auto(
    config: CronWrapperConfig,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    try:
        completed = run(
            [
                str(config.python),
                "-m",
                "ops.cloudadvisor.hermes_ops.cli",
                "sync-auto",
                "--config",
                str(config.operations_config),
            ],
            cwd=config.install_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3600,
        )
        payload = _load_exact_object(completed.stdout or "")
        state = str(payload["state"])
        notify = bool(payload["notify_ole"])
        if state in _QUIET_STATES:
            if completed.returncode != _QUIET_STATES[state] or notify:
                raise ValueError("sync-auto exit code or notification contradicts state")
            return 0
        if state == "NEEDS_OLE" and not notify:
            if completed.returncode != 2:
                raise ValueError("NEEDS_OLE exit code contradicts state")
            return 0
        if state != "NEEDS_OLE" or not payload.get("needs_ole") or not notify:
            raise ValueError("non-routine sync outcome lacks a valid Ole notification")
        if completed.returncode != 2:
            raise ValueError("NEEDS_OLE exit code contradicts state")
        fingerprint = payload.get("escalation_fingerprint")
        packet_path = payload.get("decision_packet_path")
        if not isinstance(fingerprint, str) or not isinstance(packet_path, str):
            raise ValueError("Ole notification is missing packet identity")
        packet = load_escalation_decision_packet(
            Path(packet_path),
            trusted_root=config.trusted_root,
        )
        if not _matches_packet(payload, packet):
            raise ValueError("decision packet fingerprint or evidence does not match")
        deliveries = SyncNotificationState(config.delivery_store)
        if not deliveries.should_emit(fingerprint):
            return 0
        print("🚨 Hermes upstream sync needs attention")
        print(f"Recommendation: {packet.recommendation}")
        print(packet.summary)
        if packet.pr_number is not None:
            print(f"Pull request: #{packet.pr_number}")
        print(f"Approve / Wait / Details: {packet_path}")
        deliveries.record_emitted(fingerprint)
        return 0
    except subprocess.TimeoutExpired:
        print("sync-auto wrapper failed: command timed out", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"sync-auto wrapper failed: {exc}", file=sys.stderr)
    return 2


def run_health(
    config: CronWrapperConfig,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Preserve the existing attention-only runtime health action."""
    try:
        revision = run(
            ["git", "-C", str(config.install_root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        expected_sha = (revision.stdout or "").strip()
        if revision.returncode != 0 or not expected_sha:
            raise ValueError("could not determine installed Hermes SHA")
        completed = run(
            [
                str(config.python),
                "-m",
                "ops.cloudadvisor.hermes_ops.cli",
                "health",
                "--config",
                str(config.operations_config),
                "--sha",
                expected_sha,
            ],
            cwd=config.install_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        try:
            payload = json.loads(completed.stdout or "")
        except json.JSONDecodeError as exc:
            raise ValueError("invalid health JSON") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"expected_sha", "healthy", "checks"}
            or payload.get("expected_sha") != expected_sha
            or type(payload.get("healthy")) is not bool
            or not isinstance(payload.get("checks"), list)
        ):
            raise ValueError("health JSON fields do not match the wrapper schema")
        if payload["healthy"] is True and completed.returncode == 0:
            return 0
        print("🚨 Hermes gateway health check needs attention")
        for check in payload["checks"]:
            if isinstance(check, dict) and check.get("passed") is False:
                print(f"• {check.get('name', 'Check')}: {check.get('detail', 'failed')}")
        return 0
    except subprocess.TimeoutExpired:
        print("Hermes health wrapper failed: command timed out", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"Hermes health wrapper failed: {exc}", file=sys.stderr)
    return 2


def _config_from_args(args: argparse.Namespace) -> CronWrapperConfig:
    from .cli import load_sync_policy_config

    policy = load_sync_policy_config(args.config)
    return CronWrapperConfig(
        python=args.python.resolve(strict=False),
        install_root=args.install_root.resolve(strict=False),
        operations_config=args.config.resolve(strict=False),
        trusted_root=policy.receipt_root,
        delivery_store=policy.notification_store.with_name("sync-deliveries.json"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("sync-auto", "health"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args(argv)
    config = _config_from_args(args)
    return run_health(config) if args.mode == "health" else run_sync_auto(config)


if __name__ == "__main__":
    raise SystemExit(main())
