"""Human-readable approval packet for a prepared upstream-sync candidate."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class DecisionPacket:
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


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def write_decision_packet(
    packet: DecisionPacket,
    prefix: Path,
) -> DecisionPacketArtifacts:
    json_path = prefix.with_suffix(".json")
    markdown_path = prefix.with_suffix(".md")
    payload = packet.to_dict()
    json_content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    recommendation = packet.recommendation if packet.approve_available else "Wait"
    tests = "\n".join(
        f"- {result['name']}: {result['status']}" for result in packet.test_results
    )
    markdown_content = (
        "# Hermes Upstream Sync Decision\n\n"
        f"**Recommendation:** {recommendation}\n\n"
        f"**Approve available:** {'yes' if packet.approve_available else 'no'}\n\n"
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
    _atomic_write(json_path, json_content)
    _atomic_write(markdown_path, markdown_content)
    digest = hashlib.sha256(
        json_path.read_bytes() + markdown_path.read_bytes()
    ).hexdigest()
    return DecisionPacketArtifacts(
        json_path=json_path,
        markdown_path=markdown_path,
        sha256=digest,
    )
