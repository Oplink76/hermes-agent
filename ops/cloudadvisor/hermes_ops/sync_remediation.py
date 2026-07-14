"""Constrained exact-head CI retry and candidate-only Codex repair ports."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from .command import CommandRunner
from .sync import (
    CheckResult,
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
    _verify,
    is_canonical_backend_executable,
)
from .sync_github import SyncPullRequestEvidence


class SyncRemediationError(RuntimeError):
    """A bounded remediation action lacked exact safe authority."""


class SyncRemediationPort(Protocol):
    def retry_infrastructure(
        self, candidate: SyncResult, evidence: SyncPullRequestEvidence
    ) -> bool: ...

    def repair_candidate(
        self,
        candidate: SyncResult,
        *,
        health_evidence: tuple[str, ...] = (),
    ) -> SyncResult | None: ...


_INFRASTRUCTURE_STEP_PREFIXES = (
    "set up job",
    "checkout",
    "set up python",
    "set up node",
    "install dependencies",
    "download action",
    "post ",
)


@dataclass(frozen=True)
class GhActionsRemediator:
    """Retry one exact failed Actions run only when its failed step is infrastructure."""

    repo_slug: str
    required_check: str
    runner: CommandRunner
    cwd: Path
    gh_executable: Path | str | None = None

    def __post_init__(self) -> None:
        executable = (
            shutil.which("gh")
            if self.gh_executable is None
            else str(self.gh_executable)
        )
        if not executable:
            raise SyncRemediationError("GitHub CLI executable is unavailable")
        object.__setattr__(self, "gh_executable", executable)

    def _run(self, argv: list[str]) -> str:
        executable = str(self.gh_executable)
        if (
            len(argv) < 3
            or argv[0] != executable
            or argv[1] != "run"
            or argv[2] not in {"list", "view", "rerun"}
        ):
            raise SyncRemediationError("refusing non-normalized GitHub run command")
        completed = self.runner.run(argv, cwd=self.cwd, timeout=300)
        if completed.returncode != 0:
            raise SyncRemediationError("GitHub Actions remediation command failed")
        return completed.stdout or ""

    def _json(self, argv: list[str]) -> object:
        try:
            return json.loads(self._run(argv))
        except json.JSONDecodeError as exc:
            raise SyncRemediationError(
                "GitHub Actions remediation evidence is invalid"
            ) from exc

    def retry_infrastructure(
        self, candidate: SyncResult, evidence: SyncPullRequestEvidence
    ) -> bool:
        if (
            not candidate.candidate_sha
            or candidate.pr_number is None
            or evidence.number != candidate.pr_number
            or evidence.head_sha != candidate.candidate_sha
            or evidence.required_check != self.required_check
            or evidence.required_check_conclusion != "failure"
            or type(evidence.workflow_run_id) is not int
            or evidence.workflow_run_id <= 0
            or type(evidence.required_check_run_id) is not int
            or evidence.required_check_run_id <= 0
        ):
            raise SyncRemediationError("candidate remediation identity is incomplete")
        executable = str(self.gh_executable)
        run_id = evidence.workflow_run_id
        detail = self._json([
            executable,
            "run",
            "view",
            str(run_id),
            "--repo",
            self.repo_slug,
            "--json",
            "databaseId,headSha,status,conclusion,jobs",
        ])
        if (
            not isinstance(detail, dict)
            or detail.get("databaseId") != run_id
            or detail.get("headSha") != candidate.candidate_sha
            or detail.get("status") not in {"in_progress", "completed"}
            or (
                detail.get("status") == "completed"
                and detail.get("conclusion") != "failure"
            )
        ):
            raise SyncRemediationError("GitHub Actions exact-run evidence changed")
        jobs = detail.get("jobs")
        if not isinstance(jobs, list):
            raise SyncRemediationError("GitHub Actions job evidence is invalid")
        required_jobs = [
            job
            for job in jobs
            if isinstance(job, dict)
            and job.get("databaseId") == evidence.required_check_run_id
            and job.get("name") == self.required_check
            and job.get("conclusion") == "failure"
        ]
        if len(required_jobs) != 1:
            raise SyncRemediationError("GitHub Actions exact check run is unavailable")
        steps = required_jobs[0].get("steps")
        if not isinstance(steps, list):
            return False
        failed_steps = [
            step.get("name", "")
            for step in steps
            if isinstance(step, dict) and step.get("conclusion") == "failure"
        ]
        if len(failed_steps) != 1 or not any(
            str(failed_steps[0]).casefold().startswith(prefix)
            for prefix in _INFRASTRUCTURE_STEP_PREFIXES
        ):
            return False
        self._run([
            executable,
            "run",
            "rerun",
            str(run_id),
            "--repo",
            self.repo_slug,
            "--failed",
        ])
        return True

    def repair_candidate(
        self,
        candidate: SyncResult,
        *,
        health_evidence: tuple[str, ...] = (),
    ) -> SyncResult | None:
        del candidate, health_evidence
        return None


@dataclass(frozen=True)
class CodexCandidateRemediator:
    """Make one locally verified repair in an isolated exact-head worktree."""

    config: SyncConfig
    runner: CommandRunner
    executable: Path
    prompt: str
    verify_fn: Callable[[Path, CommandRunner], list[CheckResult]] = _verify

    def __post_init__(self) -> None:
        if not is_canonical_backend_executable(self.executable, "codex"):
            raise ValueError("candidate repair must use the Codex executable")
        if not self.prompt.strip():
            raise ValueError("candidate repair prompt must not be empty")

    def retry_infrastructure(
        self, candidate: SyncResult, evidence: SyncPullRequestEvidence
    ) -> bool:
        del candidate, evidence
        return False

    def _run(
        self, argv: list[str], cwd: Path, *, timeout: int = 300
    ) -> str | None:
        completed = self.runner.run(argv, cwd=cwd, timeout=timeout)
        if completed.returncode != 0:
            return None
        return (completed.stdout or "").strip()

    def _resolution_record(
        self,
        candidate: SyncResult,
        worktree_record: Path,
        *,
        new_sha: str,
    ) -> tuple[Path | None, Path | None]:
        try:
            metadata = worktree_record.lstat()
            payload = json.loads(worktree_record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None, None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"conflicts", "strategy"}
            or payload.get("strategy") != candidate.resolution_strategy
            or not isinstance(payload.get("conflicts"), list)
        ):
            return None, None
        rows = payload["conflicts"]
        paths = [row.get("path") for row in rows if isinstance(row, dict)]
        if (
            len(paths) != len(rows)
            or len(paths) != len(candidate.conflicted_files)
            or len(paths) != len(set(paths))
            or set(paths) != set(candidate.conflicted_files)
            or any(
                set(row) != {"path", "decision"}
                or not isinstance(row.get("decision"), str)
                or not row["decision"].strip()
                for row in rows
                if isinstance(row, dict)
            )
        ):
            return None, None
        common = self._run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            self.config.repo,
        )
        if not common or not Path(common).is_absolute():
            return None, None
        evidence_dir = Path(common) / "hermes-sync-evidence"
        try:
            evidence_dir.mkdir(mode=0o700, exist_ok=True)
            directory_meta = evidence_dir.lstat()
        except OSError:
            return None, None
        if stat.S_ISLNK(directory_meta.st_mode) or not stat.S_ISDIR(
            directory_meta.st_mode
        ):
            return None, None
        target = evidence_dir / f"repair-{new_sha}.json"
        created = False
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            created = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            worktree_record.unlink()
        except OSError:
            if created:
                target.unlink(missing_ok=True)
            return None, None
        return target, evidence_dir

    def repair_candidate(
        self,
        candidate: SyncResult,
        *,
        health_evidence: tuple[str, ...] = (),
    ) -> SyncResult | None:
        if (
            candidate.state is not SyncState.PR_UPDATED
            or not candidate.candidate_sha
            or candidate.pr_number is None
            or not candidate.base_sha
            or not candidate.upstream_sha
        ):
            raise SyncRemediationError("candidate repair identity is incomplete")
        repair_strategy = "candidate_repair"
        with tempfile.TemporaryDirectory(
            prefix="hermes-sync-repair-", dir=self.config.repo.parent
        ) as temporary:
            worktree = Path(temporary)
            added = self._run(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    candidate.candidate_sha,
                ],
                self.config.repo,
                timeout=600,
            )
            if added is None:
                return None
            try:
                head = self._run(["git", "rev-parse", "HEAD"], worktree)
                if head != candidate.candidate_sha:
                    return None
                record = worktree / ".hermes-sync-repair.json"
                prompt = (
                    f"{self.prompt.rstrip()}\n\n"
                    "Repair only this exact upstream-sync candidate. Do not commit, "
                    "push, change remotes, or access paths outside this worktree. "
                    f"Observed health/check evidence: {json.dumps(health_evidence)}. "
                )
                prompt += (
                    "Write .hermes-sync-repair.json with exactly the top-level "
                    "keys `conflicts` and `strategy` and non-empty decisions. Run "
                    "`git diff --name-only HEAD` after editing and cover that exact "
                    "file set once each, with no missing, extra, or duplicate paths. "
                    f"Strategy must be {json.dumps(repair_strategy)}. "
                )
                command = [
                    str(self.executable),
                    "exec",
                    "--ignore-user-config",
                    "--sandbox",
                    "workspace-write",
                    "--ephemeral",
                    prompt,
                ]
                if self._run(command, worktree, timeout=1800) is None:
                    return None
                try:
                    repair_payload = record.read_text(encoding="utf-8")
                except OSError:
                    return None
                record.unlink(missing_ok=True)
                status = self._run(
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                    worktree,
                )
                if not status:
                    return None
                checks = self.verify_fn(worktree, self.runner)
                if not checks or any(check.status != "passed" for check in checks):
                    return None
                record.write_text(repair_payload, encoding="utf-8")
                if self._run(["git", "add", "-A"], worktree) is None:
                    return None
                if self._run(["git", "reset", "--", record.name], worktree) is None:
                    return None
                record.unlink(missing_ok=True)
                if self._run(
                    ["git", "commit", "-m", "fix(sync): repair exact candidate"],
                    worktree,
                    timeout=600,
                ) is None:
                    return None
                new_sha = self._run(["git", "rev-parse", "HEAD"], worktree)
                tree_sha = self._run(
                    ["git", "rev-parse", "HEAD^{tree}"], worktree
                )
                changed = self._run(
                    [
                        "git",
                        "diff",
                        "--name-only",
                        f"{self.config.origin}/main...HEAD",
                    ],
                    worktree,
                )
                if (
                    not new_sha
                    or new_sha == candidate.candidate_sha
                    or not tree_sha
                    or changed is None
                    or not changed
                ):
                    return None
                repair_delta_raw = self._run(
                    [
                        "git",
                        "diff",
                        "--name-only",
                        "-z",
                        f"{candidate.candidate_sha}..{new_sha}",
                    ],
                    worktree,
                )
                repair_paths = tuple(
                    path for path in (repair_delta_raw or "").split("\0") if path
                )
                if (
                    repair_delta_raw is None
                    or not repair_paths
                    or len(repair_paths) != len(set(repair_paths))
                ):
                    return None
                evidence_candidate = replace(
                    candidate,
                    candidate_sha=new_sha,
                    candidate_tree_sha=tree_sha,
                    classification=SyncClassification.MINOR_REVIEW_REQUIRED,
                    conflicted_files=repair_paths,
                    resolution_strategy=repair_strategy,
                )
                record.write_text(repair_payload, encoding="utf-8")
                resolution_record, evidence_dir = self._resolution_record(
                    evidence_candidate, record, new_sha=new_sha
                )
                if resolution_record is None:
                    return None
                destination = "HEAD:refs/heads/auto-sync/upstream"
                lease = (
                    "--force-with-lease=refs/heads/auto-sync/upstream:"
                    f"{candidate.candidate_sha}"
                )
                if self._run(
                    ["git", "push", self.config.origin, destination, lease],
                    worktree,
                    timeout=600,
                ) is None:
                    return None
                return SyncResult(
                    state=SyncState.PR_UPDATED,
                    base_sha=candidate.base_sha,
                    upstream_sha=candidate.upstream_sha,
                    candidate_sha=new_sha,
                    candidate_tree_sha=tree_sha,
                    pr_number=candidate.pr_number,
                    checks=tuple(checks),
                    risk=candidate.risk,
                    changed_files=tuple(
                        line for line in (changed or "").splitlines() if line
                    ),
                    transitions=candidate.transitions
                    + (SyncState.VERIFIED, SyncState.PUSHED, SyncState.PR_UPDATED),
                    classification=SyncClassification.MINOR_REVIEW_REQUIRED,
                    conflicted_files=repair_paths,
                    resolution_record=resolution_record,
                    resolution_evidence_dir=evidence_dir,
                    resolution_strategy=repair_strategy,
                )
            finally:
                self.runner.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=self.config.repo,
                    timeout=600,
                )


@dataclass(frozen=True)
class BoundedSyncRemediator:
    actions: GhActionsRemediator
    candidate: CodexCandidateRemediator

    def retry_infrastructure(
        self, value: SyncResult, evidence: SyncPullRequestEvidence
    ) -> bool:
        return self.actions.retry_infrastructure(value, evidence)

    def repair_candidate(
        self,
        value: SyncResult,
        *,
        health_evidence: tuple[str, ...] = (),
    ) -> SyncResult | None:
        return self.candidate.repair_candidate(
            value, health_evidence=health_evidence
        )
