"""Strict, attention-only cron boundary for CloudAdvisor Hermes operations."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from hermes_cli.agent_memory_protocol import acknowledge_attention

from .decision_packet import load_escalation_decision_packet
from .sync_status import SyncDecisionOutbox


_QUIET_STATES = {
    "NO_CHANGE": 0,
    "DEPLOYED": 0,
    "ROLLED_BACK_REVERTED": 0,
    "PENDING_REFRESH": 75,
    "LOCKED": 75,
}
_KNOWN_STATES = _QUIET_STATES.keys() | {"NEEDS_OLE"}
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
    "reason_code",
    "failed_gate",
    "repo_slug",
    "affected_files",
    "rollback_state",
    "rollback_sha",
    "revert_state",
    "revert_sha",
    "details_artifact",
    "checked_at",
    "upstream_behind",
    "fork_behind",
    "sync_required_check",
    "notify_ole",
    "escalation_fingerprint",
    "decision_packet_path",
    "decision_packet_sha256",
    "decision_idempotency_key",
}
_AGENT_MEMORY_FIELDS = {
    "enabled",
    "vault_available",
    "pending",
    "oldest_pending_hours",
    "attention_required",
    "reason",
    "fingerprint",
    "notify_ole",
}
_AGENT_MEMORY_REASONS = {
    "none",
    "corrupt_or_unsafe",
    "pending_for_24_hours",
}


@dataclass(frozen=True)
class CronWrapperConfig:
    python: Path
    install_root: Path
    operations_config: Path
    trusted_root: Path
    outbox_store: Path
    delivery_command: tuple[str, ...]


def _load_exact_object(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid sync-auto JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _SYNC_FIELDS:
        raise ValueError("sync-auto JSON fields do not match the wrapper schema")
    _validate_basic_fields(payload)
    _validate_sha_fields(payload)
    _validate_optional_strings(payload)
    _validate_affected_files(payload)
    _validate_structured_ids(payload)
    return payload


def _validate_basic_fields(payload: dict[str, object]) -> None:
    state = payload.get("state")
    if not isinstance(state, str) or state not in _KNOWN_STATES:
        raise ValueError("sync-auto state is invalid")
    if type(payload.get("notify_ole")) is not bool:
        raise ValueError("sync-auto notify_ole must be boolean")
    if type(payload.get("needs_ole")) is not bool:
        raise ValueError("sync-auto needs_ole must be boolean")
    pr_number = payload.get("pr_number")
    if pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
        raise ValueError("sync-auto pr_number is invalid")
    for field in ("upstream_behind", "fork_behind"):
        value = payload.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"sync-auto {field} is invalid")


def _validate_sha_fields(payload: dict[str, object]) -> None:
    for field in (
        "candidate_sha",
        "merge_sha",
        "deployed_sha",
        "fork_main_sha",
        "installed_sha",
        "rollback_sha",
        "revert_sha",
    ):
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"sync-auto {field} is invalid")


def _validate_optional_strings(payload: dict[str, object]) -> None:
    for field in (
        "reason",
        "checked_at",
        "sync_required_check",
        "escalation_fingerprint",
        "decision_packet_path",
        "decision_packet_sha256",
        "decision_idempotency_key",
        "reason_code",
        "failed_gate",
        "repo_slug",
        "rollback_state",
        "revert_state",
        "details_artifact",
    ):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"sync-auto {field} is invalid")


def _validate_affected_files(payload: dict[str, object]) -> None:
    affected_files = payload.get("affected_files")
    if (
        not isinstance(affected_files, list)
        or not all(
            isinstance(path, str)
            and path
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts
            for path in affected_files
        )
        or len(affected_files) != len(set(affected_files))
    ):
        raise ValueError("sync-auto affected_files is invalid")


def _validate_structured_ids(payload: dict[str, object]) -> None:
    repo_slug = payload.get("repo_slug")
    if (
        not isinstance(repo_slug, str)
        or repo_slug != repo_slug.strip()
        or repo_slug.count("/") != 1
        or not all(repo_slug.split("/"))
    ):
        raise ValueError("sync-auto repo_slug is invalid")
    reason_code = payload.get("reason_code")
    if reason_code is not None and re.fullmatch(
        r"[A-Z][A-Z0-9_]*", reason_code
    ) is None:
        raise ValueError("sync-auto reason_code is invalid")
    failed_gate = payload.get("failed_gate")
    if failed_gate is not None and re.fullmatch(
        r"[a-z][a-z0-9_]*", failed_gate
    ) is None:
        raise ValueError("sync-auto failed_gate is invalid")


def _matches_packet(payload: dict[str, object], packet) -> bool:
    scalar_match = all(
        payload.get(field) == getattr(packet, field)
        for field in (
            "state",
            "reason_code",
            "failed_gate",
            "repo_slug",
            "candidate_sha",
            "pr_number",
            "merge_sha",
            "fork_main_sha",
            "installed_sha",
            "rollback_state",
            "rollback_sha",
            "revert_state",
            "revert_sha",
            "details_artifact",
        )
    )
    return (
        scalar_match
        and tuple(payload.get("affected_files", ())) == packet.affected_files
        and payload.get("escalation_fingerprint") == packet.escalation_fingerprint
    )


def _deliver_pending(
    config: CronWrapperConfig,
    pending,
    *,
    deliver: Callable[[str], None] | None,
    delivery_run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    packet_path = Path(pending.packet_path)
    packet = load_escalation_decision_packet(
        packet_path,
        trusted_root=config.trusted_root,
    )
    if (
        packet.escalation_fingerprint != pending.escalation_fingerprint
        or packet_path.name != f"{pending.packet_sha256}.json"
    ):
        raise ValueError("pending decision packet identity does not match")
    lines = [
        "🚨 Hermes upstream sync needs attention",
        f"Recommendation: {packet.recommendation}",
        packet.summary,
        f"Decision id: {pending.idempotency_key}",
    ]
    if packet.pr_number is not None:
        lines.append(f"Pull request: #{packet.pr_number}")
    lines.append(f"Approve / Wait / Details: {packet.details_artifact}")
    message = "\n".join(lines) + "\n"
    _deliver_message(
        config,
        message,
        deliver=deliver,
        delivery_run=delivery_run,
    )
    SyncDecisionOutbox(config.outbox_store).acknowledge(
        fingerprint=pending.escalation_fingerprint,
        packet_sha256=pending.packet_sha256,
        idempotency_key=pending.idempotency_key,
    )


def _deliver_message(
    config: CronWrapperConfig,
    message: str,
    *,
    deliver: Callable[[str], None] | None,
    delivery_run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    if deliver is not None:
        deliver(message)
    else:
        if not config.delivery_command:
            raise ValueError("delivery command is not configured")
        completed = delivery_run(
            list(config.delivery_command),
            cwd=config.install_root,
            text=True,
            input=message,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if completed.returncode != 0:
            raise OSError("delivery command failed")


def _load_agent_memory_status(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid Agent Memory status JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _AGENT_MEMORY_FIELDS:
        raise ValueError(
            "Agent Memory status fields do not match the wrapper schema"
        )
    for field in (
        "enabled",
        "vault_available",
        "attention_required",
        "notify_ole",
    ):
        if type(payload[field]) is not bool:
            raise ValueError(f"Agent Memory status {field} must be boolean")
    pending = payload["pending"]
    if type(pending) is not int or pending < 0:
        raise ValueError("Agent Memory status pending is invalid")
    oldest = payload["oldest_pending_hours"]
    if (
        isinstance(oldest, bool)
        or not isinstance(oldest, (int, float))
        or not math.isfinite(oldest)
        or oldest < 0
    ):
        raise ValueError("Agent Memory status oldest_pending_hours is invalid")
    reason = payload["reason"]
    if not isinstance(reason, str) or reason not in _AGENT_MEMORY_REASONS:
        raise ValueError("Agent Memory status reason is invalid")
    fingerprint = payload["fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("Agent Memory status fingerprint is invalid")
    attention = payload["attention_required"]
    notify = payload["notify_ole"]
    if notify and not attention:
        raise ValueError("Agent Memory notification contradicts attention state")
    if (reason == "none") != (not attention):
        raise ValueError("Agent Memory reason contradicts attention state")
    if attention and pending == 0:
        raise ValueError("Agent Memory attention requires pending entries")
    return payload


def run_agent_memory_attention(
    config: CronWrapperConfig,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    deliver: Callable[[str], None] | None = None,
    delivery_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    acknowledge: Callable[[str], None] = acknowledge_attention,
) -> int:
    try:
        completed = run(
            [
                str(config.python),
                "-m",
                "hermes_cli.main",
                "agent-memory",
                "status",
            ],
            cwd=config.install_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if completed.returncode != 0:
            raise ValueError("Agent Memory status command failed")
        status = _load_agent_memory_status(completed.stdout or "")
        if not status["notify_ole"]:
            return 0
        reasons = {
            "pending_for_24_hours": "external vault unavailable for 24 hours",
            "corrupt_or_unsafe": "unsafe or corrupt outbox entry",
        }
        reason = reasons[str(status["reason"])]
        message = "\n".join(
            [
                "🚨 Hermes Agent Memory needs attention",
                "Recommendation: inspect the Agent Memory outbox",
                f"Reason: {reason}",
                f"Pending entries: {status['pending']}",
                "Hermes kept development running and preserved writes locally.",
            ]
        ) + "\n"
        _deliver_message(
            config,
            message,
            deliver=deliver,
            delivery_run=delivery_run,
        )
        acknowledge(str(status["fingerprint"]))
        return 0
    except subprocess.TimeoutExpired:
        print("Agent Memory attention wrapper failed: command timed out", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"Agent Memory attention wrapper failed: {exc}", file=sys.stderr)
    return 2


def _result_requires_no_delivery(
    payload: dict[str, object],
    *,
    returncode: int,
) -> bool:
    state = str(payload["state"])
    notify = bool(payload["notify_ole"])
    if state in _QUIET_STATES:
        if returncode != _QUIET_STATES[state] or notify:
            raise ValueError("sync-auto exit code or notification contradicts state")
        return True
    if state == "NEEDS_OLE" and not notify:
        if returncode != 2:
            raise ValueError("NEEDS_OLE exit code contradicts state")
        return True
    if state != "NEEDS_OLE" or not payload.get("needs_ole") or not notify:
        raise ValueError("non-routine sync outcome lacks a valid Ole notification")
    if returncode != 2:
        raise ValueError("NEEDS_OLE exit code contradicts state")
    return False


def _pending_from_payload(
    config: CronWrapperConfig,
    outbox: SyncDecisionOutbox,
    payload: dict[str, object],
):
    fingerprint = payload.get("escalation_fingerprint")
    packet_path = payload.get("decision_packet_path")
    packet_sha256 = payload.get("decision_packet_sha256")
    idempotency_key = payload.get("decision_idempotency_key")
    identities = (fingerprint, packet_path, packet_sha256, idempotency_key)
    if not all(isinstance(value, str) for value in identities):
        raise ValueError("Ole notification is missing packet identity")
    packet = load_escalation_decision_packet(
        Path(packet_path),
        trusted_root=config.trusted_root,
    )
    if not _matches_packet(payload, packet):
        raise ValueError("decision packet fingerprint or evidence does not match")
    pending = outbox.load()
    observed = None
    if pending is not None:
        observed = (
            pending.escalation_fingerprint,
            pending.packet_path,
            pending.packet_sha256,
            pending.idempotency_key,
        )
    expected = (
        fingerprint,
        str(Path(packet_path).resolve(strict=False)),
        packet_sha256,
        idempotency_key,
    )
    if pending is None or pending.status != "pending" or observed != expected:
        raise ValueError("Ole notification does not match the pending outbox")
    return pending


def run_sync_auto(
    config: CronWrapperConfig,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    deliver: Callable[[str], None] | None = None,
    delivery_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    try:
        outbox = SyncDecisionOutbox(config.outbox_store)
        existing = outbox.load()
        if existing is not None and existing.status == "pending":
            _deliver_pending(
                config,
                existing,
                deliver=deliver,
                delivery_run=delivery_run,
            )
            return 0
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
        if _result_requires_no_delivery(payload, returncode=completed.returncode):
            return 0
        pending = _pending_from_payload(config, outbox, payload)
        _deliver_pending(
            config,
            pending,
            deliver=deliver,
            delivery_run=delivery_run,
        )
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
        python=Path(os.path.abspath(args.python.expanduser())),
        install_root=args.install_root.resolve(strict=False),
        operations_config=args.config.resolve(strict=False),
        trusted_root=policy.receipt_root,
        outbox_store=policy.notification_store,
        delivery_command=policy.delivery_command,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("sync-auto", "health"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args)
    except (OSError, ValueError):
        print("sync-auto wrapper failed: invalid configuration", file=sys.stderr)
        return 2
    primary_code = (
        run_health(config) if args.mode == "health" else run_sync_auto(config)
    )
    memory_code = run_agent_memory_attention(config)
    return primary_code if primary_code != 0 else memory_code


if __name__ == "__main__":
    raise SystemExit(main())
