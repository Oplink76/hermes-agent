from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

import hermes_cli.kanban_repository as repository_module
from hermes_cli.kanban_repository import (
    EvidenceWorkspaceError,
    EvidenceWorkspaceResult,
    PreparedRefCASResult,
    PreparedRefRecoveryResult,
    RELEASE_CANDIDATE_REF_PREFIX,
    RepositoryConfigurationError,
    RefreshRequest,
    TargetHeadsObservation,
    VerificationCommand,
    VerificationProfile,
    advance_prepared_candidate_ref,
    build_verification_receipt,
    build_verification_receipt_key,
    commit_contains,
    delete_prepared_candidate_ref,
    delete_release_candidate_ref,
    inspect_evidence_workspace,
    inspect_prepared_candidate_ref,
    load_repository_contract,
    observe_ci_workflow_runs,
    observe_target_heads,
    refresh_story_branch,
    resolve_commit,
    restore_generated_paths,
    run_verification,
    verification_receipt_matches,
    verification_receipt_from_payload,
    verification_result_payload,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "tests@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Repository Tests"],
        check=True,
    )
    (repo / "dashboard").mkdir()
    (repo / "dashboard" / "index.html").write_text("index\n", encoding="utf-8")
    (repo / "dashboard" / "data.json").write_text("{}\n", encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run_tests.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha],
        check=True,
    )
    return repo


def board_metadata() -> dict[str, object]:
    return {
        "repository": {
            "base_ref": "refs/remotes/origin/main",
            "target_branch": "main",
            "verification_profiles": {
                "story_integration": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 1800,
                    }
                ],
                "epic_release": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 1800,
                    }
                ],
            },
            "ci_observation": {
                "provider": "github_actions",
                "required_workflows": ["CI", "Deploy Test"],
            },
            "boundary_evidence": {
                "test_globs": ["tests/**"],
                "fixture_globs": ["tests/fixtures/**"],
                "generated_paths": ["dashboard/index.html", "dashboard/data.json"],
            },
        }
    }


def test_contract_normalizes_commands_and_generated_paths(repository: Path):
    contract = load_repository_contract(board_metadata(), repo_root=repository)

    assert contract.repo_root == repository.resolve()
    assert contract.base_ref == "refs/remotes/origin/main"
    assert contract.target_branch == "main"
    assert contract.generated_paths == (
        PurePosixPath("dashboard/index.html"),
        PurePosixPath("dashboard/data.json"),
    )
    assert contract.verification["story_integration"].commands[0].argv == (
        "bash",
        "scripts/run_tests.sh",
    )
    assert contract.verification["story_integration"].commands[0].workdir == PurePosixPath(".")
    assert contract.ci_workflows == ("CI", "Deploy Test")
    assert len(contract.digest) == 64


def test_contract_digest_is_order_independent_for_mapping_keys(repository: Path):
    first = board_metadata()
    second = board_metadata()
    repository_policy = second["repository"]
    assert isinstance(repository_policy, dict)
    second["repository"] = {
        "boundary_evidence": repository_policy["boundary_evidence"],
        "ci_observation": repository_policy["ci_observation"],
        "verification_profiles": repository_policy["verification_profiles"],
        "target_branch": repository_policy["target_branch"],
        "base_ref": repository_policy["base_ref"],
    }

    assert load_repository_contract(first, repo_root=repository).digest == load_repository_contract(
        second, repo_root=repository
    ).digest


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda policy: policy.pop("base_ref"), "missing_base_ref"),
        (lambda policy: policy.update(base_ref=""), "malformed_base_ref"),
        (lambda policy: policy.update(target_branch=""), "malformed_target_branch"),
        (lambda policy: policy.update(unknown=True), "unknown_key"),
    ],
)
def test_contract_rejects_malformed_top_level_policy(repository: Path, mutator, code: str):
    metadata = board_metadata()
    policy = metadata["repository"]
    assert isinstance(policy, dict)
    mutator(policy)

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        load_repository_contract(metadata, repo_root=repository)

    assert exc_info.value.code == code


def test_contract_rejects_unknown_nested_keys(repository: Path):
    metadata = board_metadata()
    policy = metadata["repository"]
    assert isinstance(policy, dict)
    profiles = policy["verification_profiles"]
    assert isinstance(profiles, dict)
    profiles["story_integration"][0]["shell"] = True

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        load_repository_contract(metadata, repo_root=repository)

    assert exc_info.value.code == "unknown_key"


@pytest.mark.parametrize("generated_path", ["/tmp/output.txt", "../outside.txt", "dashboard/missing.txt"])
def test_contract_rejects_invalid_generated_paths(repository: Path, generated_path: str):
    metadata = board_metadata()
    policy = metadata["repository"]
    assert isinstance(policy, dict)
    boundary = policy["boundary_evidence"]
    assert isinstance(boundary, dict)
    boundary["generated_paths"] = [generated_path]

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        load_repository_contract(metadata, repo_root=repository)

    assert exc_info.value.code in {"invalid_path", "untracked_path"}


def test_inspect_evidence_workspace_allows_declared_generated_and_ignored_output(
    repository: Path,
):
    pinned_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / ".gitignore").write_text("artifacts/\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", ".gitignore"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore artifacts"],
        check=True,
        capture_output=True,
    )
    pinned_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "dashboard" / "index.html").write_text("generated\n", encoding="utf-8")
    (repository / "artifacts").mkdir()
    (repository / "artifacts" / "report.txt").write_text("evidence\n", encoding="utf-8")

    result = inspect_evidence_workspace(
        repository,
        pinned_sha,
        (PurePosixPath("dashboard/index.html"), PurePosixPath("dashboard/data.json")),
    )

    assert isinstance(result, EvidenceWorkspaceResult)
    assert result.branch_head == pinned_sha
    assert result.declared_generated == (PurePosixPath("dashboard/index.html"),)
    assert result.undeclared_tracked == ()
    assert result.untracked == ()


def test_inspect_evidence_workspace_reports_undeclared_tracked_and_untracked_output(
    repository: Path,
):
    pinned_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "scripts" / "run_tests.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (repository / "artifact.txt").write_text("diagnostic\n", encoding="utf-8")

    result = inspect_evidence_workspace(
        repository,
        pinned_sha,
        (PurePosixPath("dashboard/index.html"),),
    )

    assert result.branch_head == pinned_sha
    assert result.undeclared_tracked == ("scripts/run_tests.sh",)
    assert result.untracked == ("artifact.txt",)


def test_restore_generated_paths_restores_only_explicit_allowlist(repository: Path):
    pinned_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "dashboard" / "index.html").write_text("generated\n", encoding="utf-8")
    (repository / "scripts" / "run_tests.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    restore_generated_paths(
        repository,
        pinned_sha,
        (PurePosixPath("dashboard/index.html"),),
    )

    assert (repository / "dashboard" / "index.html").read_text(encoding="utf-8") == "index\n"
    assert (repository / "scripts" / "run_tests.sh").read_text(encoding="utf-8") == "#!/bin/sh\nexit 1\n"


def test_restore_generated_paths_rejects_unvalidated_path(repository: Path):
    pinned_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(EvidenceWorkspaceError, match="invalid_generated_path"):
        restore_generated_paths(repository, pinned_sha, (PurePosixPath("../outside"),))


def test_contract_rejects_workdir_escape(repository: Path):
    metadata = board_metadata()
    policy = metadata["repository"]
    assert isinstance(policy, dict)
    profiles = policy["verification_profiles"]
    assert isinstance(profiles, dict)
    profiles["story_integration"][0]["workdir"] = "../outside"

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        load_repository_contract(metadata, repo_root=repository)

    assert exc_info.value.code == "invalid_workdir"


def test_resolve_commit_uses_configured_ref_not_checked_out_head(repository: Path):
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "later.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "later"],
        check=True,
        capture_output=True,
    )

    configured = resolve_commit(repository, "refs/remotes/origin/main")
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert configured != head
    assert len(configured) == 40


def test_resolve_commit_rejects_missing_ref(repository: Path):
    with pytest.raises(RepositoryConfigurationError) as exc_info:
        resolve_commit(repository, "refs/remotes/origin/missing")

    assert exc_info.value.code == "missing_ref"


def test_resolve_commit_rejects_ambiguous_ref(repository: Path):
    subprocess.run(["git", "-C", str(repository), "branch", "shared"], check=True)
    subprocess.run(["git", "-C", str(repository), "tag", "shared"], check=True)

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        resolve_commit(repository, "shared")

    assert exc_info.value.code == "missing_ref"


def test_commit_contains_accepts_equal_and_ancestor_commits(repository: Path):
    base_sha = _git(repository, "rev-parse", "HEAD")
    child_sha = _commit(repository, "child.txt", "child\n", "child")

    assert commit_contains(repository, child_sha, child_sha) is True
    assert commit_contains(repository, child_sha, base_sha) is True
    assert commit_contains(repository, base_sha, child_sha) is False
    with pytest.raises(RepositoryConfigurationError) as missing:
        commit_contains(repository, "f" * 40, "f" * 40)
    assert missing.value.code == "ancestry_check_failed"


@pytest.mark.parametrize("field", ["descendant_sha", "ancestor_sha"])
def test_commit_contains_refuses_malformed_fact_sha(repository: Path, field: str):
    sha = _git(repository, "rev-parse", "HEAD")
    values = {"descendant_sha": sha, "ancestor_sha": sha}
    values[field] = "not-a-full-sha"

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        commit_contains(repository, **values)

    assert exc_info.value.code == f"malformed_{field}"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _prepared_ref_fixture(
    repository: Path, *, checked_out: bool = False
) -> tuple[str, str, str, str]:
    target_ref = "refs/heads/main"
    candidate_ref = "refs/hermes/integration-candidates/exact"
    pre_sha = _git(repository, "rev-parse", target_ref)
    _git(repository, "switch", "-c", "candidate-source")
    candidate_sha = _commit(
        repository, "candidate.txt", "candidate\n", "candidate"
    )
    _git(repository, "update-ref", candidate_ref, candidate_sha)
    if checked_out:
        _git(repository, "switch", "main")
    return target_ref, candidate_ref, pre_sha, candidate_sha


def test_prepared_ref_cas_uses_one_exact_update_ref_path(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    target_ref, candidate_ref, pre_sha, candidate_sha = _prepared_ref_fixture(repository)
    real_git = repository_module._prepared_ref_git
    calls: list[tuple[str, ...]] = []

    def capture(path: Path, *args: str):
        calls.append(args)
        return real_git(path, *args)

    monkeypatch.setattr(repository_module, "_prepared_ref_git", capture)

    result = advance_prepared_candidate_ref(
        repository,
        target_ref=target_ref,
        candidate_ref=candidate_ref,
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
    )

    assert result == PreparedRefCASResult("advanced", candidate_sha)
    assert _git(repository, "rev-parse", target_ref) == candidate_sha
    assert [call for call in calls if call[:1] == ("update-ref",)] == [
        ("update-ref", target_ref, candidate_sha, pre_sha)
    ]
    assert not any(
        command in call
        for call in calls
        for command in ("merge", "read-tree", "reset", "clean", "stash")
    )


def test_prepared_ref_cas_refuses_target_checked_out_in_any_worktree(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    target_ref, candidate_ref, pre_sha, candidate_sha = _prepared_ref_fixture(
        repository, checked_out=True
    )
    real_git = repository_module._prepared_ref_git
    calls: list[tuple[str, ...]] = []

    def capture(path: Path, *args: str):
        calls.append(args)
        return real_git(path, *args)

    monkeypatch.setattr(repository_module, "_prepared_ref_git", capture)

    result = advance_prepared_candidate_ref(
        repository,
        target_ref=target_ref,
        candidate_ref=candidate_ref,
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
    )

    assert result == PreparedRefCASResult("checked_out", pre_sha)
    assert _git(repository, "rev-parse", target_ref) == pre_sha
    assert not any(call[:1] == ("update-ref",) for call in calls)


def test_prepared_ref_cas_reports_target_moved_without_ref_movement(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    target_ref, candidate_ref, pre_sha, candidate_sha = _prepared_ref_fixture(repository)
    moved_sha = _commit(repository, "operator.txt", "moved\n", "operator move")
    _git(repository, "update-ref", target_ref, moved_sha, pre_sha)
    real_git = repository_module._prepared_ref_git
    calls: list[tuple[str, ...]] = []

    def capture(path: Path, *args: str):
        calls.append(args)
        return real_git(path, *args)

    monkeypatch.setattr(repository_module, "_prepared_ref_git", capture)

    result = advance_prepared_candidate_ref(
        repository,
        target_ref=target_ref,
        candidate_ref=candidate_ref,
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
    )

    assert result == PreparedRefCASResult("target_moved", moved_sha)
    assert _git(repository, "rev-parse", target_ref) == moved_sha
    assert not any(call[:1] == ("update-ref",) for call in calls)


def test_prepared_ref_cas_loss_reports_target_moved_and_preserves_winner(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    target_ref, candidate_ref, pre_sha, candidate_sha = _prepared_ref_fixture(repository)
    winner_sha = _commit(repository, "winner.txt", "winner\n", "CAS winner")
    real_git = repository_module._prepared_ref_git
    intercepted = False

    def lose_cas(path: Path, *args: str):
        nonlocal intercepted
        if args == ("update-ref", target_ref, candidate_sha, pre_sha):
            intercepted = True
            won = real_git(path, "update-ref", target_ref, winner_sha, pre_sha)
            assert won.returncode == 0
            return subprocess.CompletedProcess(
                ["git", "update-ref", target_ref], 1, "", "CAS lost"
            )
        return real_git(path, *args)

    monkeypatch.setattr(repository_module, "_prepared_ref_git", lose_cas)

    result = advance_prepared_candidate_ref(
        repository,
        target_ref=target_ref,
        candidate_ref=candidate_ref,
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
    )

    assert intercepted is True
    assert result == PreparedRefCASResult("target_moved", winner_sha)
    assert _git(repository, "rev-parse", target_ref) == winner_sha


def test_prepared_ref_cas_recognizes_reflected_cas_without_second_update(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    target_ref, candidate_ref, pre_sha, candidate_sha = _prepared_ref_fixture(repository)
    first = advance_prepared_candidate_ref(
        repository,
        target_ref=target_ref,
        candidate_ref=candidate_ref,
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
    )
    real_git = repository_module._prepared_ref_git
    calls: list[tuple[str, ...]] = []

    def capture(path: Path, *args: str):
        calls.append(args)
        return real_git(path, *args)

    monkeypatch.setattr(repository_module, "_prepared_ref_git", capture)
    reflected = advance_prepared_candidate_ref(
        repository,
        target_ref=target_ref,
        candidate_ref=candidate_ref,
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
    )

    assert first == PreparedRefCASResult("advanced", candidate_sha)
    assert reflected == PreparedRefCASResult("reflected", candidate_sha)
    assert not any(call[:1] == ("update-ref",) for call in calls)


@pytest.mark.parametrize(
    ("scenario", "expected_kind"),
    [
        ("preimage", "preimage"),
        ("candidate", "candidate"),
        ("descendant", "descendant"),
        ("diverged", "diverged"),
    ],
)
def test_prepared_candidate_ref_inspection_classifies_recovery_boundary(
    repository: Path, scenario: str, expected_kind: str
):
    target_ref, _candidate_ref, pre_sha, candidate_sha = _prepared_ref_fixture(
        repository
    )
    expected_current = pre_sha
    if scenario == "candidate":
        _git(repository, "update-ref", target_ref, candidate_sha, pre_sha)
        expected_current = candidate_sha
    elif scenario == "descendant":
        later_sha = _commit(repository, "later.txt", "later\n", "later")
        _git(repository, "update-ref", target_ref, later_sha, pre_sha)
        expected_current = later_sha
    elif scenario == "diverged":
        _git(repository, "switch", "main")
        expected_current = _commit(
            repository, "operator.txt", "operator\n", "operator"
        )

    result = inspect_prepared_candidate_ref(
        repository,
        target_ref=target_ref,
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
    )

    assert result == PreparedRefRecoveryResult(expected_kind, expected_current)


def test_delete_prepared_candidate_ref_uses_exact_old_value_and_preserves_mismatch(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    _target_ref, candidate_ref, _pre_sha, candidate_sha = _prepared_ref_fixture(
        repository
    )
    real_git = repository_module._prepared_ref_git
    calls: list[tuple[str, ...]] = []

    def capture(path: Path, *args: str):
        calls.append(args)
        return real_git(path, *args)

    monkeypatch.setattr(repository_module, "_prepared_ref_git", capture)

    assert delete_prepared_candidate_ref(
        repository, candidate_ref=candidate_ref, candidate_sha="f" * 40
    ) is False
    assert _git(repository, "rev-parse", candidate_ref) == candidate_sha
    assert delete_prepared_candidate_ref(
        repository, candidate_ref=candidate_ref, candidate_sha=candidate_sha
    ) is True
    assert subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", candidate_ref],
        capture_output=True,
        text=True,
    ).returncode != 0
    assert [call for call in calls if call[:2] == ("update-ref", "-d")] == [
        ("update-ref", "-d", candidate_ref, candidate_sha)
    ]


def test_release_candidate_ref_uses_release_namespace_and_exact_cleanup(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate_ref = f"{RELEASE_CANDIDATE_REF_PREFIX}exact"
    candidate_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", candidate_ref, candidate_sha)
    calls: list[tuple[str, ...]] = []
    real_git = repository_module._prepared_ref_git

    def capture(path: Path, *args: str):
        calls.append(args)
        return real_git(path, *args)

    monkeypatch.setattr(repository_module, "_prepared_ref_git", capture)

    assert delete_release_candidate_ref(
        repository, candidate_ref=candidate_ref, candidate_sha="f" * 40
    ) is False
    assert _git(repository, "rev-parse", candidate_ref) == candidate_sha
    assert delete_release_candidate_ref(
        repository, candidate_ref=candidate_ref, candidate_sha=candidate_sha
    ) is True
    assert [call for call in calls if call[:2] == ("update-ref", "-d")] == [
        ("update-ref", "-d", candidate_ref, candidate_sha)
    ]

    with pytest.raises(RepositoryConfigurationError) as exc_info:
        delete_release_candidate_ref(
            repository,
            candidate_ref="refs/hermes/integration-candidates/not-release",
            candidate_sha=candidate_sha,
        )
    assert exc_info.value.code == "malformed_release_candidate_ref"


def test_release_candidate_ref_preserves_repointed_and_mismatched_refs(
    repository: Path,
):
    """Exact ref deletion and preservation on expected-old mismatch at repo level.

    A repointed release-candidate ref must survive when the invalidation
    expected-old SHA no longer matches, and must be deletable only when the
    exact current SHA is expected.  An already-absent ref is an idempotent
    success and never re-creates the ref.
    """

    candidate_ref = f"{RELEASE_CANDIDATE_REF_PREFIX}exact"
    first_sha = _git(repository, "rev-parse", "HEAD")
    later_sha = _commit(repository, "later.txt", "later\n", "later")
    _git(repository, "update-ref", candidate_ref, first_sha)

    # Repointed — expected old SHA no longer matches → preserved.
    _git(repository, "update-ref", candidate_ref, later_sha)
    assert delete_release_candidate_ref(
        repository, candidate_ref=candidate_ref, candidate_sha=first_sha
    ) is False
    assert _git(repository, "rev-parse", candidate_ref) == later_sha

    # Exact current SHA → deleted.
    assert delete_release_candidate_ref(
        repository, candidate_ref=candidate_ref, candidate_sha=later_sha
    ) is True
    assert subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", candidate_ref],
        capture_output=True,
        text=True,
    ).returncode != 0

    # Idempotent — absent ref is a success.
    assert delete_release_candidate_ref(
        repository, candidate_ref=candidate_ref, candidate_sha=later_sha
    ) is True
    assert subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", candidate_ref],
        capture_output=True,
        text=True,
    ).returncode != 0


@pytest.mark.parametrize(
    ("scenario", "expected_kind", "expected_current", "expected_updates"),
    [
        ("advance", "advanced", "b" * 40, 1),
        ("reflected", "reflected", "b" * 40, 0),
        ("checked_out", "checked_out", "a" * 40, 0),
        ("target_moved", "target_moved", "c" * 40, 0),
        ("cas_lost", "target_moved", "c" * 40, 1),
    ],
)
def test_prepared_ref_cas_fake_git_proves_single_update_and_refusal_immutability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_kind: str,
    expected_current: str,
    expected_updates: int,
):
    target_ref = "refs/heads/epic"
    candidate_ref = "refs/hermes/integration-candidates/exact"
    pre_sha = "a" * 40
    candidate_sha = "b" * 40
    winner_sha = "c" * 40
    state = {
        "target": (
            candidate_sha
            if scenario == "reflected"
            else winner_sha if scenario == "target_moved" else pre_sha
        )
    }
    calls: list[tuple[str, ...]] = []

    def fake_git(_path: Path, *args: str):
        calls.append(args)
        if args[:2] == ("rev-parse", "--verify"):
            ref = args[2].removesuffix("^{commit}")
            value = candidate_sha if ref == candidate_ref else state["target"]
            return subprocess.CompletedProcess(["git", *args], 0, f"{value}\n", "")
        if args == ("worktree", "list", "--porcelain"):
            branch = (
                f"worktree /tmp/epic\nHEAD {pre_sha}\nbranch {target_ref}\n"
                if scenario == "checked_out"
                else f"worktree /tmp/operator\nHEAD {pre_sha}\nbranch refs/heads/operator\n"
            )
            return subprocess.CompletedProcess(["git", *args], 0, branch, "")
        if args == ("update-ref", target_ref, candidate_sha, pre_sha):
            if scenario == "cas_lost":
                state["target"] = winner_sha
                return subprocess.CompletedProcess(["git", *args], 1, "", "CAS lost")
            state["target"] = candidate_sha
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        raise AssertionError(f"unexpected Git call: {args}")

    monkeypatch.setattr(repository_module, "_prepared_ref_git", fake_git)

    result = advance_prepared_candidate_ref(
        tmp_path,
        target_ref=target_ref,
        candidate_ref=candidate_ref,
        pre_sha=pre_sha,
        candidate_sha=candidate_sha,
    )

    updates = [call for call in calls if call[:1] == ("update-ref",)]
    assert result == PreparedRefCASResult(expected_kind, expected_current)
    assert updates == (
        [("update-ref", target_ref, candidate_sha, pre_sha)]
        if expected_updates
        else []
    )
    if scenario in {"checked_out", "target_moved"}:
        assert state["target"] == expected_current


def _refresh_fixture(repository: Path) -> tuple[Path, Path]:
    _git(repository, "branch", "story")
    _git(repository, "branch", "epic")
    return repository, repository


def _refresh_request(
    story: Path,
    epic: Path,
    *,
    story_sha: str | None = None,
    epic_tip_sha: str | None = None,
) -> RefreshRequest:
    return RefreshRequest(
        repo_root=story,
        story_id="story-fixture",
        story_worktree=story,
        story_branch="story",
        story_sha=story_sha or _git(story, "rev-parse", "story"),
        epic_branch="epic",
        epic_tip_sha=epic_tip_sha or _git(epic, "rev-parse", "epic"),
    )


def test_refresh_story_branch_advances_clean_story_by_isolated_cas(repository: Path, tmp_path: Path):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    before = _git(story, "rev-parse", "story")
    remote_main_before = _git(repository, "rev-parse", "refs/remotes/origin/main")

    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    assert result.kind == "refreshed"
    assert result.before_sha == before
    assert result.after_sha == _git(story, "rev-parse", "story")
    assert result.after_sha != before
    assert (story / "epic.txt").read_text(encoding="utf-8") == "epic\n"
    assert _git(story, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(repository, "rev-parse", "refs/remotes/origin/main") == remote_main_before


def test_refresh_story_branch_returns_dirty_evidence_without_touching_story(repository: Path, tmp_path: Path):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    dirty = story / "operator-note.txt"
    dirty.write_text("keep me\n", encoding="utf-8")
    before = _git(story, "rev-parse", "story")
    remote_main_before = _git(repository, "rev-parse", "refs/remotes/origin/main")

    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    assert result.kind == "dirty"
    assert result.before_sha == before
    assert result.after_sha is None
    assert result.dirty_paths == ("operator-note.txt",)
    assert _git(story, "rev-parse", "story") == before
    assert dirty.read_text(encoding="utf-8") == "keep me\n"
    assert _git(repository, "rev-parse", "refs/remotes/origin/main") == remote_main_before


def test_refresh_story_branch_returns_conflict_and_retains_isolated_evidence(
    repository: Path, tmp_path: Path
):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "story")
    _commit(story, "shared.txt", "story\n", "story change")
    _git(repository, "checkout", "epic")
    _commit(epic, "shared.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    before = _git(story, "rev-parse", "story")
    remote_main_before = _git(repository, "rev-parse", "refs/remotes/origin/main")

    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    assert result.kind == "conflict"
    assert result.before_sha == before
    assert result.after_sha is None
    assert result.conflict_worktree is not None
    assert result.conflict_worktree.is_dir()
    assert result.conflict_paths == ("shared.txt",)
    assert _git(story, "rev-parse", "story") == before
    assert _git(story, "status", "--porcelain", "--untracked-files=all") == ""
    assert _git(repository, "rev-parse", "refs/remotes/origin/main") == remote_main_before


def test_refresh_story_branch_returns_source_moved_evidence(repository: Path, tmp_path: Path):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    pinned_story_sha = _git(story, "rev-parse", "story")
    remote_main_before = _git(repository, "rev-parse", "refs/remotes/origin/main")
    _commit(story, "story.txt", "moved\n", "move story source")
    moved_story_sha = _git(story, "rev-parse", "story")

    result = refresh_story_branch(
        _refresh_request(story, epic, story_sha=pinned_story_sha)
    )

    assert result.kind == "source_moved"
    assert result.before_sha == pinned_story_sha
    assert result.current_sha == moved_story_sha
    assert _git(story, "rev-parse", "story") == moved_story_sha
    assert _git(repository, "rev-parse", "refs/remotes/origin/main") == remote_main_before


def test_refresh_story_branch_detects_source_move_between_merge_and_cas(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    before = _git(story, "rev-parse", "story")
    original_refresh_git = repository_module._refresh_git
    moved = False

    def move_story_before_cas(path: Path, *args: str, **kwargs):
        nonlocal moved
        if args[:1] == ("update-ref",) and not moved:
            moved = True
            _commit(story, "story-late.txt", "late\n", "story moved during refresh")
        return original_refresh_git(path, *args, **kwargs)

    monkeypatch.setattr(repository_module, "_refresh_git", move_story_before_cas)
    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    moved_story_sha = _git(story, "rev-parse", "story")
    assert result.kind == "source_moved"
    assert result.before_sha == before
    assert result.current_sha == moved_story_sha
    assert moved_story_sha != before
    assert _git(story, "status", "--porcelain", "--untracked-files=all") == ""


def test_refresh_story_branch_rechecks_dirty_worktree_before_cas(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    story, epic = _refresh_fixture(repository)
    _git(repository, "checkout", "epic")
    _commit(epic, "epic.txt", "epic\n", "epic change")
    _git(repository, "checkout", "story")
    before = _git(story, "rev-parse", "story")
    original_refresh_git = repository_module._refresh_git
    status_checks = 0
    dirtied = False

    def dirty_story_before_cas(path: Path, *args: str, **kwargs):
        nonlocal dirtied, status_checks
        if args[:1] == ("status",):
            status_checks += 1
        if args[:1] == ("status",) and status_checks == 2 and not dirtied:
            dirtied = True
            (story / "README.md").write_text("operator edit\n", encoding="utf-8")
        return original_refresh_git(path, *args, **kwargs)

    monkeypatch.setattr(repository_module, "_refresh_git", dirty_story_before_cas)
    result = refresh_story_branch(_refresh_request(story, epic, story_sha=before))

    assert result.kind == "dirty"
    assert result.before_sha == before
    assert result.after_sha is None
    assert result.dirty_paths == ("README.md",)
    assert _git(story, "rev-parse", "story") == before
    assert (story / "README.md").read_text(encoding="utf-8") == "operator edit\n"


def _write_verification_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _verification_profile(*commands: VerificationCommand) -> VerificationProfile:
    return VerificationProfile(tuple(commands))


def _verification_command(
    candidate: Path,
    executable: Path,
    *args: str,
    workdir: str = ".",
    timeout_seconds: int = 5,
) -> VerificationCommand:
    workdir_path = candidate / Path(workdir)
    return VerificationCommand(
        argv=(executable.relative_to(workdir_path).as_posix(), *args),
        workdir=PurePosixPath(workdir),
        timeout_seconds=timeout_seconds,
    )


def test_run_verification_uses_configured_argv_workdir_and_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = tmp_path / "candidate"
    script = _write_verification_script(
        candidate / "nested" / "record.py",
        "import json, os; print('api_key=should-not-survive'); print(json.dumps({'argv': __import__('sys').argv[1:], 'cwd': os.getcwd(), 'secret': os.environ.get('R03_SECRET')}))",
    )
    monkeypatch.setenv("R03_SECRET", "must-not-cross-boundary")
    profile = _verification_profile(
        _verification_command(
            candidate,
            script,
            "--first",
            "value",
            workdir="nested",
            timeout_seconds=7,
        )
    )

    result = run_verification(
        profile,
        candidate,
        source_sha="source-sha",
        candidate_sha="candidate-sha",
        contract_digest="contract-digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "passed"
    assert result.source_sha == "source-sha"
    assert result.candidate_sha == "candidate-sha"
    assert result.contract_digest == "contract-digest"
    assert result.profile == "story_integration"
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.status == "passed"
    assert step.argv[1:] == ("--first", "value")
    assert step.workdir == PurePosixPath("nested")
    payload = json.loads(step.stdout_tail.splitlines()[-1])
    assert payload["argv"] == ["--first", "value"]
    assert Path(payload["cwd"]) == candidate / "nested"
    assert payload["secret"] is None
    assert "should-not-survive" not in step.stdout_tail
    assert "[REDACTED]" in step.stdout_tail


def test_run_verification_stops_on_nonzero_and_caps_output(tmp_path: Path):
    candidate = tmp_path / "candidate"
    failing = _write_verification_script(
        candidate / "fail.py",
        "print('x' * 10000); raise SystemExit(3)",
    )
    marker = candidate / "should-not-run"
    following = _write_verification_script(
        candidate / "following.py",
        f"__import__('pathlib').Path({str(marker)!r}).touch()",
    )
    profile = _verification_profile(
        _verification_command(candidate, failing),
        _verification_command(candidate, following),
    )

    result = run_verification(
        profile,
        candidate,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="epic_release",
        subject_id="epic-1",
    )

    assert result.status == "failed"
    assert len(result.steps) == 1
    assert result.steps[0].returncode == 3
    assert len(result.steps[0].stdout_tail) <= 4096
    assert not marker.exists()


def test_run_verification_classifies_timeout_as_infrastructure_error(tmp_path: Path):
    candidate = tmp_path / "candidate"
    sleeper = _write_verification_script(
        candidate / "sleep.py",
        "__import__('time').sleep(2)",
    )
    profile = _verification_profile(
        _verification_command(candidate, sleeper, timeout_seconds=1),
    )

    result = run_verification(
        profile,
        candidate,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "infrastructure_error"
    assert len(result.steps) == 1
    assert result.steps[0].status == "infrastructure_error"
    assert result.steps[0].error == "timeout"


def test_run_verification_missing_profile_is_configuration_error(tmp_path: Path):
    result = run_verification(
        None,
        tmp_path,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "configuration_error"
    assert result.steps == ()


def test_verification_receipt_key_changes_for_each_meaningful_input(
    repository: Path, monkeypatch: pytest.MonkeyPatch
):
    runner = repository / "scripts" / "run_tests.sh"
    runner.chmod(0o755)
    profile = VerificationProfile(
        (VerificationCommand(("scripts/run_tests.sh",), PurePosixPath("."), 60),)
    )
    values = {
        "candidate_sha": "a" * 40,
        "contract_digest": "b" * 64,
        "generated_policy_digest": "c" * 64,
        "gate_kind": "story_integration",
        "profile_name": "story_integration",
    }

    def make_key(current_profile=profile, **updates):
        return build_verification_receipt_key(
            current_profile, repository, **{**values, **updates}
        )

    base = make_key()
    assert len(base.digest) == 64
    assert base.executor_policy == "hermes_repository_verifier:v1:story_integration"

    changed = [
        make_key(candidate_sha="d" * 40),
        make_key(
            current_profile=VerificationProfile(
                (VerificationCommand(("scripts/run_tests.sh", "--changed"), PurePosixPath("."), 60),)
            )
        ),
        make_key(
            current_profile=VerificationProfile(
                (
                    VerificationCommand(("scripts/run_tests.sh",), PurePosixPath("."), 60),
                    VerificationCommand(("scripts/run_tests.sh", "--second"), PurePosixPath("."), 60),
                )
            )
        ),
        make_key(
            current_profile=VerificationProfile(
                (
                    VerificationCommand(("scripts/run_tests.sh", "--second"), PurePosixPath("."), 60),
                    VerificationCommand(("scripts/run_tests.sh",), PurePosixPath("."), 60),
                )
            )
        ),
        make_key(
            current_profile=VerificationProfile(
                (VerificationCommand(("run_tests.sh",), PurePosixPath("scripts"), 60),)
            )
        ),
        make_key(
            current_profile=VerificationProfile(
                (VerificationCommand(("scripts/run_tests.sh",), PurePosixPath("."), 61),)
            )
        ),
        make_key(contract_digest="d" * 64),
        make_key(generated_policy_digest="d" * 64),
        make_key(gate_kind="epic_release"),
        make_key(profile_name="epic_release"),
    ]
    assert all(key.digest != base.digest for key in changed)

    runner.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    assert make_key().digest != base.digest

    monkeypatch.setattr(repository_module.platform, "system", lambda: "ChangedOS")
    assert make_key().digest != base.digest


def test_verification_receipt_key_is_stable_for_equivalent_profiles(repository: Path):
    first = VerificationProfile(
        (VerificationCommand(("scripts/run_tests.sh",), PurePosixPath("."), 60),)
    )
    second = VerificationProfile(
        (VerificationCommand(("scripts/run_tests.sh",), PurePosixPath("."), 60),)
    )
    values = {
        "candidate_sha": "a" * 40,
        "contract_digest": "b" * 64,
        "generated_policy_digest": "c" * 64,
        "gate_kind": "story_integration",
        "profile_name": "story_integration",
    }

    assert build_verification_receipt_key(first, repository, **values) == build_verification_receipt_key(
        second, repository, **values
    )


def test_build_verification_receipt_rejects_nonpassing_result(tmp_path: Path):
    result = run_verification(
        None,
        tmp_path,
        source_sha="a" * 40,
        candidate_sha="b" * 40,
        contract_digest="c" * 64,
        scope="story_integration",
        subject_id="story-1",
        generated_policy_digest="d" * 64,
    )

    with pytest.raises(ValueError, match="passed"):
        build_verification_receipt(result, subject_id="story-1", created_at=123)


def test_verification_receipt_from_payload_rejects_malformed_receipt():
    assert verification_receipt_from_payload({}) is None


def test_verification_receipt_match_requires_exact_candidate_contract_and_subject(
    repository: Path,
):
    profile = VerificationProfile(
        (VerificationCommand(("bash", "scripts/run_tests.sh"), PurePosixPath("."), 60),)
    )
    candidate_sha = "a" * 40
    contract_digest = "b" * 64
    result = run_verification(
        profile,
        repository,
        source_sha="c" * 40,
        candidate_sha=candidate_sha,
        contract_digest=contract_digest,
        scope="story_integration",
        subject_id="story-1",
        profile_name="story_integration",
        generated_policy_digest="d" * 64,
    )
    assert result.status == "passed"
    payload = verification_result_payload(
        result, scope="story_integration", subject_id="story-1", created_at=123
    )

    expected = {
        "source_sha": "c" * 40,
        "candidate_sha": candidate_sha,
        "contract_digest": contract_digest,
        "gate_kind": "story_integration",
        "subject_id": "story-1",
        "profile_name": "story_integration",
    }
    assert verification_receipt_matches(payload, **expected)
    for field, wrong in (
        ("source_sha", "e" * 40),
        ("candidate_sha", "e" * 40),
        ("contract_digest", "e" * 64),
        ("gate_kind", "epic_release"),
        ("subject_id", "story-2"),
        ("profile_name", "epic_release"),
    ):
        mismatch = {**expected, field: wrong}
        assert not verification_receipt_matches(payload, **mismatch)


def test_run_verification_missing_executable_is_configuration_error(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    profile = _verification_profile(
        VerificationCommand(
            argv=("does-not-exist-r03",),
            workdir=PurePosixPath("."),
            timeout_seconds=5,
        )
    )

    result = run_verification(
        profile,
        candidate,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "configuration_error"
    assert result.steps == ()


def test_run_verification_process_error_is_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = tmp_path / "candidate"
    script = _write_verification_script(candidate / "runner.py", "print('ok')")
    profile = _verification_profile(_verification_command(candidate, script))

    def fail_process(*args, **kwargs):
        raise OSError("process unavailable")

    monkeypatch.setattr(repository_module.subprocess, "run", fail_process)
    result = run_verification(
        profile,
        candidate,
        source_sha="source",
        candidate_sha="candidate",
        contract_digest="digest",
        scope="story_integration",
        subject_id="story-1",
    )

    assert result.status == "infrastructure_error"
    assert len(result.steps) == 1
    assert result.steps[0].status == "infrastructure_error"
    assert result.steps[0].error == "process_error"


def _git_dir(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _remote_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)],
        check=True,
        capture_output=True,
    )
    _git(seed, "config", "user.email", "boundary@example.com")
    _git(seed, "config", "user.name", "Boundary Fixture")
    (seed / "dashboard").mkdir()
    (seed / "dashboard" / "data.json").write_text("{}\n", encoding="utf-8")
    (seed / "scripts").mkdir()
    runner = seed / "scripts" / "run_tests.sh"
    runner.write_text(
        "#!/bin/sh\nset -eu\ntest -f story.txt\ntest -f epic.txt\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    (seed / "README.md").write_text("boundary fixture\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "fixture: initialize repository boundary")
    initial_sha = _git(seed, "rev-parse", "HEAD")

    remote = tmp_path / "origin.git"
    # Clone the seed into a bare fixture remote.  No push is needed for setup,
    # and the production repository service still has no remote-write path.
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(remote)],
        check=True,
        capture_output=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(remote), str(local)],
        check=True,
        capture_output=True,
    )
    _git(local, "config", "user.email", "boundary@example.com")
    _git(local, "config", "user.name", "Boundary Fixture")
    return remote, local, initial_sha


def test_repository_boundary_refreshes_verifies_and_preserves_remote_refs(
    tmp_path: Path,
):
    remote, repository, base_sha = _remote_fixture(tmp_path)
    _git(repository, "branch", "epic")
    _git(repository, "branch", "story")
    _git(repository, "checkout", "epic")
    epic_tip_sha = _commit(repository, "epic.txt", "epic\n", "fixture: advance epic")
    _git(repository, "checkout", "story")
    story_sha = _commit(repository, "story.txt", "story\n", "fixture: add story change")

    metadata = {
        "repository": {
            "base_ref": "refs/remotes/origin/main",
            "target_branch": "main",
            "verification_profiles": {
                "story_integration": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 30,
                    }
                ],
                "epic_release": [
                    {
                        "argv": ["bash", "scripts/run_tests.sh"],
                        "workdir": ".",
                        "timeout_seconds": 30,
                    }
                ],
            },
            "ci_observation": {
                "provider": "github_actions",
                "required_workflows": ["CI"],
            },
            "boundary_evidence": {
                "test_globs": ["tests/**"],
                "fixture_globs": ["tests/fixtures/**"],
                "generated_paths": ["dashboard/data.json"],
            },
        }
    }
    contract = load_repository_contract(metadata, repo_root=repository)
    assert resolve_commit(repository, contract.base_ref) == base_sha
    assert _git(repository, "branch", "--show-current") == "story"
    remote_refs_before = _git_dir(
        remote,
        "for-each-ref",
        "--format=%(refname)=%(objectname)",
        "refs/heads",
    )
    local_remote_base_before = _git(
        repository, "rev-parse", "refs/remotes/origin/main"
    )

    refreshed = refresh_story_branch(
        RefreshRequest(
            repo_root=repository,
            story_id="story-boundary",
            story_worktree=repository,
            story_branch="story",
            story_sha=story_sha,
            epic_branch="epic",
            epic_tip_sha=epic_tip_sha,
        )
    )
    assert refreshed.kind == "refreshed"
    assert refreshed.before_sha == story_sha
    assert refreshed.after_sha is not None
    candidate_sha = refreshed.after_sha
    assert candidate_sha == _git(repository, "rev-parse", "refs/heads/story")
    assert candidate_sha != story_sha
    assert _git(repository, "branch", "--show-current") == "story"

    verification = run_verification(
        contract.verification["story_integration"],
        repository,
        source_sha=candidate_sha,
        candidate_sha=candidate_sha,
        contract_digest=contract.digest,
        scope="story_integration",
        subject_id="story-boundary",
    )
    assert verification.status == "passed"
    assert verification.source_sha == candidate_sha
    assert verification.candidate_sha == candidate_sha

    generated = repository / "dashboard" / "data.json"
    generated.write_text("{\"evidence\": true}\n", encoding="utf-8")
    evidence = inspect_evidence_workspace(
        repository,
        candidate_sha,
        contract.generated_paths,
    )
    assert evidence.branch == "story"
    assert evidence.branch_head == candidate_sha
    assert evidence.undeclared_tracked == ()
    assert evidence.declared_generated == (PurePosixPath("dashboard/data.json"),)
    restore_generated_paths(repository, candidate_sha, evidence.declared_generated)
    assert generated.read_text(encoding="utf-8") == "{}\n"
    assert _git(repository, "status", "--porcelain", "--untracked-files=all") == ""

    assert _git_dir(
        remote,
        "for-each-ref",
        "--format=%(refname)=%(objectname)",
        "refs/heads",
    ) == remote_refs_before
    assert _git(repository, "rev-parse", "refs/remotes/origin/main") == local_remote_base_before


# ---------------------------------------------------------------------------
# E06 — Read-only target-head observation for the human release handoff
# ---------------------------------------------------------------------------


def test_observe_target_heads_reads_diverged_local_and_remote_heads_without_syncing(
    tmp_path: Path,
):
    remote, repository, base_sha = _remote_fixture(tmp_path)
    local_main = _commit(repository, "extra.txt", "extra\n", "local-only move")

    observation = observe_target_heads(
        repository, target_branch="main", base_ref="refs/remotes/origin/main"
    )

    assert observation.local_head == local_main
    assert observation.remote_head == base_sha
    assert observation.remote_name == "origin"
    assert observation.remote_available is True
    assert observation.local_head != observation.remote_head
    # Strictly read-only: the bare remote and the local remote-tracking ref
    # are exactly as they were — no fetch, no sync, no remote write.
    assert _git_dir(remote, "rev-parse", "refs/heads/main") == base_sha
    assert _git(repository, "rev-parse", "refs/remotes/origin/main") == base_sha


def test_observe_target_heads_fake_transport_refuses_remote_write_verbs_before_subprocess(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The observation seam refuses every write verb before any subprocess.

    A fake transport stands in for the subprocess boundary: any merge,
    push, fetch, update-ref, reset, clean, checkout, branch, tag, or
    worktree verb raises immediately, proving the production path never
    even attempts one.  The observation completes with read verbs only.
    """

    local_sha = _git(repository, "rev-parse", "HEAD")
    remote_sha = _git(repository, "rev-parse", "refs/remotes/origin/main")
    calls: list[tuple[str, ...]] = []
    forbidden = (
        "push",
        "fetch",
        "pull",
        "update-ref",
        "merge",
        "reset",
        "clean",
        "checkout",
        "switch",
        "branch",
        "tag",
        "worktree",
        "stash",
        "gc",
        "prune",
        "clone",
    )

    def fake_observe(path: Path, *args: str):
        calls.append(args)
        assert not any(verb in args for verb in forbidden), (
            f"remote-write verb attempted before subprocess: {args}"
        )
        if args[:2] == ("rev-parse", "--verify"):
            return subprocess.CompletedProcess(
                ["git", *args], 0, f"{local_sha}\n", ""
            )
        if args[:2] == ("ls-remote", "--heads"):
            return subprocess.CompletedProcess(
                ["git", *args], 0, f"{remote_sha}\trefs/heads/main\n", ""
            )
        raise AssertionError(f"unexpected Git observation: {args}")

    monkeypatch.setattr(repository_module, "_remote_observe_git", fake_observe)

    observation = observe_target_heads(
        repository, target_branch="main", base_ref="refs/remotes/origin/main"
    )

    assert observation == TargetHeadsObservation(
        local_head=local_sha,
        remote_head=remote_sha,
        remote_name="origin",
        remote_available=True,
    )
    assert calls
    assert {call[0] for call in calls} == {"rev-parse", "ls-remote"}


def test_observe_target_heads_reports_remote_unavailability_without_remote_write(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
):
    local_sha = _git(repository, "rev-parse", "HEAD")
    calls: list[tuple[str, ...]] = []

    def fake_observe(path: Path, *args: str):
        calls.append(args)
        if args[:2] == ("rev-parse", "--verify"):
            return subprocess.CompletedProcess(
                ["git", *args], 0, f"{local_sha}\n", ""
            )
        if args[:2] == ("ls-remote", "--heads"):
            return subprocess.CompletedProcess(
                ["git", *args], 128, "", "fatal: could not read from remote"
            )
        raise AssertionError(f"unexpected Git observation: {args}")

    monkeypatch.setattr(repository_module, "_remote_observe_git", fake_observe)

    observation = observe_target_heads(
        repository, target_branch="main", base_ref="refs/remotes/origin/main"
    )

    assert observation.local_head == local_sha
    assert observation.remote_head is None
    assert observation.remote_available is False
    assert {call[0] for call in calls} == {"rev-parse", "ls-remote"}


def test_observe_target_heads_transport_failure_is_reported_not_raised(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
):
    def fail_transport(path: Path, *args: str):
        return None  # _remote_observe_git returns None on OSError / SubprocessError

    monkeypatch.setattr(repository_module, "_remote_observe_git", fail_transport)

    observation = observe_target_heads(
        repository, target_branch="main", base_ref="refs/remotes/origin/main"
    )

    assert observation.local_head is None
    assert observation.remote_head is None
    assert observation.remote_available is False


def test_observe_target_heads_missing_remote_branch_is_available_but_headless(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
):
    local_sha = _git(repository, "rev-parse", "HEAD")

    def fake_observe(path: Path, *args: str):
        if args[:2] == ("rev-parse", "--verify"):
            return subprocess.CompletedProcess(
                ["git", *args], 0, f"{local_sha}\n", ""
            )
        if args[:2] == ("ls-remote", "--heads"):
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        raise AssertionError(f"unexpected Git observation: {args}")

    monkeypatch.setattr(repository_module, "_remote_observe_git", fake_observe)

    observation = observe_target_heads(
        repository, target_branch="main", base_ref="refs/remotes/origin/main"
    )

    assert observation.local_head == local_sha
    assert observation.remote_head is None
    assert observation.remote_available is True


@pytest.mark.parametrize(
    ("target_branch", "base_ref", "code"),
    [
        ("", "refs/remotes/origin/main", "malformed_target_branch"),
        (" main", "refs/remotes/origin/main", "malformed_target_branch"),
        ("main", "refs/heads/main", "malformed_base_ref"),
        ("main", "", "malformed_base_ref"),
        ("main", "refs/remotes/origin", "malformed_base_ref"),
        ("main", "refs/remotes/", "malformed_base_ref"),
        ("main", "refs/remotes//main", "malformed_base_ref"),
    ],
)
def test_observe_target_heads_rejects_malformed_target_branch_and_base_ref(
    repository: Path, target_branch: str, base_ref: str, code: str
):
    with pytest.raises(RepositoryConfigurationError) as exc_info:
        observe_target_heads(
            repository, target_branch=target_branch, base_ref=base_ref
        )

    assert exc_info.value.code == code


# ---------------------------------------------------------------------------
# E06B — Read-only exact-SHA CI observation (workflow runs)
# ---------------------------------------------------------------------------


def test_observe_ci_workflow_runs_read_only_get_maps_latest_run_conclusions(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
):
    local_sha = _git(repository, "rev-parse", "HEAD")
    git_calls: list[tuple[str, ...]] = []
    get_urls: list[str] = []

    def fake_observe(path: Path, *args: str):
        git_calls.append(args)
        assert args == ("remote", "get-url", "origin")
        return subprocess.CompletedProcess(
            ["git", *args], 0, "git@github.com:acme/widgets.git\n", ""
        )

    def fake_get(url: str, *, timeout: int = 30):
        get_urls.append(url)
        return {
            "workflow_runs": [
                # Newest-first ordering: the first name match is the latest.
                {"name": "CI", "conclusion": "success"},
                {"name": "Deploy Test", "conclusion": "failure"},
                {"name": "CI", "conclusion": "failure"},
            ]
        }

    monkeypatch.setattr(repository_module, "_remote_observe_git", fake_observe)
    monkeypatch.setattr(repository_module, "_http_observe_get", fake_get)

    conclusions = observe_ci_workflow_runs(
        repository,
        base_ref="refs/remotes/origin/main",
        workflows=("CI", "Deploy Test"),
        head_sha=local_sha,
    )

    assert conclusions == {"CI": "success", "Deploy Test": "failure"}
    assert git_calls == [("remote", "get-url", "origin")]
    assert get_urls == [
        "https://api.github.com/repos/acme/widgets/actions/runs"
        f"?head_sha={local_sha}&per_page=100"
    ]


def test_observe_ci_workflow_runs_read_only_fake_transport_refuses_every_write_primitive(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The CI observation path never issues a write verb before subprocess.

    A fake transport stands in for both boundaries (Git and HTTP): any
    push/fetch/merge/update-ref/checkout/tag/worktree Git verb or any
    rerun/cancel/dispatch URL primitive raises immediately, proving the
    production path never even attempts one.  The observation completes
    with one read-only ``remote get-url`` and one GET.
    """

    local_sha = _git(repository, "rev-parse", "HEAD")
    forbidden_git = (
        "push",
        "fetch",
        "pull",
        "update-ref",
        "merge",
        "reset",
        "clean",
        "checkout",
        "switch",
        "branch",
        "tag",
        "worktree",
        "stash",
        "gc",
        "prune",
        "clone",
    )
    forbidden_url = ("rerun", "cancel", "dispatches", "/merge", "/push")

    def fake_observe(path: Path, *args: str):
        assert not any(verb in args for verb in forbidden_git), (
            f"git write verb attempted before subprocess: {args}"
        )
        if args[:2] == ("remote", "get-url"):
            return subprocess.CompletedProcess(
                ["git", *args], 0, "https://github.com/acme/widgets.git\n", ""
            )
        raise AssertionError(f"unexpected Git observation: {args}")

    def fake_get(url: str, *, timeout: int = 30):
        assert not any(verb in url for verb in forbidden_url), (
            f"CI write primitive attempted before subprocess: {url}"
        )
        assert "actions/runs" in url
        assert f"head_sha={local_sha}" in url
        return {"workflow_runs": []}

    monkeypatch.setattr(repository_module, "_remote_observe_git", fake_observe)
    monkeypatch.setattr(repository_module, "_http_observe_get", fake_get)

    conclusions = observe_ci_workflow_runs(
        repository,
        base_ref="refs/remotes/origin/main",
        workflows=("CI",),
        head_sha=local_sha,
    )

    assert conclusions == {"CI": None}


def test_observe_ci_workflow_runs_returns_none_when_remote_unobservable(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
):
    local_sha = _git(repository, "rev-parse", "HEAD")

    def fail_transport(path: Path, *args: str):
        return None  # remote get-url fails -> unavailable

    monkeypatch.setattr(repository_module, "_remote_observe_git", fail_transport)

    conclusions = observe_ci_workflow_runs(
        repository,
        base_ref="refs/remotes/origin/main",
        workflows=("CI",),
        head_sha=local_sha,
    )

    assert conclusions is None


def test_observe_ci_workflow_runs_returns_none_when_provider_unreachable(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
):
    local_sha = _git(repository, "rev-parse", "HEAD")

    def fake_observe(path: Path, *args: str):
        if args[:2] == ("remote", "get-url"):
            return subprocess.CompletedProcess(
                ["git", *args], 0, "git@github.com:acme/widgets.git\n", ""
            )
        raise AssertionError(f"unexpected Git observation: {args}")

    def fail_get(url: str, *, timeout: int = 30):
        return None  # HTTP GET fails -> unavailable

    monkeypatch.setattr(repository_module, "_remote_observe_git", fake_observe)
    monkeypatch.setattr(repository_module, "_http_observe_get", fail_get)

    conclusions = observe_ci_workflow_runs(
        repository,
        base_ref="refs/remotes/origin/main",
        workflows=("CI",),
        head_sha=local_sha,
    )

    assert conclusions is None


def test_observe_ci_workflow_runs_rejects_malformed_head_sha(
    repository: Path,
):
    with pytest.raises(RepositoryConfigurationError) as exc_info:
        observe_ci_workflow_runs(
            repository,
            base_ref="refs/remotes/origin/main",
            workflows=("CI",),
            head_sha="not-a-full-sha",
        )

    assert exc_info.value.code == "malformed_head_sha"


def test_http_observe_get_seam_is_strictly_get_only(
    monkeypatch: pytest.MonkeyPatch,
):
    """The real HTTP seam builds a GET request — never any write method."""

    requests: list = []

    class FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"workflow_runs": []}'

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

    def fake_urlopen(req, timeout=None):
        requests.append(req)
        assert req.method == "GET"
        assert req.full_url.startswith("https://api.github.com/")
        return FakeResponse()

    monkeypatch.setattr(repository_module.urllib.request, "urlopen", fake_urlopen)

    data = repository_module._http_observe_get(
        "https://api.github.com/repos/acme/widgets/actions/runs?head_sha=x"
    )

    assert data == {"workflow_runs": []}
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].headers["Accept"] == "application/vnd.github+json"
