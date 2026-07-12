"""Human-readable approval packet for a prepared upstream-sync candidate."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from utils import atomic_json_write, atomic_replace

from .sync_controller import AutonomousSyncResult, AutonomousSyncState


@dataclass(frozen=True)
class DecisionPacket:
    pr_number: int
    candidate_sha: str
    upstream_commit_count: int
    fork_custom_areas_touched: tuple[str, ...]
    test_results: tuple[dict[str, str], ...]
    ci_url: str
    ci_status: str
    independent_review_status: str
    risk_explanation: str
    rollback_sha: str
    recommendation: str

    @property
    def approve_available(self) -> bool:
        return (
            self.ci_status == "success"
            and self.independent_review_status == "green"
            and all(result.get("status") == "passed" for result in self.test_results)
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["fork_custom_areas_touched"] = list(self.fork_custom_areas_touched)
        payload["test_results"] = list(self.test_results)
        payload["approve_available"] = self.approve_available
        return payload


@dataclass(frozen=True)
class DecisionPacketArtifacts:
    json_path: Path
    markdown_path: Path
    sha256: str


@dataclass(frozen=True)
class EscalationDecisionPacket:
    """Secret-free evidence handed to Ole when automation must stop."""

    schema_version: int
    escalation_fingerprint: str
    recommendation: str
    summary: str
    actions: tuple[str, ...]
    state: str
    candidate_sha: str | None
    pr_number: int | None
    merge_sha: str | None
    fork_main_sha: str | None
    installed_sha: str | None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["actions"] = list(self.actions)
        return payload


@dataclass(frozen=True)
class EscalationDecisionPacketArtifact:
    path: Path
    sha256: str


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def write_decision_packet(
    packet: DecisionPacket,
    prefix: Path,
) -> DecisionPacketArtifacts:
    json_path = prefix.with_suffix(".json")
    markdown_path = prefix.with_suffix(".md")
    payload = packet.to_dict()
    recommendation = packet.recommendation if packet.approve_available else "Wait"
    tests = "\n".join(
        f"- {result['name']}: {result['status']}" for result in packet.test_results
    )
    markdown_content = (
        "# Hermes Upstream Sync Decision\n\n"
        f"**Recommendation:** {recommendation}\n\n"
        f"**Approve available:** {'yes' if packet.approve_available else 'no'}\n\n"
        f"- Pull request: #{packet.pr_number}\n"
        f"- Candidate SHA: `{packet.candidate_sha}`\n"
        f"- Upstream commits: {packet.upstream_commit_count}\n"
        f"- Fork areas touched: {', '.join(packet.fork_custom_areas_touched) or 'none'}\n"
        f"- Required CI (`All required checks pass`): {packet.ci_status}\n"
        f"- CI: {packet.ci_url}\n"
        f"- Independent review: {packet.independent_review_status}\n"
        f"- Rollback SHA: `{packet.rollback_sha}`\n\n"
        "## Local tests\n\n"
        f"{tests}\n\n"
        "## Risk\n\n"
        f"{packet.risk_explanation}\n"
    )
    atomic_json_write(json_path, payload, mode=0o600, sort_keys=True)
    _atomic_text_write(markdown_path, markdown_content)
    digest = hashlib.sha256(json_path.read_bytes()).hexdigest()
    return DecisionPacketArtifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        sha256=digest,
    )


def _canonical_packet_bytes(packet: EscalationDecisionPacket) -> bytes:
    return (
        json.dumps(
            packet.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _optional_sha(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a full lowercase commit SHA or null")
    return value


def publish_escalation_decision_packet(
    result: AutonomousSyncResult,
    *,
    fingerprint: str,
    trusted_root: Path,
) -> EscalationDecisionPacketArtifact:
    """Publish one deterministic packet without copying free-form logs/reasons."""
    state = (
        result.state.value
        if isinstance(result.state, AutonomousSyncState)
        else str(result.state)
    )
    if state != AutonomousSyncState.NEEDS_OLE.value or not result.needs_ole:
        raise ValueError("decision packets are only valid for NEEDS_OLE outcomes")
    if not _valid_digest(fingerprint):
        raise ValueError("escalation fingerprint must be 64 lowercase hex characters")
    packet = EscalationDecisionPacket(
        schema_version=1,
        escalation_fingerprint=fingerprint,
        recommendation="Wait",
        summary=(
            "Hermes upstream sync needs attention because automation could not "
            "prove that continuing is safe."
        ),
        actions=("Approve", "Wait", "Details"),
        state=AutonomousSyncState.NEEDS_OLE.value,
        candidate_sha=result.candidate_sha,
        pr_number=result.pr_number,
        merge_sha=result.merge_sha,
        fork_main_sha=result.fork_main_sha,
        installed_sha=result.installed_sha,
    )
    encoded = _canonical_packet_bytes(packet)
    digest = hashlib.sha256(encoded).hexdigest()
    root = Path(trusted_root).expanduser().resolve(strict=False)
    path = root / "decision-packets" / fingerprint / f"{digest}.json"
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError("content-addressed decision packet does not match its path")
    else:
        _atomic_text_write(path, encoded.decode("utf-8"))
    return EscalationDecisionPacketArtifact(path=path, sha256=digest)


def load_escalation_decision_packet(
    path: Path,
    *,
    trusted_root: Path,
) -> EscalationDecisionPacket:
    root = (
        Path(trusted_root).expanduser().resolve(strict=False) / "decision-packets"
    )
    resolved = Path(path).expanduser().resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("decision packet is outside the trusted decision-packet root")
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if resolved.name != f"{digest}.json":
        raise ValueError("decision packet content hash does not match its path")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("decision packet is not valid JSON") from exc
    expected = {field.name for field in fields(EscalationDecisionPacket)}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("decision packet fields do not match schema")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported decision packet schema")
    fingerprint = payload.get("escalation_fingerprint")
    if not _valid_digest(fingerprint) or resolved.parent.name != fingerprint:
        raise ValueError("decision packet fingerprint does not match its path")
    if payload.get("recommendation") != "Wait":
        raise ValueError("decision packet recommendation must be Wait")
    if payload.get("summary") != (
        "Hermes upstream sync needs attention because automation could not "
        "prove that continuing is safe."
    ):
        raise ValueError("decision packet summary is not canonical")
    if payload.get("actions") != ["Approve", "Wait", "Details"]:
        raise ValueError("decision packet actions are not canonical")
    if payload.get("state") != AutonomousSyncState.NEEDS_OLE.value:
        raise ValueError("decision packet state must be NEEDS_OLE")
    pr_number = payload.get("pr_number")
    if pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
        raise ValueError("decision packet PR number is invalid")
    return EscalationDecisionPacket(
        schema_version=1,
        escalation_fingerprint=fingerprint,
        recommendation="Wait",
        summary=payload["summary"],
        actions=("Approve", "Wait", "Details"),
        state=AutonomousSyncState.NEEDS_OLE.value,
        candidate_sha=_optional_sha(payload.get("candidate_sha"), field="candidate_sha"),
        pr_number=pr_number,
        merge_sha=_optional_sha(payload.get("merge_sha"), field="merge_sha"),
        fork_main_sha=_optional_sha(payload.get("fork_main_sha"), field="fork_main_sha"),
        installed_sha=_optional_sha(payload.get("installed_sha"), field="installed_sha"),
    )
