from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ops.cloudadvisor.hermes_ops.command import SubprocessCommandRunner
from ops.cloudadvisor.hermes_ops.deploy import DeployConfig, DeploymentRecord
from ops.cloudadvisor.hermes_ops.health import HealthCheck
from ops.cloudadvisor.hermes_ops.sync import CheckResult, SyncConfig
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
        launcher.write_bytes(
            f'@"{sys.executable}" "{driver}" %*\r\n'.encode("utf-8")
        )
        return launcher
    launcher = tmp_path / name
    launcher.write_text(
        f"#!{sys.executable}\nexec(compile(open({str(driver)!r}, "
        "encoding='utf-8').read(), "
        f"{str(driver)!r}, 'exec'))\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


class GateRunner(SubprocessCommandRunner):
    def __init__(self):
        self.local_gate_calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
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
            base_sha=self._remote_sha("refs/heads/main"),
            head_sha=self._remote_sha(f"refs/heads/{row['head']}"),
            required_check="All required checks pass",
            required_check_conclusion="success",
            workflow_run_id=1000 + pr_number,
            required_check_run_id=2000 + pr_number,
            merge_sha=row["merge_sha"],
        )

    def merge_exact(self, pr_number: int, *, expected_head: str):
        row = self.prs[pr_number]
        assert row["state"] == "open"
        assert self.expected_base_sha == self._remote_sha("refs/heads/main")
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
        return merge_sha


def test_full_real_controller_rollback_revert_reconstruct_review_receipt_deploy(
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
    _git(repo, "add", "feature.txt")
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
        "failed upstream behavior\n", encoding="utf-8"
    )
    _git(upstream_work, "commit", "-am", "upstream change")
    _git(upstream_work, "push", "origin", "main")
    upstream_sha = _git(upstream_work, "rev-parse", "HEAD")
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
            install_root=tmp_path / "install",
            origin="origin",
            record_root=tmp_path / "deployments",
            repo_slug=sync.repo_slug,
            sync_receipt_root=receipt_root,
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
            return repair.repair_candidate(
                candidate, health_evidence=health_evidence
            )

    deployments: list[tuple[Path, str, int]] = []

    def deploy(receipt: Path, sha: str, pr_number: int) -> DeploymentRecord:
        loaded = SyncEligibilityReceipt.load(receipt)
        assert loaded.merge_sha == sha
        deployments.append((receipt, sha, pr_number))
        if len(deployments) == 1:
            return DeploymentRecord(
                id="failed",
                requested_sha=sha,
                previous_sha=healthy_base,
                snapshot={},
                runtime_before={},
                runtime_after={},
                checks=(HealthCheck("runtime:default", False),),
                status="rolled_back_healthy",
                rollback={"status": "healthy"},
            )
        return DeploymentRecord(
            id="deployed",
            requested_sha=sha,
            previous_sha=healthy_base,
            snapshot={},
            runtime_before={},
            runtime_after={},
            checks=(HealthCheck("runtime:default", True),),
            status="deployed",
            rollback=None,
        )

    result = run_autonomous_sync(
        config,
        runner=runner,
        github=github,
        resolver=None,
        reviewer=ClaudeConflictReviewer(
            executable=claude,
            runner=runner,
            resolver_backend="codex",
            evidence_dir=receipt_root / "resolutions",
        ),
        remediator=Remediator(),
        deploy_fn=deploy,
        verify_runtime_fn=lambda sha: sha == healthy_base,
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
    )

    assert result.state is AutonomousSyncState.DEPLOYED
    assert len(deployments) == 2
    assert github.created_heads[0] == "auto-sync/upstream"
    assert github.created_heads[1].startswith("auto-sync/revert-")
    assert github.created_heads[2] == "auto-sync/upstream"
    assert SyncEligibilityReceipt.load(deployments[1][0]).review is not None
    final = tmp_path / "final"
    subprocess.run(
        ["git", "clone", str(origin), str(final)], check=True, capture_output=True
    )
    assert (final / "feature.txt").read_text(encoding="utf-8") == "repaired behavior\n"
    assert upstream_sha != healthy_base
    assert len(runner.local_gate_calls) >= 4
