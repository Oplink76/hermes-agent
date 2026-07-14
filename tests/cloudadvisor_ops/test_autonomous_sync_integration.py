from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.deploy import (
    DeployConfig,
    DeployRequest,
    DeploymentRecord,
    ReleaseEvidence,
    deploy,
)
from ops.cloudadvisor.hermes_ops.health import HealthCheck, HealthReport
from ops.cloudadvisor.hermes_ops.sync import (
    CheckResult,
    SyncConfig,
    SyncState,
    prepare_candidate,
)
from ops.cloudadvisor.hermes_ops.sync_controller import (
    AutonomousSyncConfig,
    AutonomousSyncState,
    run_autonomous_sync,
)
from ops.cloudadvisor.hermes_ops.sync_github import SyncPullRequestEvidence
from ops.cloudadvisor.hermes_ops.sync_receipt import SyncEligibilityReceipt
from ops.cloudadvisor.hermes_ops.sync_remediation import CodexCandidateRemediator
from ops.cloudadvisor.hermes_ops.sync_review import ClaudeConflictReviewer


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _python_launcher(tmp_path: Path, name: str, source: str) -> Path:
    driver = tmp_path / f"{name}_driver.py"
    driver.write_text(source, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / f"{name}.cmd"
        launcher.write_bytes(f'@"{sys.executable}" "{driver}" %*\r\n'.encode("utf-8"))
        return launcher
    launcher = tmp_path / name
    launcher.write_text(
        f"#!{sys.executable}\nexec(compile(open({str(driver)!r}, 'r', encoding='utf-8').read(), "
        f"{str(driver)!r}, 'exec'))\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


class GateRunner(SubprocessCommandRunner):
    def __init__(self):
        self.local_gate_calls: list[tuple[str, ...]] = []
        self.interrupt_next_uv_sync = False

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        if argv[:5] == [
            "env",
            "UV_PROJECT_ENVIRONMENT=.venv",
            "uv",
            "sync",
            "--locked",
        ]:
            if self.interrupt_next_uv_sync:
                self.interrupt_next_uv_sync = False
                raise KeyboardInterrupt("simulated process loss after checkout")
            return subprocess.CompletedProcess(argv, 0, "synced\n", "")
        if (
            len(argv) >= 3
            and argv[1:3] == ["-m", "compileall"]
            or "scripts/run_tests.sh" in argv
        ):
            self.local_gate_calls.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0, "green\n", "")
        return super().run(argv, cwd, timeout)


class ProtectedGitHub:
    def __init__(self, origin: Path, root: Path):
        self.origin = origin
        self.root = root
        self.expected_base_sha: str | None = None
        self.next_pr = 7
        self.prs: dict[int, dict[str, object]] = {}
        self.created_heads: list[str] = []

    def find_open_pull_request(self, head: str, base: str):
        for number, row in self.prs.items():
            if row["state"] == "open" and row["head"] == head:
                return number
        return None

    def create_pull_request(self, *, head: str, base: str, title: str, body: str):
        number = self.next_pr
        self.next_pr += 1
        self.prs[number] = {
            "head": head,
            "state": "open",
            "merge_sha": None,
            "head_sha": None,
            "base_sha": None,
        }
        self.created_heads.append(head)
        return number

    def update_pull_request(self, number: int, *, title: str, body: str):
        assert self.prs[number]["state"] == "open"

    def _remote_sha(self, ref: str) -> str:
        output = _git(self.root, "ls-remote", "origin", ref)
        return output.split()[0]

    def evidence(self, pr_number: int):
        row = self.prs[pr_number]
        return SyncPullRequestEvidence(
            number=pr_number,
            state=str(row["state"]),
            base_sha=str(
                row["base_sha"] or self._remote_sha("refs/heads/main")
            ),
            head_sha=str(
                row["head_sha"]
                or self._remote_sha(f"refs/heads/{row['head']}")
            ),
            required_check="All required checks pass",
            required_check_conclusion="success",
            workflow_run_id=1000 + pr_number,
            required_check_run_id=2000 + pr_number,
            merge_sha=row["merge_sha"],
        )

    def merge_exact(self, pr_number: int, *, expected_head: str):
        row = self.prs[pr_number]
        assert row["state"] == "open"
        base_sha = self._remote_sha("refs/heads/main")
        assert self.expected_base_sha == base_sha
        assert expected_head == self._remote_sha(f"refs/heads/{row['head']}")
        admin = self.root.parent / f"admin-{pr_number}"
        subprocess.run(
            ["git", "clone", str(self.origin), str(admin)],
            check=True,
            capture_output=True,
        )
        _git(admin, "config", "user.name", "Protected GitHub")
        _git(admin, "config", "user.email", "github@example.invalid")
        _git(admin, "switch", "main")
        _git(admin, "fetch", "origin", str(row["head"]))
        assert _git(admin, "rev-parse", "FETCH_HEAD") == expected_head
        _git(admin, "merge", "--no-ff", "FETCH_HEAD", "-m", f"merge PR {pr_number}")
        merge_sha = _git(admin, "rev-parse", "HEAD")
        _git(admin, "push", "origin", "main")
        row["state"] = "merged"
        row["merge_sha"] = merge_sha
        row["head_sha"] = expected_head
        row["base_sha"] = base_sha
        return merge_sha

    def verify(self, pr_number: int) -> ReleaseEvidence:
        row = self.prs[pr_number]
        return ReleaseEvidence(
            pr_number=pr_number,
            merged=row["state"] == "merged",
            merge_sha=str(row["merge_sha"]),
            repo_slug="Oplink76/hermes-agent",
            head_sha=str(row["head_sha"]),
            base_ref_name="main",
            base_sha=str(row["base_sha"]),
            required_check="All required checks pass",
            required_check_conclusion="success",
        )


class RecordingSnapshots:
    def __init__(self, events: list[tuple[object, ...]]):
        self.events = events
        self.restore_count = 0

    def verify_preservation(self) -> bool:
        self.events.append(("preservation",))
        return True

    def create(self, previous_sha: str):
        snapshot = {"previous_sha": previous_sha}
        self.events.append(("snapshot", previous_sha))
        return snapshot

    def verify(self, snapshot) -> bool:
        self.events.append(("snapshot_verified", snapshot["previous_sha"]))
        return True

    def restore(self, snapshot) -> None:
        self.restore_count += 1
        self.events.append(("snapshot_restored", snapshot["previous_sha"]))


class RecordingServices:
    def __init__(self, events: list[tuple[object, ...]]):
        self.events = events
        self.running = ("ai.hermes.gateway",)
        self.start_count = 0
        self.stop_count = 0

    def loaded_services(self) -> tuple[str, ...]:
        self.events.append(("loaded", self.running))
        return self.running

    def running_services(self) -> tuple[str, ...]:
        self.events.append(("running", self.running))
        return self.running

    def inventory(self):
        return {"running": self.running, "starts": self.start_count}

    def stop(self, services: tuple[str, ...]) -> None:
        self.stop_count += 1
        self.events.append(("stop", services))
        self.running = tuple(
            service for service in self.running if service not in services
        )

    def start(self, services: tuple[str, ...]) -> None:
        self.start_count += 1
        self.events.append(("start", services))
        self.running = tuple(dict.fromkeys((*self.running, *services)))


class FailSecondCandidateHealth:
    def __init__(self, events: list[tuple[object, ...]]):
        self.events = events
        self.candidate_checks = 0

    def check(
        self,
        *,
        expected_sha: str,
        services: tuple[str, ...],
        identity_required: bool = True,
        apply_injection: bool = True,
    ) -> HealthReport:
        self.events.append((
            "health",
            expected_sha,
            services,
            identity_required,
            apply_injection,
        ))
        if apply_injection:
            self.candidate_checks += 1
        if apply_injection and self.candidate_checks == 2:
            return HealthReport((HealthCheck("runtime:default", False),))
        return HealthReport((HealthCheck("runtime:default", True),))


def test_real_candidate_recreates_branch_after_remote_deletion(tmp_path: Path):
    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Hermes E2E")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / "feature.txt").write_text("fork\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "fork base")
    _git(repo, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(upstream)],
        check=True,
        capture_output=True,
    )
    upstream_work = tmp_path / "upstream-work"
    subprocess.run(
        ["git", "clone", str(upstream), str(upstream_work)],
        check=True,
        capture_output=True,
    )
    _git(upstream_work, "config", "user.name", "Official Upstream")
    _git(upstream_work, "config", "user.email", "upstream@example.invalid")
    (upstream_work / "upstream.txt").write_text("upstream\n", encoding="utf-8")
    _git(upstream_work, "add", "upstream.txt")
    _git(upstream_work, "commit", "-m", "upstream change")
    _git(upstream_work, "push", "origin", "main")
    _git(repo, "remote", "add", "upstream", str(upstream))
    candidate = tmp_path / "candidate"
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "auto-sync/upstream",
        str(candidate),
        "main",
    )
    _git(repo, "push", "-u", "origin", "auto-sync/upstream")
    stale_sha = _git(
        repo, "rev-parse", "refs/remotes/origin/auto-sync/upstream"
    )
    _git(origin, "update-ref", "-d", "refs/heads/auto-sync/upstream")
    assert _git(repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream") == ""
    assert (
        _git(repo, "rev-parse", "refs/remotes/origin/auto-sync/upstream")
        == stale_sha
    )

    result = prepare_candidate(
        SyncConfig(
            repo=repo,
            worktree=candidate,
            origin="origin",
            upstream="upstream",
            candidate_branch="auto-sync/upstream",
            repo_slug="Oplink76/hermes-agent",
            lock_path=tmp_path / "sync.lock",
        ),
        runner=GateRunner(),
        github=ProtectedGitHub(origin, repo),
    )

    assert result.state is SyncState.PR_UPDATED
    assert result.candidate_sha == _git(
        repo, "ls-remote", "origin", "refs/heads/auto-sync/upstream"
    ).split()[0]


def test_real_controller_recovers_interrupted_checkout_then_reverts_repairs_and_redeploys(
    tmp_path: Path,
):
    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.name", "Hermes E2E")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / "feature.txt").write_text("healthy\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'hermes-e2e'\nversion = '0.0.0'\n"
        "[project.optional-dependencies]\nall = []\n",
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\nrevision = 1\n", encoding="utf-8")
    _git(repo, "add", "feature.txt", "pyproject.toml", "uv.lock")
    _git(repo, "commit", "-m", "healthy base")
    _git(repo, "push", "-u", "origin", "main")
    healthy_base = _git(repo, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(upstream)],
        check=True,
        capture_output=True,
    )
    upstream_work = tmp_path / "upstream-work"
    subprocess.run(
        ["git", "clone", str(upstream), str(upstream_work)],
        check=True,
        capture_output=True,
    )
    _git(upstream_work, "config", "user.name", "Official Upstream")
    _git(upstream_work, "config", "user.email", "upstream@example.invalid")
    (upstream_work / "feature.txt").write_text(
        "clean upstream behavior\n", encoding="utf-8"
    )
    _git(upstream_work, "commit", "-am", "clean upstream change")
    _git(upstream_work, "push", "origin", "main")
    clean_upstream_sha = _git(upstream_work, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "upstream", str(upstream))
    candidate_worktree = tmp_path / "candidate"
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "auto-sync/upstream",
        str(candidate_worktree),
        "main",
    )
    install = tmp_path / "install"
    subprocess.run(
        ["git", "clone", str(origin), str(install)],
        check=True,
        capture_output=True,
    )
    assert _git(install, "rev-parse", "HEAD") == healthy_base

    codex = _python_launcher(
        tmp_path,
        "codex",
        "\n".join([
            "import json",
            "from pathlib import Path",
            "Path('feature.txt').write_text('repaired behavior\\n', encoding='utf-8')",
            "Path('.hermes-sync-repair.json').write_text(",
            "    json.dumps({'conflicts': [{'path': 'feature.txt', "
            "'decision': 'repair failed runtime behavior'}], "
            "'strategy': 'candidate_repair'}),",
            "    encoding='utf-8',",
            ")",
        ])
        + "\n",
    )
    claude = _python_launcher(
        tmp_path,
        "claude",
        "import json\n"
        "print(json.dumps({'structured_output': "
        "{'verdict': 'green', 'findings': []}}))\n",
    )
    runner = GateRunner()
    sync = SyncConfig(
        repo=repo,
        worktree=candidate_worktree,
        origin="origin",
        upstream="upstream",
        candidate_branch="auto-sync/upstream",
        repo_slug="Oplink76/hermes-agent",
        lock_path=tmp_path / "sync.lock",
    )
    receipt_root = tmp_path / "receipts"
    config = AutonomousSyncConfig(
        sync=sync,
        deploy=DeployConfig(
            install_root=install,
            origin="origin",
            record_root=tmp_path / "deployments",
            repo_slug=sync.repo_slug,
            sync_receipt_root=receipt_root,
            required_check="All required checks pass",
            uv_extras=("all",),
        ),
        receipt_root=receipt_root,
        required_check="All required checks pass",
        resolver_backend="codex",
    )
    github = ProtectedGitHub(origin, repo)
    repair = CodexCandidateRemediator(
        config=sync,
        runner=runner,
        executable=codex,
        prompt="Repair the exact failed candidate.",
    )

    class Remediator:
        def retry_infrastructure(self, candidate, evidence):
            raise AssertionError("green checks must not request infrastructure retry")

        def repair_candidate(self, candidate, *, health_evidence=()):
            return repair.repair_candidate(candidate, health_evidence=health_evidence)

    deployments: list[tuple[Path, int, DeploymentRecord]] = []
    deploy_events: list[tuple[object, ...]] = []
    snapshots = RecordingSnapshots(deploy_events)
    services = RecordingServices(deploy_events)
    health = FailSecondCandidateHealth(deploy_events)

    def deploy_exact(receipt: Path, sha: str, pr_number: int) -> DeploymentRecord:
        loaded = SyncEligibilityReceipt.load(receipt)
        assert loaded.merge_sha == sha
        record = deploy(
            DeployRequest(
                sha=sha,
                pr_number=pr_number,
                actor="hermes-upstream-sync",
                authority_kind="automated_sync",
                authority_record=receipt,
            ),
            config=config.deploy,
            runner=runner,
            github=github,
            snapshots=snapshots,
            services=services,
            health=health,
        )
        deployments.append((receipt, pr_number, record))
        return record

    reviewer = ClaudeConflictReviewer(
        executable=claude,
        runner=runner,
        resolver_backend="codex",
        evidence_dir=receipt_root / "resolutions",
    )
    clean_result = run_autonomous_sync(
        config,
        runner=runner,
        github=github,
        resolver=None,
        reviewer=reviewer,
        remediator=Remediator(),
        deploy_fn=deploy_exact,
        verify_runtime_fn=lambda sha: _git(install, "rev-parse", "HEAD") == sha,
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
    )
    assert clean_result.state is AutonomousSyncState.DEPLOYED
    assert clean_result.merge_sha == clean_result.deployed_sha
    assert _git(install, "rev-parse", "HEAD") == clean_result.deployed_sha
    assert github.prs[7]["head_sha"] == clean_result.candidate_sha
    assert github.prs[7]["merge_sha"] == clean_result.deployed_sha
    assert not list(tmp_path.rglob("approval*.json"))

    (upstream_work / "feature.txt").write_text(
        "failed upstream behavior\n", encoding="utf-8"
    )
    _git(upstream_work, "commit", "-am", "failing upstream change")
    _git(upstream_work, "push", "origin", "main")
    upstream_sha = _git(upstream_work, "rev-parse", "HEAD")
    runner.interrupt_next_uv_sync = True
    with pytest.raises(KeyboardInterrupt, match="process loss after checkout"):
        run_autonomous_sync(
            config,
            runner=runner,
            github=github,
            resolver=None,
            reviewer=reviewer,
            remediator=Remediator(),
            deploy_fn=deploy_exact,
            verify_runtime_fn=lambda sha: (
                _git(install, "rev-parse", "HEAD") == sha
                and services.running == ("ai.hermes.gateway",)
            ),
            clock=lambda: 0.0,
            sleeper=lambda seconds: None,
        )

    interrupted_merge = str(github.prs[8]["merge_sha"])
    assert _git(install, "rev-parse", "HEAD") == interrupted_merge
    recovered = run_autonomous_sync(
        config,
        runner=runner,
        github=github,
        resolver=None,
        reviewer=reviewer,
        remediator=Remediator(),
        deploy_fn=deploy_exact,
        verify_runtime_fn=lambda sha: (
            _git(install, "rev-parse", "HEAD") == sha
            and services.running == ("ai.hermes.gateway",)
        ),
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
    )
    assert recovered.state is AutonomousSyncState.ROLLED_BACK_REVERTED, (
        recovered.reason,
        recovered.reason_code,
        recovered.failed_gate,
    )
    assert recovered.candidate_sha == github.prs[8]["head_sha"]
    assert recovered.merge_sha == interrupted_merge
    assert recovered.details_artifact is not None
    assert recovered.details_artifact.startswith(
        "reconstruction/pending-reconstruction-"
    )
    health.candidate_checks = 2
    result = run_autonomous_sync(
        config,
        runner=runner,
        github=github,
        resolver=None,
        reviewer=reviewer,
        remediator=Remediator(),
        deploy_fn=deploy_exact,
        verify_runtime_fn=lambda sha: (
            _git(install, "rev-parse", "HEAD") == sha
            and services.running == ("ai.hermes.gateway",)
        ),
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert len(deployments) == 3
    clean_deployment = deployments[0][2]
    failed_deployment = deployments[1][2]
    final_deployment = deployments[2][2]
    assert clean_deployment.status == "deployed"
    assert clean_deployment.previous_sha == healthy_base
    assert failed_deployment.status == "rolled_back_healthy"
    assert failed_deployment.previous_sha == clean_result.deployed_sha
    assert failed_deployment.rollback["status"] == "rolled_back_healthy"
    assert "interrupted" in failed_deployment.rollback["trigger"]
    assert final_deployment.status == "deployed"
    assert final_deployment.previous_sha == clean_result.deployed_sha
    assert final_deployment.requested_sha == result.deployed_sha
    assert _git(install, "rev-parse", "HEAD") == result.deployed_sha
    assert snapshots.restore_count == 1
    assert services.stop_count == 3
    assert services.start_count == 3
    assert services.running == ("ai.hermes.gateway",)
    health_events = [event for event in deploy_events if event[0] == "health"]
    assert [event[1] for event in health_events] == [
        clean_deployment.requested_sha,
        clean_result.deployed_sha,
        final_deployment.requested_sha,
    ]
    assert [event[4] for event in health_events] == [True, False, True]
    failed_pr = github.prs[8]
    revert_pr = github.prs[9]
    repaired_pr = github.prs[10]
    assert failed_pr["merge_sha"] == failed_deployment.requested_sha
    assert repaired_pr["merge_sha"] == result.deployed_sha
    assert health_events[1][1:] == (
        clean_result.deployed_sha,
        ("ai.hermes.gateway",),
        False,
        False,
    )
    quarantine_artifacts = list(config.quarantine_root.glob("*.json"))
    assert len(quarantine_artifacts) == 1
    quarantine = json.loads(quarantine_artifacts[0].read_text(encoding="utf-8"))
    assert quarantine["candidate_sha"] == failed_pr["head_sha"]
    assert quarantine["merge_sha"] == failed_deployment.requested_sha
    stored_deployments = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "deployments").glob("*.json")
    ]
    assert {record["status"] for record in stored_deployments} == {
        "rolled_back_healthy",
        "deployed",
    }
    assert {record["previous_sha"] for record in stored_deployments} == {
        healthy_base,
        clean_result.deployed_sha,
    }
    assert github.created_heads[0:2] == ["auto-sync/upstream"] * 2
    assert github.created_heads[2].startswith("auto-sync/revert-")
    assert github.created_heads[3] == "auto-sync/upstream"
    _git(repo, "fetch", "origin", "main")
    revert_merge_sha = str(revert_pr["merge_sha"])
    revert_head_sha = str(revert_pr["head_sha"])
    assert _git(repo, "rev-list", "--parents", "-n", "1", revert_merge_sha).split() == [
        revert_merge_sha,
        failed_deployment.requested_sha,
        revert_head_sha,
    ]
    assert _git(repo, "rev-parse", f"{revert_merge_sha}^{{tree}}") == _git(
        repo, "rev-parse", f"{clean_result.deployed_sha}^{{tree}}"
    )
    assert SyncEligibilityReceipt.load(deployments[2][0]).review is not None
    assert not list(tmp_path.rglob("approval*.json"))
    final = tmp_path / "final"
    subprocess.run(
        ["git", "clone", str(origin), str(final)], check=True, capture_output=True
    )
    assert (final / "feature.txt").read_text(encoding="utf-8") == "repaired behavior\n"
    assert _git(final, "rev-parse", "HEAD") == result.deployed_sha
    assert _git(install, "rev-parse", "HEAD") == result.deployed_sha
    assert clean_upstream_sha != healthy_base
    assert upstream_sha != healthy_base
    assert len(runner.local_gate_calls) >= 6
