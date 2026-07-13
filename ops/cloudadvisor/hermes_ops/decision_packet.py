"""Human-readable approval packet for a prepared upstream-sync candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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
    reason_code: str
    failed_gate: str
    repo_slug: str
    candidate_sha: str | None
    pr_number: int | None
    merge_sha: str | None
    fork_main_sha: str | None
    installed_sha: str | None
    affected_files: tuple[str, ...]
    rollback_state: str | None
    rollback_sha: str | None
    revert_state: str | None
    revert_sha: str | None
    details_artifact: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["actions"] = list(self.actions)
        payload["affected_files"] = list(self.affected_files)
        return payload


@dataclass(frozen=True)
class EscalationDecisionPacketArtifact:
    path: Path
    sha256: str
    details_path: Path


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


def _structured_evidence(
    result: AutonomousSyncResult,
    *,
    repo_slug: str,
) -> dict[str, object]:
    reason_code = getattr(result, "reason_code", None)
    failed_gate = getattr(result, "failed_gate", None)
    if not isinstance(reason_code, str) or re.fullmatch(
        r"[A-Z][A-Z0-9_]*", reason_code
    ) is None:
        raise ValueError("decision packet requires a stable reason code")
    if not isinstance(failed_gate, str) or re.fullmatch(
        r"[a-z][a-z0-9_]*", failed_gate
    ) is None:
        raise ValueError("decision packet requires an exact failed gate")
    if (
        not isinstance(repo_slug, str)
        or repo_slug != repo_slug.strip()
        or repo_slug.count("/") != 1
        or not all(repo_slug.split("/"))
    ):
        raise ValueError("decision packet repository is invalid")
    affected_files = tuple(getattr(result, "affected_files", ()))
    if len(affected_files) != len(set(affected_files)) or any(
        not isinstance(path, str)
        or not path
        or "\0" in path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
        for path in affected_files
    ):
        raise ValueError("decision packet affected files are invalid")
    for field in ("rollback_state", "revert_state"):
        value = getattr(result, field, None)
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) is None
        ):
            raise ValueError(f"decision packet {field} is invalid")
    controller_details = getattr(result, "details_artifact", None)
    if controller_details is not None and (
        not isinstance(controller_details, str)
        or not controller_details
        or Path(controller_details).is_absolute()
        or ".." in Path(controller_details).parts
    ):
        raise ValueError("decision packet controller details reference is invalid")
    for field in (
        "candidate_sha",
        "fork_main_sha",
        "installed_sha",
        "merge_sha",
        "revert_sha",
        "rollback_sha",
    ):
        _optional_sha(getattr(result, field, None), field=field)
    if result.pr_number is not None and (
        type(result.pr_number) is not int or result.pr_number <= 0
    ):
        raise ValueError("decision packet PR number is invalid")
    return {
        "affected_files": affected_files,
        "candidate_sha": result.candidate_sha,
        "controller_details_artifact": controller_details,
        "failed_gate": failed_gate,
        "fork_main_sha": result.fork_main_sha,
        "installed_sha": result.installed_sha,
        "merge_sha": result.merge_sha,
        "pr_number": result.pr_number,
        "reason_code": reason_code,
        "repo_slug": repo_slug,
        "revert_sha": getattr(result, "revert_sha", None),
        "revert_state": getattr(result, "revert_state", None),
        "rollback_sha": getattr(result, "rollback_sha", None),
        "rollback_state": getattr(result, "rollback_state", None),
    }


def _write_content_addressed(
    *,
    root: Path,
    directory: str,
    fingerprint: str,
    payload: dict[str, object],
) -> tuple[Path, str]:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    path = root / directory / fingerprint / f"{digest}.json"
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError(f"content-addressed {directory} does not match its path")
    else:
        _atomic_text_write(path, encoded.decode("utf-8"))
    return path, digest


def publish_escalation_decision_packet(
    result: AutonomousSyncResult,
    *,
    fingerprint: str,
    trusted_root: Path,
    repo_slug: str,
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
    evidence = _structured_evidence(result, repo_slug=repo_slug)
    root = Path(trusted_root).expanduser().resolve(strict=False)
    details_payload = {
        "schema_version": 1,
        "escalation_fingerprint": fingerprint,
        **{
            key: list(value) if key == "affected_files" else value
            for key, value in evidence.items()
        },
    }
    details_path, _ = _write_content_addressed(
        root=root,
        directory="decision-details",
        fingerprint=fingerprint,
        payload=details_payload,
    )
    packet = EscalationDecisionPacket(
        schema_version=1,
        escalation_fingerprint=fingerprint,
        recommendation="Wait",
        summary=f"Automation stopped at {evidence['failed_gate']} ({evidence['reason_code']}).",
        actions=("Approve", "Wait", "Details"),
        state=AutonomousSyncState.NEEDS_OLE.value,
        reason_code=evidence["reason_code"],
        failed_gate=evidence["failed_gate"],
        repo_slug=evidence["repo_slug"],
        candidate_sha=result.candidate_sha,
        pr_number=result.pr_number,
        merge_sha=result.merge_sha,
        fork_main_sha=result.fork_main_sha,
        installed_sha=result.installed_sha,
        affected_files=evidence["affected_files"],
        rollback_state=evidence["rollback_state"],
        rollback_sha=evidence["rollback_sha"],
        revert_state=evidence["revert_state"],
        revert_sha=evidence["revert_sha"],
        details_artifact=str(details_path),
    )
    encoded = _canonical_packet_bytes(packet)
    digest = hashlib.sha256(encoded).hexdigest()
    path = root / "decision-packets" / fingerprint / f"{digest}.json"
    if path.exists():
        if path.read_bytes() != encoded:
            raise ValueError("content-addressed decision packet does not match its path")
    else:
        _atomic_text_write(path, encoded.decode("utf-8"))
    return EscalationDecisionPacketArtifact(
        path=path,
        sha256=digest,
        details_path=details_path,
    )


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
    metadata = resolved.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise ValueError("decision packet is not a trusted regular file")
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
    reason_code = payload.get("reason_code")
    failed_gate = payload.get("failed_gate")
    if not isinstance(reason_code, str) or re.fullmatch(
        r"[A-Z][A-Z0-9_]*", reason_code
    ) is None:
        raise ValueError("decision packet reason code is invalid")
    if not isinstance(failed_gate, str) or re.fullmatch(
        r"[a-z][a-z0-9_]*", failed_gate
    ) is None:
        raise ValueError("decision packet failed gate is invalid")
    if payload.get("summary") != (
        f"Automation stopped at {failed_gate} ({reason_code})."
    ):
        raise ValueError("decision packet summary is not canonical")
    if payload.get("actions") != ["Approve", "Wait", "Details"]:
        raise ValueError("decision packet actions are not canonical")
    if payload.get("state") != AutonomousSyncState.NEEDS_OLE.value:
        raise ValueError("decision packet state must be NEEDS_OLE")
    repo_slug = payload.get("repo_slug")
    if (
        not isinstance(repo_slug, str)
        or repo_slug != repo_slug.strip()
        or repo_slug.count("/") != 1
        or not all(repo_slug.split("/"))
    ):
        raise ValueError("decision packet repository is invalid")
    pr_number = payload.get("pr_number")
    if pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
        raise ValueError("decision packet PR number is invalid")
    affected_files = payload.get("affected_files")
    if (
        not isinstance(affected_files, list)
        or not all(isinstance(path, str) for path in affected_files)
        or len(affected_files) != len(set(affected_files))
        or any(
            not path
            or "\0" in path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in affected_files
        )
    ):
        raise ValueError("decision packet affected files are invalid")
    details_artifact = payload.get("details_artifact")
    if not isinstance(details_artifact, str):
        raise ValueError("decision packet details reference is invalid")
    details_path = Path(details_artifact).expanduser().resolve(strict=False)
    details_root = (
        Path(trusted_root).expanduser().resolve(strict=False) / "decision-details"
    )
    if not details_path.is_relative_to(details_root):
        raise ValueError("decision packet details are outside the trusted root")
    details_metadata = details_path.lstat()
    if (
        stat.S_ISLNK(details_metadata.st_mode)
        or not stat.S_ISREG(details_metadata.st_mode)
        or (os.name != "nt" and stat.S_IMODE(details_metadata.st_mode) != 0o600)
    ):
        raise ValueError("decision packet details are not a trusted regular file")
    details_raw = details_path.read_bytes()
    if details_path.name != f"{hashlib.sha256(details_raw).hexdigest()}.json":
        raise ValueError("decision packet details hash does not match its path")
    try:
        details = json.loads(details_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("decision packet details are unreadable") from exc
    expected_details = {
        "schema_version",
        "escalation_fingerprint",
        "affected_files",
        "candidate_sha",
        "controller_details_artifact",
        "failed_gate",
        "fork_main_sha",
        "installed_sha",
        "merge_sha",
        "pr_number",
        "reason_code",
        "repo_slug",
        "revert_sha",
        "revert_state",
        "rollback_sha",
        "rollback_state",
    }
    if (
        not isinstance(details, dict)
        or set(details) != expected_details
        or details.get("schema_version") != 1
        or details.get("escalation_fingerprint") != fingerprint
        or any(
            details.get(field) != payload.get(field)
            for field in expected_details
            - {"schema_version", "escalation_fingerprint", "controller_details_artifact"}
        )
    ):
        raise ValueError("decision packet details do not match packet evidence")
    controller_details = details.get("controller_details_artifact")
    if controller_details is not None and (
        not isinstance(controller_details, str)
        or not controller_details
        or Path(controller_details).is_absolute()
        or ".." in Path(controller_details).parts
    ):
        raise ValueError("decision packet controller details reference is invalid")
    for field in ("rollback_state", "revert_state"):
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value) is None
        ):
            raise ValueError(f"decision packet {field} is invalid")
    return EscalationDecisionPacket(
        schema_version=1,
        escalation_fingerprint=fingerprint,
        recommendation="Wait",
        summary=payload["summary"],
        actions=("Approve", "Wait", "Details"),
        state=AutonomousSyncState.NEEDS_OLE.value,
        reason_code=reason_code,
        failed_gate=failed_gate,
        repo_slug=repo_slug,
        candidate_sha=_optional_sha(payload.get("candidate_sha"), field="candidate_sha"),
        pr_number=pr_number,
        merge_sha=_optional_sha(payload.get("merge_sha"), field="merge_sha"),
        fork_main_sha=_optional_sha(payload.get("fork_main_sha"), field="fork_main_sha"),
        installed_sha=_optional_sha(payload.get("installed_sha"), field="installed_sha"),
        affected_files=tuple(affected_files),
        rollback_state=payload.get("rollback_state"),
        rollback_sha=_optional_sha(payload.get("rollback_sha"), field="rollback_sha"),
        revert_state=payload.get("revert_state"),
        revert_sha=_optional_sha(payload.get("revert_sha"), field="revert_sha"),
        details_artifact=str(details_path),
    )
