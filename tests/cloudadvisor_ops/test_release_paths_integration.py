from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from ops.cloudadvisor.hermes_ops.deploy import (
    DeployConfig,
    DeployRequest,
    ReleaseEvidence,
    deploy,
)
from ops.cloudadvisor.hermes_ops.health import HealthCheck, HealthReport
from ops.cloudadvisor.hermes_ops.snapshot import SnapshotCoordinator
from ops.cloudadvisor.hermes_ops.sync import (
    CheckResult,
    SyncClassification,
    SyncConfig,
    SyncResult,
    SyncState,
    run as run_sync,
)
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_receipt import (
    finalize_sync_receipt,
    write_sync_receipt,
)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _seed_remotes(tmp_path: Path) -> tuple[Path, Path, Path]:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.name", "Hermes Integration Test")
    _git(seed, "config", "user.email", "hermes-test@example.invalid")
    (seed / "base.txt").write_text("fork base\n", encoding="utf-8")
    (seed / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\n\n[project.optional-dependencies]\ndev = []\n',
        encoding="utf-8",
    )
    _git(seed, "add", "base.txt", "pyproject.toml")
    _git(seed, "commit", "-m", "base")

    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(origin)],
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(upstream)],
        check=True,
        text=True,
        capture_output=True,
    )
    return seed, origin, upstream


class SyncRunner:
    """Use real Git while isolating the expensive repository-wide gates."""

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        if argv[0] == "git":
            return subprocess.run(
                argv,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        if argv[0] == "rg":
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeGitHub:
    def __init__(self):
        self.created = 0

    def find_open_pull_request(self, head: str, base: str) -> int | None:
        assert (head, base) == ("auto-sync/upstream", "main")
        return None

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str
    ) -> int:
        self.created += 1
        return 41

    def update_pull_request(self, number: int, *, title: str, body: str) -> None:
        raise AssertionError("no existing pull request should be updated")


def test_sync_real_git_path_pushes_candidate_without_mutating_origin_main(
    tmp_path: Path,
):
    seed, origin, upstream = _seed_remotes(tmp_path)
    _git(seed, "remote", "add", "upstream", str(upstream))
    (seed / "upstream.txt").write_text("upstream change\n", encoding="utf-8")
    _git(seed, "add", "upstream.txt")
    _git(seed, "commit", "-m", "upstream change")
    upstream_sha = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "upstream", "main")

    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "Hermes Integration Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")
    _git(repo, "remote", "add", "upstream", str(upstream))
    origin_main_before = _git(repo, "rev-parse", "origin/main")
    worktree = tmp_path / "candidate"
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "auto-sync/upstream",
        str(worktree),
        "origin/main",
    )
    github = FakeGitHub()

    result = run_sync(
        SyncConfig(
            repo=repo,
            worktree=worktree,
            origin="origin",
            upstream="upstream",
            candidate_branch="auto-sync/upstream",
            repo_slug="Oplink76/hermes-agent",
            lock_path=tmp_path / "sync.lock",
        ),
        runner=SyncRunner(),
        github=github,
    )

    origin_main_after = _git(repo, "ls-remote", str(origin), "refs/heads/main").split()[
        0
    ]
    candidate_sha = _git(
        repo,
        "ls-remote",
        str(origin),
        "refs/heads/auto-sync/upstream",
    ).split()[0]
    assert result.state is SyncState.PR_UPDATED
    assert github.created == 1
    assert origin_main_after == origin_main_before
    assert candidate_sha == result.candidate_sha
    assert (
        _git(worktree, "merge-base", "--is-ancestor", upstream_sha, candidate_sha) == ""
    )


class ReleaseVerifier:
    def __init__(self, sha: str):
        self.sha = sha

    def verify(self, pr_number: int) -> ReleaseEvidence:
        return ReleaseEvidence(
            pr_number=pr_number,
            merged=True,
            merge_sha=self.sha,
            repo_slug="Oplink76/hermes-agent",
            head_sha=self.sha,
            base_ref_name="main",
            base_sha="integration-base",
            required_check="All required checks pass",
            required_check_conclusion="success",
        )


class DeployRunner:
    def __init__(self, database: Path):
        self.database = database

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        if argv[0] == "git":
            return subprocess.run(
                argv,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        if argv == ["python", "verify-preservation.py"]:
            return subprocess.CompletedProcess(argv, 0, "verified", "")
        if argv == ["python", "migrate.py"]:
            with sqlite3.connect(self.database) as connection:
                connection.execute("UPDATE state SET value = 'mutated'")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["env", "UV_PROJECT_ENVIRONMENT=.venv", "uv"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", f"unexpected command: {argv}")


class Services:
    def __init__(self):
        self.running = ("ai.hermes.gateway",)
        self.events: list[tuple[str, tuple[str, ...]]] = []

    def loaded_services(self) -> tuple[str, ...]:
        return self.running

    def running_services(self) -> tuple[str, ...]:
        return self.running

    def inventory(self):
        return {"running": self.running}

    def stop(self, services: tuple[str, ...]) -> None:
        self.running = tuple(
            service for service in self.running if service not in services
        )
        self.events.append(("stop", services))

    def start(self, services: tuple[str, ...]) -> None:
        self.running = tuple(dict.fromkeys((*self.running, *services)))
        self.events.append(("start", services))


class FailCandidateHealth:
    def __init__(self, install_root: Path, candidate_sha: str):
        self.install_root = install_root
        self.candidate_sha = candidate_sha

    def check(
        self,
        *,
        expected_sha: str,
        services: tuple[str, ...],
        identity_required: bool = True,
        apply_injection: bool = True,
    ) -> HealthReport:
        actual_sha = _git(self.install_root, "rev-parse", "HEAD")
        passed = expected_sha == actual_sha and expected_sha != self.candidate_sha
        return HealthReport(
            checks=(HealthCheck("integration:checkout_identity", passed, actual_sha),)
        )


class ExactHealthyRuntime:
    def __init__(self, install_root: Path):
        self.install_root = install_root

    def check(
        self,
        *,
        expected_sha: str,
        services: tuple[str, ...],
        identity_required: bool = True,
        apply_injection: bool = True,
    ) -> HealthReport:
        del services, identity_required, apply_injection
        actual_sha = _git(self.install_root, "rev-parse", "HEAD")
        return HealthReport(
            checks=(
                HealthCheck(
                    "integration:checkout_identity",
                    actual_sha == expected_sha,
                    actual_sha,
                ),
            )
        )


class SyncReleaseVerifier:
    def __init__(
        self, *, pr_number: int, base_sha: str, candidate_sha: str, merge_sha: str
    ):
        self.pr_number = pr_number
        self.base_sha = base_sha
        self.candidate_sha = candidate_sha
        self.merge_sha = merge_sha

    def verify(self, pr_number: int) -> ReleaseEvidence:
        assert pr_number == self.pr_number
        return ReleaseEvidence(
            pr_number=pr_number,
            merged=True,
            merge_sha=self.merge_sha,
            repo_slug="Oplink76/hermes-agent",
            head_sha=self.candidate_sha,
            base_ref_name="main",
            base_sha=self.base_sha,
            required_check="All required checks pass",
            required_check_conclusion="success",
        )


def test_automated_sync_receipt_deploys_only_exact_real_merge_without_human_artifact(
    tmp_path: Path,
):
    seed, origin, upstream = _seed_remotes(tmp_path)
    base_sha = _git(seed, "rev-parse", "HEAD")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "remote", "add", "upstream", str(upstream))
    (seed / "upstream.txt").write_text("official upstream\n", encoding="utf-8")
    _git(seed, "add", "upstream.txt")
    _git(seed, "commit", "-m", "official upstream change")
    candidate_sha = _git(seed, "rev-parse", "HEAD")
    candidate_tree_sha = _git(seed, "rev-parse", "HEAD^{tree}")
    _git(seed, "push", "upstream", "HEAD:main")
    _git(seed, "push", "origin", "HEAD:auto-sync/upstream")

    required_checks = tuple(
        CheckResult(name, "passed", "real integration path")
        for name in (
            "diff_check",
            "unmerged_index",
            "conflict_markers",
            "compileall",
            "tests",
        )
    )
    candidate = SyncResult(
        state=SyncState.PR_UPDATED,
        base_sha=base_sha,
        upstream_sha=candidate_sha,
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree_sha,
        pr_number=41,
        checks=required_checks,
        changed_files=("upstream.txt",),
        classification=SyncClassification.CLEAN,
    )
    evidence = SyncPullRequestEvidence(
        number=41,
        state="open",
        base_sha=base_sha,
        head_sha=candidate_sha,
        required_check="All required checks pass",
        required_check_conclusion="success",
        workflow_run_id=4100,
        required_check_run_id=4101,
    )
    receipt_root = tmp_path / "receipts"
    premerge = write_sync_receipt(
        receipt_root,
        candidate,
        evidence,
        repo_slug="Oplink76/hermes-agent",
    )

    protected_merge = tmp_path / "protected-merge"
    subprocess.run(
        ["git", "clone", str(origin), str(protected_merge)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(protected_merge, "config", "user.name", "Protected GitHub")
    _git(protected_merge, "config", "user.email", "github@example.invalid")
    _git(protected_merge, "fetch", "origin", "auto-sync/upstream")
    assert _git(protected_merge, "rev-parse", "FETCH_HEAD") == candidate_sha
    _git(
        protected_merge,
        "merge",
        "--no-ff",
        "FETCH_HEAD",
        "-m",
        "merge exact autonomous candidate",
    )
    merge_sha = _git(protected_merge, "rev-parse", "HEAD")
    _git(protected_merge, "push", "origin", "main")
    finalized = finalize_sync_receipt(premerge.path, merge_sha=merge_sha)

    install_root = tmp_path / "install"
    subprocess.run(
        ["git", "clone", str(origin), str(install_root)],
        check=True,
        text=True,
        capture_output=True,
    )
    _git(install_root, "switch", "--detach", base_sha)
    runner = DeployRunner(tmp_path / "unused.sqlite")
    snapshots = SnapshotCoordinator(
        install_root=install_root,
        hermes_homes=(),
        snapshot_root=tmp_path / "snapshots",
        preservation_command=("python", "verify-preservation.py"),
        runner=runner,
    )
    services = Services()
    record = deploy(
        DeployRequest(
            sha=merge_sha,
            pr_number=41,
            actor="hermes-upstream-sync",
            authority_kind="automated_sync",
            authority_record=finalized.path,
        ),
        config=DeployConfig(
            install_root=install_root,
            origin="origin",
            record_root=tmp_path / "records",
            repo_slug="Oplink76/hermes-agent",
            sync_receipt_root=receipt_root,
            required_check="All required checks pass",
            uv_extras=("dev",),
        ),
        runner=runner,
        github=SyncReleaseVerifier(
            pr_number=41,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            merge_sha=merge_sha,
        ),
        snapshots=snapshots,
        services=services,
        health=ExactHealthyRuntime(install_root),
    )

    assert record.status == "deployed"
    assert record.requested_sha == merge_sha
    assert _git(install_root, "rev-parse", "HEAD") == merge_sha
    assert (
        _git(install_root, "merge-base", "--is-ancestor", candidate_sha, merge_sha)
        == ""
    )
    assert any(check.name == "preflight:authority" for check in record.checks)
    assert not list(tmp_path.rglob("approval*.json"))


def test_deploy_real_git_and_sqlite_path_preserves_mutated_state_on_failure(
    tmp_path: Path,
):
    seed, origin, _ = _seed_remotes(tmp_path)
    install_root = tmp_path / "install"
    subprocess.run(
        ["git", "clone", str(origin), str(install_root)],
        check=True,
        text=True,
        capture_output=True,
    )
    previous_sha = _git(install_root, "rev-parse", "HEAD")
    _git(seed, "remote", "add", "origin", str(origin))
    (seed / "release.txt").write_text("approved release\n", encoding="utf-8")
    _git(seed, "add", "release.txt")
    _git(seed, "commit", "-m", "approved release")
    candidate_sha = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "origin", "main")

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    database = hermes_home / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('preserved')")

    packet = tmp_path / "decision-packet.json"
    packet.write_text(
        json.dumps({
            "pr_number": 41,
            "candidate_sha": candidate_sha,
            "approve_available": True,
            "ci_status": "success",
            "independent_review_status": "green",
            "test_results": [{"name": "release suite", "status": "passed"}],
        }),
        encoding="utf-8",
    )
    packet_sha = hashlib.sha256(packet.read_bytes()).hexdigest()
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps({
            "approver": "Ole Ørum-Petersen",
            "pr_number": 41,
            "merge_sha": candidate_sha,
            "approved_at": "2026-07-10T12:00:00+02:00",
            "decision_packet": str(packet),
            "decision_packet_sha256": packet_sha,
        }),
        encoding="utf-8",
    )
    if os.name != "nt":
        approval.chmod(0o444)

    runner = DeployRunner(database)
    snapshots = SnapshotCoordinator(
        install_root=install_root,
        hermes_homes=[hermes_home],
        snapshot_root=tmp_path / "snapshots",
        preservation_command=("python", "verify-preservation.py"),
        runner=runner,
    )
    services = Services()

    record = deploy(
        DeployRequest(
            sha=candidate_sha,
            pr_number=41,
            approval_record=approval,
            actor="integration-test",
        ),
        config=DeployConfig(
            install_root=install_root,
            origin="origin",
            record_root=tmp_path / "records",
            repo_slug="Oplink76/hermes-agent",
            uv_extras=("dev",),
            postinstall_commands=(("python", "migrate.py"),),
        ),
        runner=runner,
        github=ReleaseVerifier(candidate_sha),
        snapshots=snapshots,
        services=services,
        health=FailCandidateHealth(install_root, candidate_sha),
    )

    with sqlite3.connect(database) as connection:
        preserved_value = connection.execute("SELECT value FROM state").fetchone()[0]
    assert record.status == "rolled_back_healthy"
    assert _git(install_root, "rev-parse", "HEAD") == previous_sha
    assert preserved_value == "mutated"
    assert services.events == [
        ("stop", ("ai.hermes.gateway",)),
        ("start", ("ai.hermes.gateway",)),
        ("stop", ("ai.hermes.gateway",)),
        ("start", ("ai.hermes.gateway",)),
    ]
    stored = json.loads((tmp_path / "records" / f"{record.id}.json").read_text())
    assert stored["status"] == "rolled_back_healthy"
