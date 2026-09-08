"""Kanban DB verification tests, isolated to keep per-file CI work bounded."""

from __future__ import annotations
import json
import subprocess
import sys
import threading
import time
import types
import unittest.mock
from pathlib import Path, PurePosixPath
import pytest
from hermes_cli import kanban_db as kb
import hermes_cli.kanban_db_connect as kanban_db_connect
import shutil as shutil
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_repository import (
    VerificationCommand,
)

from tests.hermes_cli.test_kanban_db import kanban_home as kanban_home
from tests.hermes_cli.test_kanban_db import (
    _RecordingOpsClient,
    _assert_no_push,
    _commit_file,
    _configure_candidate_verification,
    _configure_task_expected,
    _configure_task_row_and_events,
    _d4_escalated,
    _d4_expected_snapshot,
    _enable_merge_after_green,
    _full_tables_state,
    _git_output,
    _init_git_repo,
    _make_deploy_epic,
    _make_done_standalone_story,
    _make_epic_branch,
    _make_epic_with_children,
    _make_fact_ready_epic,
    _record_git_calls,
    _resolve_preflight,
    _run_reusable_verification,
    _set_human_escalation_profile,
    _set_task_status,
    _v2_product_board,
    _v2_product_board_with_repo,
    _verification_run_fixture,
    _write_product_board_enf,
)


def test_build_merge_candidate_keeps_checked_out_target_unchanged(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    source_sha = _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")

    candidate = kb._build_verified_merge_candidate(
        repo,
        "main",
        "wt/source",
        "test candidate",
        lambda path: (path / "epic_work.txt").read_text() == "epic work\n",
    )

    assert candidate.pre_sha == pre_sha
    assert candidate.target_worktree == repo.resolve()
    assert candidate.candidate_sha != pre_sha
    assert subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", source_sha, "main"]
    ).returncode == 1
    assert kb._fast_forward_target(candidate) is True
    assert subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", source_sha, "main"]
    ).returncode == 0


def test_build_merge_candidate_rejects_dirty_checked_out_target(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")
    (repo / "tracked.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(kb.IntegrationCandidateError, match="target worktree is dirty"):
        kb._build_verified_merge_candidate(
            repo, "main", "wt/source", "test candidate", lambda _path: True
        )

    assert _git_output(repo, "rev-parse", "main") == pre_sha
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "dirty"


def test_build_merge_candidate_updates_unchecked_target_ref(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    source_sha = _make_epic_branch(repo, "wt/source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "operator"],
        check=True,
        capture_output=True,
        text=True,
    )
    candidate = kb._build_verified_merge_candidate(
        repo, "main", "wt/source", "test candidate", lambda _path: True
    )
    assert candidate.target_worktree is None
    assert kb._fast_forward_target(candidate) is True
    assert _git_output(repo, "branch", "--show-current") == "operator"
    assert subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", source_sha, "main"]
    ).returncode == 0


def test_build_merge_candidate_conflict_preserves_target(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "wt/source"],
        check=True,
        capture_output=True,
        text=True,
    )
    _commit_file(repo, "shared.txt", "source\n", "source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    _commit_file(repo, "shared.txt", "main\n", "main")
    pre_sha = _git_output(repo, "rev-parse", "main")
    with pytest.raises(kb.IntegrationCandidateError, match="merge conflict"):
        kb._build_verified_merge_candidate(
            repo, "main", "wt/source", "test candidate", lambda _path: True
        )
    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_build_merge_candidate_verification_failure_preserves_target(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")
    with pytest.raises(kb.IntegrationCandidateError, match="verification failed"):
        kb._build_verified_merge_candidate(
            repo, "main", "wt/source", "test candidate", lambda _path: False
        )
    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_build_merge_candidate_uses_configured_verification_profile(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "wt/source"],
        check=True,
        capture_output=True,
        text=True,
    )
    verifier = repo / "verify.py"
    verifier.write_text(
        "#!/usr/bin/env python3\nprint('configured-verifier')\n",
        encoding="utf-8",
    )
    verifier.chmod(0o755)
    source_sha = _commit_file(repo, "verify.py", verifier.read_text(), "verifier")
    profile = kb.VerificationProfile(
        (
            VerificationCommand(
                argv=("verify.py",),
                workdir=PurePosixPath("."),
                timeout_seconds=5,
            ),
        )
    )
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )

    candidate = kb._build_verified_merge_candidate(
        repo,
        "main",
        "wt/source",
        "configured candidate",
        verification_profile=profile,
        verification_contract_digest="contract-digest",
        verification_scope="story_integration",
        verification_subject_id="story-1",
        expected_source_sha=source_sha,
    )

    assert candidate.verification_result is not None
    assert candidate.verification_result.status == "passed"
    assert candidate.verification_result.profile == "story_integration"
    assert candidate.verification_result.contract_digest == "contract-digest"


def test_build_merge_candidate_missing_configured_profile_is_not_legacy_fallback(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    source_sha = _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")

    with pytest.raises(kb.IntegrationCandidateError) as exc_info:
        kb._build_verified_merge_candidate(
            repo,
            "main",
            "wt/source",
            "configured candidate",
            verification_profile=None,
            verification_contract_digest="contract-digest",
            verification_scope="epic_release",
            verification_subject_id="epic-1",
            expected_source_sha=source_sha,
        )

    assert exc_info.value.verification_result is not None
    assert exc_info.value.verification_result.status == "configuration_error"
    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_merge_epic_preserves_explicit_injected_candidate_verification(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-injected-verification"
    _v2_product_board_with_repo(board, repo)
    _configure_candidate_verification(
        board, repo, command=("python", "-c", "raise SystemExit(9)")
    )
    epic, children = _make_fact_ready_epic(board, repo)
    injected = unittest.mock.Mock(return_value=True)

    with kanban_db_connect.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, candidate_verify_fn=injected
        )

    assert result == "merged"
    injected.assert_called_once()


def test_parser_addition_preserves_exact_repository_verification_receipt_reuse(
    kanban_home, tmp_path, monkeypatch
):
    fixture = _verification_run_fixture(kanban_home, tmp_path, monkeypatch)
    _repo, board, task_id, _contract, _candidate_sha, count_file = fixture

    with kanban_db_connect.connect(board=board) as conn:
        first = _run_reusable_verification(conn, fixture)
        second = _run_reusable_verification(conn, fixture)
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='repository_verification'",
            (task_id,),
        ).fetchone()[0]

    assert first.status == second.status == "passed"
    assert first.reused is False
    assert second.reused is True
    assert second.steps == first.steps
    assert count_file.read_text(encoding="utf-8") == "x"
    assert event_count == 1


def test_configured_verification_reuses_receipt_after_connection_crash_boundary(
    kanban_home, tmp_path, monkeypatch
):
    fixture = _verification_run_fixture(kanban_home, tmp_path, monkeypatch)
    _repo, board, _task_id, _contract, _candidate_sha, count_file = fixture

    with kanban_db_connect.connect(board=board) as conn:
        first = _run_reusable_verification(conn, fixture)
    with kanban_db_connect.connect(board=board) as conn:
        recovered = _run_reusable_verification(conn, fixture)

    assert first.reused is False
    assert recovered.reused is True
    assert count_file.read_text(encoding="utf-8") == "x"


def test_verified_candidate_crash_reuses_persisted_receipt(
    kanban_home, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-verification-crash-reuse"
    _v2_product_board_with_repo(board, repo)
    count_file = tmp_path / "verification-count.txt"
    _configure_candidate_verification(
        board,
        repo,
        command=(
            sys.executable,
            "-c",
            f"from pathlib import Path; p=Path({str(count_file)!r}); "
            "p.write_text(p.read_text() + 'x' if p.exists() else 'x')",
        ),
    )
    epic, children = _make_fact_ready_epic(board, repo)
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+00:00")
    apply = unittest.mock.Mock(side_effect=[False, True])
    monkeypatch.setattr(kb, "_fast_forward_target", apply)

    with kanban_db_connect.connect(board=board) as conn:
        first = kb.merge_epic_to_main(conn, epic, board=board)
        second = kb.merge_epic_to_main(conn, epic, board=board)
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='repository_verification'",
            (epic,),
        ).fetchone()[0]

    assert first == "verify_failed"
    assert second == "merged"
    assert apply.call_count == 2
    assert count_file.read_text(encoding="utf-8") == "x"
    assert event_count == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "candidate_sha",
        "contract_digest",
        "command_set_digest",
        "runtime_toolchain_digest",
        "generated_policy_digest",
        "gate_kind",
        "executor_policy",
        "key_digest",
        "result_digest",
        "foreign_scope",
        "foreign_subject",
        "foreign_task",
        "malformed_json",
        "failed_result",
        "missing_receipt",
    ],
)
def test_configured_verification_rejects_key_result_and_foreign_receipts(
    kanban_home, tmp_path, monkeypatch, tamper
):
    fixture = _verification_run_fixture(kanban_home, tmp_path, monkeypatch)
    _repo, board, task_id, _contract, _candidate_sha, count_file = fixture

    with kanban_db_connect.connect(board=board) as conn:
        first = _run_reusable_verification(conn, fixture)
        row = conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id=? AND kind='repository_verification'",
            (task_id,),
        ).fetchone()
        payload = json.loads(row["payload"])
        if tamper == "foreign_task":
            foreign = kb.create_task(conn, title="Foreign", board=board)
            conn.execute("UPDATE task_events SET task_id=? WHERE id=?", (foreign, row["id"]))
        elif tamper == "malformed_json":
            conn.execute("UPDATE task_events SET payload='{' WHERE id=?", (row["id"],))
        elif tamper == "failed_result":
            payload["status"] = "failed"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        elif tamper == "missing_receipt":
            payload.pop("receipt")
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        elif tamper == "foreign_scope":
            payload["scope"] = "epic_release"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        elif tamper == "foreign_subject":
            payload["subject_id"] = "foreign-story"
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        elif tamper == "result_digest":
            payload["receipt"]["result_digest"] = "0" * 64
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        else:
            key_name = "digest" if tamper == "key_digest" else tamper
            payload["receipt"]["key"][key_name] = "0" * 64
            conn.execute(
                "UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"])
            )
        conn.commit()

        rerun = _run_reusable_verification(conn, fixture)

    assert first.reused is False
    assert rerun.reused is False
    assert count_file.read_text(encoding="utf-8") == "xx"


def test_merge_epic_records_configured_profile_failure_as_attention_required(
    kanban_home, tmp_path
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-config-attention"
    _v2_product_board_with_repo(board, repo)
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["repository"] = {
        "base_ref": "refs/heads/main",
        "target_branch": "main",
        "verification_profiles": {
            "story_integration": {
                "commands": [
                    {
                        "argv": ["python", "-m", "unittest"],
                        "workdir": ".",
                        "timeout_seconds": 5,
                    }
                ]
            }
        },
        "ci_observation": {
            "provider": "test",
            "required_workflows": ["CI"],
        },
        "boundary_evidence": {
            "test_globs": [],
            "fixture_globs": [],
            "generated_paths": ["README.md"],
        },
    }
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    epic, children = _make_fact_ready_epic(board, repo)

    with kanban_db_connect.connect(board=board) as conn:
        result = kb.merge_epic_to_main(conn, epic, board=board)
        task = kb.get_task(conn, epic)
        verification_events = [
            event
            for event in kb.list_events(conn, epic)
            if event.kind == "repository_verification"
        ]

    assert result == "attention_required"
    assert task is not None and task.rework_count == 0
    assert verification_events
    payload = verification_events[-1].payload
    assert isinstance(payload, dict)
    assert payload["status"] == "configuration_error"
    assert payload["rework_eligible"] is False


def test_fast_forward_rejects_target_that_moved_after_candidate(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    candidate = kb._build_verified_merge_candidate(
        repo, "main", "wt/source", "test candidate", lambda _path: True
    )
    _commit_file(repo, "operator.txt", "new\n", "operator moved main")
    moved_sha = _git_output(repo, "rev-parse", "main")
    assert kb._fast_forward_target(candidate) is False
    assert _git_output(repo, "rev-parse", "main") == moved_sha


def test_fast_forward_rejects_checked_out_target_race_to_candidate_descendant(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    candidate = kb._build_verified_merge_candidate(
        repo, "main", "wt/source", "test candidate", lambda _path: True
    )
    integration_git = kb._integration_git
    raced: dict[str, str] = {}

    def advance_target_before_merge(cwd, args, *, timeout=120):
        if args == ["merge", "--ff-only", candidate.candidate_sha] and not raced:
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )
            raced["sha"] = _commit_file(
                repo,
                "operator-after-candidate.txt",
                "unverified\n",
                "operator advanced past candidate",
            )
        return integration_git(cwd, args, timeout=timeout)

    monkeypatch.setattr(kb, "_integration_git", advance_target_before_merge)

    assert kb._fast_forward_target(candidate) is False
    assert _git_output(repo, "rev-parse", "main") == raced["sha"]
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", candidate.candidate_ref],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def test_fast_forward_rejects_target_checked_out_after_candidate(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "operator"],
        check=True,
        capture_output=True,
        text=True,
    )
    candidate = kb._build_verified_merge_candidate(
        repo, "main", "wt/source", "test candidate", lambda _path: True
    )
    assert candidate.target_worktree is None

    late_checkout = tmp_path / "late-main"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(late_checkout), "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    pre_sha = _git_output(repo, "rev-parse", "main")

    assert kb._fast_forward_target(candidate) is False
    assert _git_output(repo, "rev-parse", "main") == pre_sha
    assert _git_output(late_checkout, "status", "--porcelain") == ""


def test_build_merge_candidate_rejects_reviewed_source_ref_drift(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    approved_sha = _make_epic_branch(repo, "wt/source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "wt/source"],
        check=True,
        capture_output=True,
        text=True,
    )
    _commit_file(repo, "after-review.txt", "drift\n", "post-review drift")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    pre_sha = _git_output(repo, "rev-parse", "main")

    with pytest.raises(kb.IntegrationCandidateError, match="source branch moved"):
        kb._build_verified_merge_candidate(
            repo,
            "main",
            "wt/source",
            "test candidate",
            lambda _path: True,
            expected_source_sha=approved_sha,
        )

    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_build_merge_candidate_rejects_source_drift_during_verification(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    approved_sha = _make_epic_branch(repo, "wt/source")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "post-review", "wt/source"],
        check=True,
        capture_output=True,
        text=True,
    )
    drift_sha = _commit_file(repo, "after-review.txt", "drift\n", "post-review drift")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    pre_sha = _git_output(repo, "rev-parse", "main")

    def move_source_during_verify(_candidate: Path) -> bool:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "update-ref",
                "refs/heads/wt/source",
                drift_sha,
                approved_sha,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return True

    with pytest.raises(kb.IntegrationCandidateError, match="source branch moved"):
        kb._build_verified_merge_candidate(
            repo,
            "main",
            "wt/source",
            "test candidate",
            move_source_during_verify,
            expected_source_sha=approved_sha,
        )

    assert _git_output(repo, "rev-parse", "main") == pre_sha


def test_build_merge_candidate_preserves_dirty_scratch_on_cleanup_failure(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_epic_branch(repo, "wt/source")
    pre_sha = _git_output(repo, "rev-parse", "main")
    scratch: Path | None = None

    def dirty_verify(path: Path) -> bool:
        nonlocal scratch
        scratch = path
        (path / "verification-output.txt").write_text("keep", encoding="utf-8")
        return True

    with pytest.raises(kb.IntegrationCandidateError, match="scratch worktree is dirty"):
        kb._build_verified_merge_candidate(
            repo, "main", "wt/source", "test candidate", dirty_verify
        )
    assert _git_output(repo, "rev-parse", "main") == pre_sha
    assert scratch is not None and (scratch / "verification-output.txt").exists()


def test_merge_epic_to_main_happy_path_merges_and_never_pushes(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-happy"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kanban_db_connect.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")

    epic_branch = kb.epic_branch_for(epic)
    epic_sha = _make_epic_branch(repo, epic_branch)

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "merged"
    notify.assert_not_called()
    _assert_no_push(calls)

    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", epic_sha, "main"],
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 0, "main must contain the epic's commit"

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
    )
    assert status.stdout.strip() == "", "working tree must be clean after merge"


def test_merge_epic_to_main_refuses_unignored_sibling_worktree(
    kanban_home, tmp_path, monkeypatch
):
    """All target dirt, including an unignored worktree, fails closed."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-untracked-worktree"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kanban_db_connect.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")

    epic_branch = kb.epic_branch_for(epic)
    epic_sha = _make_epic_branch(repo, epic_branch)

    # Real linked worktree at <repo>/.worktrees/story, exactly as v2 story
    # dispatch creates it -- untracked from main's point of view.
    worktree_branch = "wt/story-1"
    worktree_path = repo / ".worktrees" / "story-1"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", worktree_branch, str(worktree_path), "main"],
        check=True, capture_output=True, text=True,
    )

    # Sanity-check the repro premise before exercising the fix.
    dirty_status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
    )
    assert "?? .worktrees/" in dirty_status.stdout, "expected the sibling worktree to be untracked on main"
    clean_status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True,
    )
    assert clean_status.stdout.strip() == "", "tracked-only status must be clean"

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "verify_failed"
    notify.assert_called_once()
    _assert_no_push(calls)
    assert not any("reset" in cmd for cmd in calls)

    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", epic_sha, "main"],
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 1, "dirty main must not contain the epic's commit"


def test_merge_epic_to_main_conflict_aborts_blocks_and_never_pushes(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-conflict"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kanban_db_connect.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")

    epic_branch = kb.epic_branch_for(epic)
    # Branch off main, then have BOTH main and the epic branch modify the
    # same line differently so the merge conflicts.
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", epic_branch], check=True,
        capture_output=True, text=True,
    )
    _commit_file(repo, "shared.txt", "epic version\n", "epic edits shared")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "main"], check=True, capture_output=True, text=True,
    )
    _commit_file(repo, "shared.txt", "main version\n", "main edits shared")
    pre_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "conflict"
    _assert_no_push(calls)
    notify.assert_called_once()

    post_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert post_sha == pre_sha, "a failed merge must never leave main mutated"

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
    )
    assert status.stdout.strip() == "", "merge --abort must leave a clean tree"

    with kanban_db_connect.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 1


def test_merge_epic_to_main_post_merge_verify_fails_resets_and_blocks(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-verify-fail"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kanban_db_connect.connect(board=board) as conn:
        for child in children:
            _set_task_status(conn, child, "done")

    epic_branch = kb.epic_branch_for(epic)
    _make_epic_branch(repo, epic_branch)
    pre_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()

    # Branch-aware: True for the epic branch (so epic_ready's own verify
    # passes) but False for main (so the post-merge check fails).
    def verify(branch: str) -> bool:
        return branch != "main"

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=verify, notify_fn=notify,
        )

    assert result == "verify_failed"
    _assert_no_push(calls)
    notify.assert_called_once()

    post_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert post_sha == pre_sha, "reset --hard must undo the merge"

    with kanban_db_connect.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 1


def test_merge_epic_to_main_not_ready_does_not_touch_git(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-merge-not-ready"
    _v2_product_board_with_repo(board, repo)
    epic, children = _make_epic_with_children(board)
    with kanban_db_connect.connect(board=board) as conn:
        _set_task_status(conn, children[0], "done")
        # children[1] stays not-done.

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "not_ready"
    assert calls == []
    notify.assert_not_called()


def test_merge_epic_to_main_non_v2_board_returns_none(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "legacy-merge-board"
    kb.create_board(board, name="Legacy Board", default_workdir=str(repo))
    with kanban_db_connect.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.merge_epic_to_main(
            conn, epic, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result is None
    assert calls == []
    notify.assert_not_called()


def test_deploy_epic_happy_path_deploys_test_then_preprod_and_notifies(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-happy"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    calls = _record_git_calls(monkeypatch)
    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert ops.calls == [
        ("build_roll", "test"), ("smoke", "test"),
        ("build_roll", "preprod"), ("smoke", "preprod"),
    ]
    _assert_no_push(calls)
    assert not any(("remote" in cmd or "origin" in cmd) for cmd in calls)

    with kanban_db_connect.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 0

    notify.assert_called_once()
    message = notify.call_args[0][0]
    assert message["failure"] is False
    assert message["epic_title"] == "Epic"
    assert {s["id"] for s in message["stories"]} == set(children)
    assert message["commit_range"] and ".." in message["commit_range"]
    assert [e["env"] for e in message["envs_status"]] == ["test", "preprod"]
    assert all(e["smoke_ok"] for e in message["envs_status"])

    assert result == message


def test_deploy_epic_test_smoke_fails_stops_blocks_and_pages(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-test-smoke-fail"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    ops = _RecordingOpsClient(smoke_fail={"test"})
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert ("build_roll", "preprod") not in ops.calls
    assert ("smoke", "preprod") not in ops.calls
    assert ops.calls == [("build_roll", "test"), ("smoke", "test")]

    with kanban_db_connect.connect(board=board) as conn:
        row = conn.execute(
            "SELECT blocked, running FROM tasks WHERE id = ?", (epic,)
        ).fetchone()
    assert row["blocked"] == 1
    assert row["running"] == 0

    notify.assert_called_once()
    message = notify.call_args[0][0]
    assert message["failure"] is True
    assert message["reason"]
    assert result == message


def test_deploy_epic_preprod_build_fails_blocks_and_pages(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-preprod-build-fail"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    ops = _RecordingOpsClient(build_fail={"preprod"})
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert ops.calls == [
        ("build_roll", "test"), ("smoke", "test"), ("build_roll", "preprod"),
    ]
    assert ("smoke", "preprod") not in ops.calls

    with kanban_db_connect.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 1

    notify.assert_called_once()
    message = notify.call_args[0][0]
    assert message["failure"] is True
    envs_status = {e["env"]: e for e in message["envs_status"]}
    assert envs_status["test"]["smoke_ok"] is True
    assert envs_status["preprod"]["built"] is False
    assert result == message


def test_deploy_epic_message_shape_contains_epic_stories_range_and_status(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-message-shape"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    message = notify.call_args[0][0]
    assert message["epic_id"] == epic
    assert message["epic_title"] == "Epic"
    story_ids = {s["id"] for s in message["stories"]}
    story_titles = {s["title"] for s in message["stories"]}
    assert story_ids == set(children)
    assert story_titles == {"Story 0", "Story 1"}
    assert message["commit_range"]
    assert len(message["envs_status"]) == 2
    assert message["reason"] is None


def test_notify_operations_failure_message_includes_reason(kanban_home, tmp_path, monkeypatch):
    board = "v2-notify-failure-reason"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    notify = unittest.mock.Mock()
    envs_status = [{"env": "test", "built": True, "smoke_ok": False, "detail": "smoke check failed"}]
    with kanban_db_connect.connect(board=board) as conn:
        message = kb.notify_operations(
            conn, epic, board=board, envs_status=envs_status,
            failure=True, reason="deploy: test smoke failed", notify_fn=notify,
        )

    assert message["failure"] is True
    assert message["reason"] == "deploy: test smoke failed"
    notify.assert_called_once_with(message)


def test_deploy_epic_rejects_production_env_deploys_nothing(kanban_home, tmp_path, monkeypatch):
    board = "v2-deploy-boundary-prod"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        with pytest.raises(ValueError):
            kb.deploy_epic(
                conn, epic, board=board, envs=("test", "prod"),
                ops_client=ops, notify_fn=notify,
            )

    assert ops.calls == []
    notify.assert_not_called()
    with kanban_db_connect.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 0


def test_deploy_epic_never_touches_git_push_or_remote(kanban_home, tmp_path, monkeypatch):
    """Boundary proof (T5.3): record every subprocess call across a full
    happy-path deploy and assert the deploy path never runs `git push` and
    never invokes a remote/origin verb -- only local `rev-parse` for the
    commit range."""
    board = "v2-deploy-boundary-no-push"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)

    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert calls, "expected deploy_epic to run at least one local git subcommand"
    for cmd in calls:
        assert "push" not in cmd, f"git push invoked during deploy: {cmd}"
        assert "remote" not in cmd, f"git remote invoked during deploy: {cmd}"
        assert "origin" not in cmd, f"origin referenced during deploy: {cmd}"


def test_deploy_epic_non_v2_board_returns_none(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "legacy-deploy-board"
    kb.create_board(board, name="Legacy Board", default_workdir=str(repo))
    with kanban_db_connect.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)

    ops = _RecordingOpsClient()
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, ops_client=ops, notify_fn=notify)

    assert result is None
    assert ops.calls == []
    notify.assert_not_called()


def test_deploy_epic_default_ops_client_raises_not_implemented(kanban_home, tmp_path, monkeypatch):
    """No ``ops_client`` injected -> the module stub is used, and it raises
    rather than silently deploying anything (real adapter deferred to
    feat/container-ops-api / PR #3)."""
    board = "v2-deploy-default-client"
    epic, repo, children = _make_deploy_epic(tmp_path, board)

    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.deploy_epic(conn, epic, board=board, notify_fn=notify)

    # build_roll("test") raises NotImplementedError inside the loop, which
    # deploy_epic treats like any other build failure: block + page.
    assert result is not None
    assert result["failure"] is True
    with kanban_db_connect.connect(board=board) as conn:
        row = conn.execute("SELECT blocked FROM tasks WHERE id = ?", (epic,)).fetchone()
    assert row["blocked"] == 1
    notify.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 6 T6.1: migrate_cards_to_v2_flags -- reconcile existing cards' flags
# to their legacy status when a board flips to handoff_v2.
#
# A board's existing cards have real statuses (running/blocked/ready/...) but
# the running/blocked flag columns default to 0, so an already-running card
# would read status='running', running=0 -- a direct disagreement with
# _legacy_status. migrate_cards_to_v2_flags is the inverse of _legacy_status:
# it sets every card's flags to MATCH its status, using the same mapping as
# CR2's _apply_v2_flags_for_status (now shared via _v2_flags_for_status).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected_running,expected_blocked",
    [
        ("running", 1, 0),
        ("blocked", 0, 1),
        ("ready", 0, 0),
        ("todo", 0, 0),
        ("review", 0, 0),
        ("scheduled", 0, 0),
        ("archived", 0, 0),
        ("done", 0, 0),
        ("triage", 0, 0),
    ],
)
def test_v2_flags_for_status_mapping(status, expected_running, expected_blocked):
    assert kb._v2_flags_for_status(status) == (expected_running, expected_blocked)


def test_migrate_cards_to_v2_flags_reconciles_mixed_board(kanban_home, monkeypatch):
    """Seed a mixed-status board with flags left at their 0 default (as if
    handoff_v2 had just been flipped on), migrate, and assert every card's
    flags now agree with its status via _legacy_status.

    For the non-flag-derivable statuses (ready/todo/review/done/scheduled/
    archived), ``_legacy_status`` falls through to the column status for the
    card's ``current_step_key`` -- so each such card is seeded with a custom
    column whose status matches, letting ``_legacy_status`` round-trip back
    to the original status once flags are reconciled. running/blocked cards
    round-trip via flag precedence regardless of column.
    """
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-mixed"
    kb.create_board(board, name="Migrate Mixed", preset="product")
    meta_path = kb.board_metadata_path(board)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["columns"] = [
        {"name": "development", "status": "ready"},
        {"name": "ready", "status": "ready"},
        {"name": "todo", "status": "todo"},
        {"name": "review", "status": "review"},
        {"name": "done", "status": "done"},
        {"name": "scheduled", "status": "scheduled"},
        {"name": "archived", "status": "archived"},
    ]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    meta = kb.read_board_metadata(board)

    statuses = ["running", "blocked", "ready", "todo", "review", "done", "scheduled", "archived"]
    with kanban_db_connect.connect(board=board) as conn:
        ids = []
        for status in statuses:
            step_key = "development" if status in ("running", "blocked") else status
            tid = kb.create_task(
                conn, title=f"card-{status}", workflow_template_id="custom",
                current_step_key=step_key,
            )
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            ids.append(tid)

        count = kb.migrate_cards_to_v2_flags(conn, board=board)

        rows = conn.execute(
            "SELECT id, status, running, blocked, current_step_key FROM tasks "
            "WHERE id IN ({})".format(",".join("?" * len(ids))),
            ids,
        ).fetchall()

    assert count == len(statuses)
    assert len(rows) == len(statuses)
    for row in rows:
        expected_running, expected_blocked = kb._v2_flags_for_status(row["status"])
        assert row["running"] == expected_running
        assert row["blocked"] == expected_blocked
        assert kb._legacy_status(row, meta) == row["status"]


def test_migrate_cards_to_v2_flags_idempotent(kanban_home, monkeypatch):
    """Running the migration a second time leaves flags unchanged and the
    consistency invariant intact."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-idempotent"
    kb.create_board(board, name="Migrate Idempotent", preset="product")
    meta = kb.read_board_metadata(board)

    with kanban_db_connect.connect(board=board) as conn:
        tid_running = kb.create_task(
            conn, title="running-card", workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid_running,))
        tid_blocked = kb.create_task(
            conn, title="blocked-card", workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid_blocked,))

        kb.migrate_cards_to_v2_flags(conn, board=board)
        first = {
            tid: dict(conn.execute(
                "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
            ).fetchone())
            for tid in (tid_running, tid_blocked)
        }

        kb.migrate_cards_to_v2_flags(conn, board=board)
        second = {
            tid: dict(conn.execute(
                "SELECT status, running, blocked FROM tasks WHERE id = ?", (tid,),
            ).fetchone())
            for tid in (tid_running, tid_blocked)
        }

    assert second == first
    for tid in (tid_running, tid_blocked):
        row = second[tid]
        assert kb._legacy_status(row, meta) == row["status"]


def test_migrate_cards_to_v2_flags_does_not_touch_status_or_phase(kanban_home, monkeypatch):
    """The migration only ever writes running/blocked -- status and
    current_step_key must be byte-for-byte unchanged."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-preserves-status"
    kb.create_board(board, name="Migrate Preserves Status", preset="product")

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="card", workflow_template_id="product",
            current_step_key="development",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        before = dict(conn.execute(
            "SELECT status, current_step_key FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

        kb.migrate_cards_to_v2_flags(conn, board=board)

        after = dict(conn.execute(
            "SELECT status, current_step_key FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

    assert after == before


# ---------------------------------------------------------------------------
# Phase 6 T6.1 (extended): migrate_cards_to_v2_flags also reconciles PHASE for
# terminal 'done' cards -- a dry run on a copy of a production board found
# real cards with status='done' still parked at a non-done phase (legacy
# completions predating the "done ⟹ phase=done" rule), whose _legacy_status
# read something other than 'done'.
# ---------------------------------------------------------------------------

def test_migrate_cards_to_v2_flags_reconciles_phase_for_done(kanban_home, monkeypatch):
    """A legacy 'done' card stuck at a non-done phase (the real dry-run
    finding) gets its current_step_key advanced to 'done' too, not just its
    flags -- so _legacy_status agrees with the stored status again."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-done-phase"
    kb.create_board(board, name="Migrate Done Phase", preset="product")
    meta = kb.read_board_metadata(board)

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title="legacy-done-card", workflow_template_id="product",
            current_step_key="release_measure",
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))

        kb.migrate_cards_to_v2_flags(conn, board=board)

        row = dict(conn.execute(
            "SELECT status, current_step_key, running, blocked FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone())

    assert row["status"] == "done"
    assert row["current_step_key"] == "done"
    assert (row["running"], row["blocked"]) == (0, 0)
    assert kb._legacy_status(row, meta) == "done"


@pytest.mark.parametrize(
    "status,step_key",
    [
        ("ready", "development"),
        ("running", "development"),
        ("blocked", "development"),
        ("review", "review"),
        ("todo", "development"),
        ("archived", "release_measure"),
    ],
)
def test_migrate_cards_to_v2_flags_leaves_non_done_phase_untouched(
    kanban_home, monkeypatch, status, step_key
):
    """Only status='done' cards get their phase moved -- every other status
    (including archived, which is out-of-band, not a workflow phase) keeps
    its current_step_key exactly as-is."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = f"migrate-cards-phase-untouched-{status}"
    kb.create_board(board, name="Migrate Phase Untouched", preset="product")

    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn, title=f"card-{status}", workflow_template_id="product",
            current_step_key=step_key,
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))

        kb.migrate_cards_to_v2_flags(conn, board=board)

        row = dict(conn.execute(
            "SELECT status, current_step_key FROM tasks WHERE id = ?", (tid,),
        ).fetchone())

    assert row["status"] == status
    assert row["current_step_key"] == step_key


def test_migrate_cards_to_v2_flags_phase_for_done_idempotent(kanban_home, monkeypatch):
    """A done card already at phase 'done' is unchanged, and running the
    migration a second time is a no-op for the phase fixup too."""
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    board = "migrate-cards-done-phase-idempotent"
    kb.create_board(board, name="Migrate Done Phase Idempotent", preset="product")

    with kanban_db_connect.connect(board=board) as conn:
        tid_already_done = kb.create_task(
            conn, title="already-done-card", workflow_template_id="product",
            current_step_key="done",
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid_already_done,))
        tid_legacy_done = kb.create_task(
            conn, title="legacy-done-card", workflow_template_id="product",
            current_step_key="release_measure",
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid_legacy_done,))

        kb.migrate_cards_to_v2_flags(conn, board=board)
        first = {
            tid: dict(conn.execute(
                "SELECT status, current_step_key, running, blocked FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone())
            for tid in (tid_already_done, tid_legacy_done)
        }

        kb.migrate_cards_to_v2_flags(conn, board=board)
        second = {
            tid: dict(conn.execute(
                "SELECT status, current_step_key, running, blocked FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone())
            for tid in (tid_already_done, tid_legacy_done)
        }

    assert second == first
    assert second[tid_already_done]["current_step_key"] == "done"
    assert second[tid_legacy_done]["current_step_key"] == "done"

def test_product_board_defaults_helper_enables_handoff_v2_and_gitignore(kanban_home, tmp_path):
    repo = tmp_path / "product-repo"
    _init_git_repo(repo)

    meta = kb.ensure_product_board_defaults(
        "product-defaults",
        name="Product Defaults",
        default_workdir=str(repo),
    )

    assert meta["preset"] == "product"
    assert meta["columns"] == kb.PRODUCT_BOARD_COLUMNS
    assert meta["product_workflow"]["handoff_v2"] is True
    assert meta["product_workflow"]["assignees"] == kb.PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES
    assert ".worktrees/" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_project_bound_product_task_defaults_to_product_backlog_and_worktree(kanban_home, tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    repo = tmp_path / "product-repo"
    _init_git_repo(repo)
    kb.ensure_product_board_defaults("prod", name="Product", default_workdir=str(repo))

    home = kanban_home
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(
            pconn,
            name="Product Repo",
            primary_path=str(repo),
            board_slug="prod",
        )

    with kanban_db_connect.connect(board="prod") as conn:
        tid = kb.create_task(conn, title="User story: isolated work", project_id=project_id, board="prod")
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.workflow_template_id == "product"
    assert task.current_step_key == "backlog"
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == str(repo / ".worktrees" / tid)
    assert task.branch_name.startswith("product-repo/")
    assert any(event.kind == "workflow_defaulted" for event in events)
    assert ".worktrees/" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_generic_board_task_without_metadata_stays_plain(kanban_home):
    kb.create_board("generic", name="Generic")

    with kanban_db_connect.connect(board="generic") as conn:
        tid = kb.create_task(conn, title="plain", board="generic")
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.workflow_template_id is None
    assert task.current_step_key is None
    assert task.workspace_kind == "scratch"
    assert not any(event.kind == "workflow_defaulted" for event in events)


def test_project_bound_task_explicit_non_product_metadata_not_overwritten(kanban_home, tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    repo = tmp_path / "product-repo"
    _init_git_repo(repo)
    kb.ensure_product_board_defaults("prod", name="Product", default_workdir=str(repo))
    monkeypatch.setenv("HERMES_HOME", str(kanban_home))
    with pdb.connect_closing() as pconn:
        project_id = pdb.create_project(
            pconn,
            name="Product Repo",
            primary_path=str(repo),
            board_slug="prod",
        )

    with kanban_db_connect.connect(board="prod") as conn:
        tid = kb.create_task(
            conn,
            title="custom workflow",
            project_id=project_id,
            board="prod",
            workflow_template_id="custom",
            current_step_key="intake",
        )
        task = kb.get_task(conn, tid)

    assert task.workflow_template_id == "custom"
    assert task.current_step_key == "intake"


def test_product_board_role_story_creation_gets_workflow_metadata(kanban_home, tmp_path):
    board = "product-board-enf-create"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_product_board_enf(board, repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="User story: Safe paper order evidence",
            assignee="architect",
            board=board,
        )
        task = kb.get_task(conn, tid)
    # masquerade fix: a plain architect card on a product board becomes a real
    # product story anchored to the correct step (architecture), not stuck.
    assert task.workflow_template_id == kb.PRODUCT_WORKFLOW_TEMPLATE_ID
    assert task.current_step_key == "architecture"
    assert task.status == "ready"


def test_product_board_claim_repairs_legacy_plain_architect_story(kanban_home, tmp_path):
    board = "product-board-enf-claim"
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_product_board_enf(board, repo)
    with kanban_db_connect.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="User story: Legacy card",
            assignee="architect",
            board=board,
        )
        # Simulate the Trading Company regression: a plain architect card with
        # NULL workflow fields slipped onto a product board before this fix.
        conn.execute(
            "UPDATE tasks SET workflow_template_id=NULL, current_step_key=NULL, "
            "workspace_kind='scratch', workspace_path=NULL WHERE id=?",
            (tid,),
        )
        conn.commit()
        claimed = kb.claim_task(conn, tid, board=board)
        repaired = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    assert claimed is not None
    assert repaired.workflow_template_id == kb.PRODUCT_WORKFLOW_TEMPLATE_ID
    assert repaired.current_step_key == "architecture"
    assert any(e.kind == "workflow_repaired" for e in events)


def test_merge_standalone_story_to_main_happy_merges_and_never_pushes(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-merge-happy"
    _v2_product_board_with_repo(board, repo)
    story, sha = _make_done_standalone_story(board, repo)

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb._merge_standalone_story_to_main(
            conn, story, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )

    assert result == "merged"
    notify.assert_not_called()
    _assert_no_push(calls)
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "main"],
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 0, "main must contain the story's commit"


def test_release_reverifies_already_merged_standalone_story(
    kanban_home, tmp_path,
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-already-merged-release"
    _v2_product_board_with_repo(board, repo)
    story, source_sha = _make_done_standalone_story(board, repo)
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--ff-only", "wt/story-1"],
        check=True,
        capture_output=True,
        text=True,
    )
    observed: list[str] = []

    def verify(candidate: Path) -> bool:
        observed.append((candidate / "epic_work.txt").read_text(encoding="utf-8"))
        return True

    with kanban_db_connect.connect(board=board) as conn:
        result = kb._merge_standalone_story_to_main(
            conn,
            story,
            board=board,
            candidate_verify_fn=verify,
            expected_source_sha=source_sha,
            allow_release_measure=True,
        )
        event = next(
            event
            for event in kb.list_events(conn, story)
            if event.kind == "story_merged_to_main"
        )

    assert result == "already_merged"
    assert observed == ["epic work\n"]
    assert event.payload["source_sha"] == source_sha


def test_merge_standalone_story_with_epic_returns_none(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-has-epic"
    _v2_product_board_with_repo(board, repo)
    with kanban_db_connect.connect(board=board) as conn:
        epic = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story = kb.create_task(
            conn, title="Story", board=board,
            branch_name="wt/s", workspace_kind="worktree", workspace_path=str(repo),
        )
        kb.add_epic_membership(conn, epic_id=epic, task_id=story)
        _set_task_status(conn, story, "done")
        result = kb._merge_standalone_story_to_main(conn, story, board=board, verify_fn=lambda b: True)
    assert result is None  # epic'd stories go through the epic path, not here


def test_merge_standalone_story_conflict_aborts_blocks_never_pushes(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-merge-conflict"
    _v2_product_board_with_repo(board, repo)
    story, _ = _make_done_standalone_story(board, repo)
    # make main touch the SAME file the story branch changed -> conflict
    _commit_file(repo, "epic_work.txt", "conflicting main content\n", "main change")
    pre_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()

    calls = _record_git_calls(monkeypatch)
    notify = unittest.mock.Mock()
    with kanban_db_connect.connect(board=board) as conn:
        result = kb._merge_standalone_story_to_main(
            conn, story, board=board, verify_fn=lambda b: True, notify_fn=notify,
        )
        blocked = kb.get_task(conn, story).blocked

    assert result == "conflict"
    assert blocked, "story must be blocked on conflict"
    notify.assert_called()
    _assert_no_push(calls)
    post_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert post_main == pre_main, "main must be untouched after an aborted conflict"


def test_merge_standalone_story_verify_failure_resets_and_blocks(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-standalone-merge-verifyfail"
    _v2_product_board_with_repo(board, repo)
    story, _ = _make_done_standalone_story(board, repo)
    pre_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()

    _record_git_calls(monkeypatch)
    with kanban_db_connect.connect(board=board) as conn:
        result = kb._merge_standalone_story_to_main(
            conn, story, board=board, verify_fn=lambda b: False,  # suite red
        )
        blocked = kb.get_task(conn, story).blocked

    assert result == "verify_failed"
    assert blocked
    post_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert post_main == pre_main, "main must be reset to pre-merge sha on verify failure"


def test_reconcile_merge_after_green_OFF_does_not_merge(kanban_home, tmp_path, monkeypatch):
    """CRITICAL safety: with the default (merge_after_green unset), reconcile
    must NOT merge a done standalone story to main."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    board = "v2-reconcile-mergeback-off"
    _v2_product_board_with_repo(board, repo)  # NOTE: merge_after_green NOT set
    story, sha = _make_done_standalone_story(board, repo)
    pre_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()

    with kanban_db_connect.connect(board=board) as conn:
        result = kb.reconcile(conn, board=board, spawn_fn=lambda *a, **k: None)

    assert result.merged_to_main == [], "must not merge when policy is off"
    post_main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert post_main == pre_main, "main must be untouched when merge_after_green is off"


def test_reconcile_merge_after_green_ON_merges_one_standalone_per_pass(kanban_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "scripts").mkdir()
    test_script = repo / "scripts" / "run_tests.sh"
    test_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    test_script.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(repo), "add", "scripts/run_tests.sh"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "add test gate"],
        check=True, capture_output=True, text=True,
    )
    board = "v2-reconcile-mergeback-on"
    _v2_product_board_with_repo(board, repo)
    _enable_merge_after_green(board)
    story, sha = _make_done_standalone_story(board, repo)
    with kanban_db_connect.connect(board=board) as conn:
        result = kb.reconcile(conn, board=board, spawn_fn=lambda *a, **k: None)

    assert story in result.merged_to_main
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "main"],
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 0, "reconcile must carry the story into main when opted in"


def test_block_task_uses_connection_board_for_omitted_escalation(
    kanban_home, monkeypatch
):
    board_a = "block-connection-a"
    board_b = "block-connection-b"
    kb.ensure_product_board_defaults(board_a)
    kb.ensure_product_board_defaults(board_b)
    _set_human_escalation_profile(board_a, "resolver")
    _set_human_escalation_profile(board_b, "wrong-profile")
    kb.set_current_board(board_b)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board_a)))

    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="connection-board escalation",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        claimed = kb.claim_task(conn, task_id, board=board_a)
        assert claimed is not None
        assert kb.block_task(
            conn,
            task_id,
            reason="Need a human decision",
            kind="needs_input",
            attempted_resolutions=["checked the documented alternatives"],
            expected_run_id=claimed.current_run_id,
        )
        task = kb.get_task(conn, task_id)
        preflight = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "human_input_preflight"
        ][-1]

    assert task.assignee == "resolver"
    assert preflight.payload["hermes_assignee"] == "resolver"


def test_human_cli_block_uses_connection_board_for_omitted_escalation(
    kanban_home, monkeypatch, capsys
):
    board_a = "cli-connection-a"
    board_b = "cli-connection-b"
    kb.ensure_product_board_defaults(board_a)
    kb.ensure_product_board_defaults(board_b)
    _set_human_escalation_profile(board_a, "resolver")
    _set_human_escalation_profile(board_b, "wrong-profile")
    kb.set_current_board(board_b)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board_a)))

    with kanban_db_connect.connect(board=board_a) as conn:
        task_id = kb.create_task(
            conn,
            title="CLI connection-board escalation",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
        )
        claimed = kb.claim_task(conn, task_id, board=board_a)
        assert claimed is not None

    from hermes_cli import kanban

    args = types.SimpleNamespace(
        task_id=task_id,
        ids=[],
        reason=["Need", "a", "human", "decision"],
        kind="needs_input",
    )
    assert kanban._cmd_block(args) == 0
    capsys.readouterr()

    with kanban_db_connect.connect(board=board_a) as conn:
        task = kb.get_task(conn, task_id)
        preflight = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "human_input_preflight"
        ][-1]

    assert task.assignee == "resolver"
    assert preflight.payload["hermes_assignee"] == "resolver"


def test_d4_answer_reentry_happy_path_and_comments_are_tolerated(kanban_home):
    board = "d4-board"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        kb.add_comment(conn, tid, "operator", "I am checking the fixture.")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (tid, "audit", json.dumps({"note": "read-only observation"}), int(time.time())),
        )
        conn.commit()
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board,
            answer="Use the vendored fixture token.", answered_by="operator",
            expected=expected,
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
        comments = kb.list_comments(conn, tid)
    assert task is not None
    assert task.assignee == "resolver"
    assert task.status == "ready"
    assert task.blocked is False
    assert task.current_run_id is None
    answer_events = [event for event in events if event.id == event_id]
    assert len(answer_events) == 1
    assert answer_events[0].payload["human_answer"] == "Use the vendored fixture token."
    assert answer_events[0].payload["answered_by"] == "operator"
    assert not [comment for comment in comments if "Use the vendored" in comment.body]


def test_d4_expected_snapshot_is_a_db_boundary_cas(kanban_home):
    board = "d4-cas"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        stale = dict(expected)
        stale["escalation_event_id"] += 1
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=stale,
            )


def test_d4_lifecycle_mutation_makes_escalation_stale(kanban_home):
    board = "d4-stale-lifecycle"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        conn.execute("UPDATE tasks SET current_step_key='review' WHERE id=?", (tid,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (tid, "workflow_step", json.dumps({"current_step_key": "review"}), int(time.time())),
        )
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="stale", answered_by="operator",
                expected=expected,
            )


def test_d4_provenance_rejects_malformed_cross_task_and_wrong_profile(kanban_home):
    board = "d4-provenance"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        resolved_id = expected["escalation_event_id"] - 1
        conn.execute("UPDATE task_events SET payload='not-json' WHERE id=?", (resolved_id,))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )

    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        resolved_id = expected["escalation_event_id"] - 1
        payload = {"action": "escalate", "preflight_event_id": expected["preflight_event_id"]}
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), resolved_id))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )

    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        preflight = next(event for event in kb.list_events(conn, tid) if event.id == expected["preflight_event_id"])
        payload = dict(preflight.payload)
        payload["hermes_assignee"] = "not-resolver"
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), preflight.id))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )


def test_d4_cross_task_preflight_reference_is_rejected(kanban_home):
    board = "d4-cross-task"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        _, _, other_expected = _d4_escalated(conn, board)
        resolved_id = expected["escalation_event_id"] - 1
        resolved = next(event for event in kb.list_events(conn, tid) if event.id == resolved_id)
        payload = dict(resolved.payload or {})
        payload["preflight_event_id"] = other_expected["preflight_event_id"]
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), resolved_id),
        )
        conn.commit()
        current = kb.resolver_escalation_expected_snapshot(conn, tid)
        assert current is not None
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="cross-task", answered_by="operator",
                expected=current,
            )


def test_d4_rejects_same_task_historical_run_substitution(kanban_home):
    board = "d4-historical-run"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, first_run, first_expected = _d4_escalated(conn, board)
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="first answer", answered_by="operator",
            expected=first_expected,
        )
        fresh = kb.claim_task(conn, tid, board=board)
        assert fresh is not None and fresh.current_run_id is not None
        assert _resolve_preflight(
            conn, tid, fresh.current_run_id, board, decision="escalate",
            fault_domain="framework", reason="Second escalation",
        )
        current = _d4_expected_snapshot(conn, tid)
        conn.execute("UPDATE task_events SET run_id=? WHERE id=?", (first_run, current["escalation_event_id"]))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="stale second", answered_by="operator",
                expected=current,
            )


@pytest.mark.parametrize("bad_identity", ["", "   ", None, 0, [], {}, "x" * 257])
def test_d4_rejects_blank_or_non_string_answered_by(kanban_home, bad_identity):
    board = "d4-identity"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        with pytest.raises(ValueError, match="answered_by"):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by=bad_identity,
                expected=expected,
            )


@pytest.mark.parametrize("bad_answer", ["", "   ", None, 0, [], {}, "x" * 2049])
def test_d4_rejects_blank_non_string_or_oversized_answer(kanban_home, bad_answer):
    board = "d4-answer"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        with pytest.raises(ValueError, match="answer"):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer=bad_answer, answered_by="operator",
                expected=expected,
            )


@pytest.mark.parametrize("raw_assignee", [{}, [], 0, False, "", "   ", "x" * 257])
def test_d4_source_assignee_is_validated_before_coercion(kanban_home, raw_assignee):
    board = "d4-source-assignee"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        preflight = next(event for event in kb.list_events(conn, tid) if event.id == expected["preflight_event_id"])
        payload = dict(preflight.payload)
        payload["original_assignee"] = raw_assignee
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), preflight.id))
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )


def test_d4_missing_or_none_assignee_uses_governed_backlog_derivation(kanban_home):
    board = "d4-backlog"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board, step="backlog")
        preflight = next(event for event in kb.list_events(conn, tid) if event.id == expected["preflight_event_id"])
        payload = dict(preflight.payload)
        payload.pop("original_assignee", None)
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), preflight.id))
        conn.commit()
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="backlog answer", answered_by="operator",
            expected=expected,
        )
        events = kb.list_events(conn, tid)
    reentry = [event for event in events if event.payload.get("kind") == "resolver_reentry"][-1]
    assert reentry.payload["original_assignee"] == "productowner"


def test_d4_release_measure_resume_preserves_canonical_none(kanban_home):
    board = "d4-release-measure"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board, step="release_measure")
        preflight = next(event for event in kb.list_events(conn, tid) if event.id == expected["preflight_event_id"])
        payload = dict(preflight.payload)
        payload["original_assignee"] = None
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), preflight.id))
        conn.commit()
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="measure answer", answered_by="operator",
            expected=expected,
        )
        event = [event for event in kb.list_events(conn, tid) if event.payload.get("kind") == "resolver_reentry"][-1]
    assert event.payload["original_assignee"] is None


def test_d4_release_measure_resume_restores_none_after_fresh_resolver(kanban_home):
    board = "d4-release-measure-resume"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board, step="release_measure")
        preflight = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["preflight_event_id"]
        )
        payload = dict(preflight.payload)
        payload["original_assignee"] = None
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), preflight.id),
        )
        conn.commit()
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="measure answer", answered_by="operator",
            expected=expected,
        )
        fresh = kb.claim_task(conn, tid, board=board)
        assert fresh is not None and fresh.current_run_id is not None
        assert _resolve_preflight(
            conn, tid, fresh.current_run_id, board, decision="resume",
            fault_domain="task_state", reason="Apply measure answer",
        )
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee is None
    assert task.current_step_key == "release_measure"


@pytest.mark.parametrize("bad_configured_assignee", ["x" * 257, "", "   ", False, 0, [], {}])
def test_d4_configured_assignee_is_validated_before_helper_coercion(
    kanban_home, bad_configured_assignee,
):
    board = "d4-configured-assignee"
    _v2_product_board(board)
    metadata = kb.read_board_metadata(board)
    workflow = metadata.setdefault("product_workflow", {})
    workflow.setdefault("assignees", dict(kb.PRODUCT_WORKFLOW_DEFAULT_ASSIGNEES))
    workflow["assignees"]["developer"] = bad_configured_assignee
    kb.board_metadata_path(board).write_text(json.dumps(metadata), encoding="utf-8")
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        preflight = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["preflight_event_id"]
        )
        payload = dict(preflight.payload)
        payload["original_assignee"] = None
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), preflight.id),
        )
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )


def test_d4_non_resolver_block_fails_closed(kanban_home):
    board = "d4-non-resolver-block"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["escalation_event_id"]
        )
        payload = dict(blocked.payload)
        payload["kind"] = "needs_input"
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), blocked.id),
        )
        conn.commit()
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )


def test_d4_attempted_resolutions_are_bounded_and_one_event_is_written(kanban_home):
    board = "d4-bounds"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked = next(event for event in kb.list_events(conn, tid) if event.id == expected["escalation_event_id"])
        payload = dict(blocked.payload)
        payload["attempted_resolutions"] = ["x" * 1000 for _ in range(100)]
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), blocked.id))
        conn.commit()
        before = len(kb.list_events(conn, tid))
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="bounded", answered_by="operator",
            expected=expected,
        )
        events = kb.list_events(conn, tid)
    assert len(events) == before + 1
    answered = next(event for event in events if event.id == event_id)
    assert len(answered.payload["attempted_resolutions"]) <= 20
    assert sum(len(item) for item in answered.payload["attempted_resolutions"]) <= 4096


def test_d4_second_escalation_reentry_cycle_works(kanban_home):
    board = "d4-repeat"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, first_expected = _d4_escalated(conn, board)
        kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="first", answered_by="operator",
            expected=first_expected,
        )
        fresh = kb.claim_task(conn, tid, board=board)
        assert fresh is not None and fresh.current_run_id is not None
        assert _resolve_preflight(
            conn, tid, fresh.current_run_id, board, decision="resume",
            fault_domain="task_state", reason="First answer applied",
        )
        worker = kb.claim_task(conn, tid, board=board)
        assert worker is not None and worker.current_run_id is not None
        assert kb.block_task(
            conn, tid, reason="Need another decision", kind="needs_input",
            expected_run_id=worker.current_run_id, board=board,
            human_escalation_assignee="resolver",
        )
        second = kb.claim_task(conn, tid, board=board)
        assert second is not None and second.current_run_id is not None
        assert _resolve_preflight(
            conn, tid, second.current_run_id, board, decision="escalate",
            fault_domain="framework", reason="Second question",
        )
        second_expected = _d4_expected_snapshot(conn, tid)
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="second", answered_by="operator",
            expected=second_expected,
        )
        events = kb.list_events(conn, tid)
    assert event_id > 0
    assert len([event for event in events if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT]) == 4


def test_d4_concurrent_answers_have_exactly_one_winner_and_public_conflict(kanban_home):
    board = "d4-concurrent"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
    barrier = threading.Barrier(2)
    results = []

    def answer(answer_text):
        with kanban_db_connect.connect(board=board) as conn:
            barrier.wait(timeout=5)
            try:
                event_id = kb.reenter_resolver_escalation(
                    conn, tid, board=board, answer=answer_text, answered_by="operator",
                    expected=dict(expected),
                )
                results.append(("ok", event_id))
            except Exception as exc:
                results.append(("error", exc))

    threads = [threading.Thread(target=answer, args=(f"answer-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len([result for result in results if result[0] == "ok"]) == 1, results
    errors = [result[1] for result in results if result[0] == "error"]
    assert len(errors) == 1, results
    assert isinstance(errors[0], kb.TaskSnapshotConflict), errors
    with kanban_db_connect.connect(board=board) as conn:
        answer_events = [
            event for event in kb.list_events(conn, tid)
            if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT
            and event.payload.get("kind") == "resolver_reentry"
        ]
    assert len(answer_events) == 1


def test_d4_real_dispatcher_claims_development_and_review_reentry(kanban_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    original_claim_review = kb.claim_review_task
    for step, board in [("development", "d4-dispatch-dev"), ("review", "d4-dispatch-review")]:
        _v2_product_board(board)
        with kanban_db_connect.connect(board=board) as conn:
            tid, _, expected = _d4_escalated(conn, board, step=step)
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="dispatch answer", answered_by="operator",
                expected=expected,
            )

        calls = []
        monkeypatch.setattr(
            kb, "claim_review_task",
            lambda conn, task_id, _claim=original_claim_review, **kwargs: (
                calls.append(task_id), _claim(conn, task_id, **kwargs)
            )[1],
        )
        with kanban_db_connect.connect(board=board) as conn:
            result = kbd.dispatch_once(conn, spawn_fn=lambda task, workspace, board=None: 9001, board=board)
            task = kb.get_task(conn, tid)
            run = kb.get_run(conn, task.current_run_id) if task and task.current_run_id else None
        assert tid in [spawn[0] for spawn in result.spawned]
        assert task is not None and task.current_run_id is not None
        assert run is not None and run.profile == "resolver"


@pytest.mark.parametrize(
    "field",
    [
        "assignee",
        "project_id",
        "workflow_template_id",
        "current_step_key",
        "workspace_path",
        "branch_name",
    ],
)
def test_d3_rejects_oversized_cas_metadata_before_create_mutation(kanban_home, field):
    kwargs = {
        "title": "D3 oversized metadata",
        "workspace_kind": "worktree",
        field: "值🙂é" * 30_000,
    }
    if field in {"workflow_template_id", "current_step_key"}:
        kwargs["workflow_template_id"] = "product"
        kwargs["current_step_key"] = "development"
        kwargs[field] = "值🙂é" * 30_000
    if field == "project_id":
        # The bound must be checked before project lookup, so a huge ID cannot
        # be hidden behind the normal unknown-project error.
        kwargs["workspace_kind"] = "scratch"

    with kanban_db_connect.connect() as conn:
        with pytest.raises(ValueError, match=field):
            kb.create_task(conn, **kwargs)
        assert kb.list_tasks(conn) == []


def test_d4_rejects_coherently_forged_non_resolver_cycle_without_mutation(kanban_home):
    board = "d4-forged-resolver"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked_id = expected["escalation_event_id"]
        preflight = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["preflight_event_id"]
        )
        resolved_id = blocked_id - 1
        resolved = next(event for event in kb.list_events(conn, tid) if event.id == resolved_id)
        forged_preflight = dict(preflight.payload)
        forged_preflight["hermes_assignee"] = "developer"
        forged_resolved = dict(resolved.payload)
        forged_resolved["resolver_profile"] = "developer"
        conn.execute(
            "UPDATE task_runs SET profile='developer' WHERE id=?",
            (expected["run_id"],),
        )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(forged_preflight), preflight.id),
        )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(forged_resolved), resolved.id),
        )
        conn.commit()
        before_task = kb.get_task(conn, tid)
        before_events = kb.list_events(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )
        assert kb.get_task(conn, tid) == before_task
        assert kb.list_events(conn, tid) == before_events


def test_d4_default_human_escalation_profile_accepts_resolver_cycle(kanban_home):
    board = "d4-default-human-profile"
    _v2_product_board(board)
    _set_human_escalation_profile(board, "default")
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="answer", answered_by="operator",
            expected=expected,
        )
        task = kb.get_task(conn, tid)
        answer_event = next(event for event in kb.list_events(conn, tid) if event.id == event_id)
    assert task is not None and task.assignee == "resolver"
    assert answer_event.payload["hermes_assignee"] == "resolver"


def test_d4_rejects_coherently_forged_default_cycle_without_mutation(kanban_home):
    board = "d4-forged-default"
    _v2_product_board(board)
    _set_human_escalation_profile(board, "default")
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked_id = expected["escalation_event_id"]
        preflight = next(
            event for event in kb.list_events(conn, tid)
            if event.id == expected["preflight_event_id"]
        )
        resolved_id = blocked_id - 1
        resolved = next(event for event in kb.list_events(conn, tid) if event.id == resolved_id)
        forged_preflight = dict(preflight.payload)
        forged_preflight["hermes_assignee"] = "default"
        forged_resolved = dict(resolved.payload)
        forged_resolved["resolver_profile"] = "default"
        conn.execute(
            "UPDATE task_runs SET profile='default' WHERE id=?",
            (expected["run_id"],),
        )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(forged_preflight), preflight.id),
        )
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(forged_resolved), resolved.id),
        )
        conn.commit()
        before = _full_tables_state(conn)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )
        assert _full_tables_state(conn) == before


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("reason", 123),
        ("reason", None),
        ("resolution", {}),
        ("resolution", False),
        ("attempted_resolutions", {}),
        ("attempted_resolutions", None),
        ("attempted_resolutions", ["valid", 7]),
    ],
)
def test_d4_rejects_present_malformed_copied_evidence_atomically(
    kanban_home, field, bad_value
):
    board = f"d4-evidence-{field}-{type(bad_value).__name__}"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked = next(event for event in kb.list_events(conn, tid) if event.id == expected["escalation_event_id"])
        payload = dict(blocked.payload)
        payload[field] = bad_value
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), blocked.id),
        )
        conn.commit()
        before_task = kb.get_task(conn, tid)
        before_events = kb.list_events(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )
        assert kb.get_task(conn, tid) == before_task
        assert kb.list_events(conn, tid) == before_events


def test_d4_absent_optional_copied_evidence_remains_distinct_from_malformed(kanban_home):
    board = "d4-evidence-absent"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        blocked = next(event for event in kb.list_events(conn, tid) if event.id == expected["escalation_event_id"])
        payload = dict(blocked.payload)
        payload.pop("reason")
        payload.pop("resolution")
        payload.pop("attempted_resolutions")
        conn.execute(
            "UPDATE task_events SET payload=? WHERE id=?",
            (json.dumps(payload), blocked.id),
        )
        conn.commit()
        event_id = kb.reenter_resolver_escalation(
            conn, tid, board=board, answer="answer", answered_by="operator",
            expected=expected,
        )
        answer_event = next(event for event in kb.list_events(conn, tid) if event.id == event_id)
    assert answer_event.payload["reason"] == ""
    assert answer_event.payload["resolver_escalation_reason"] == ""
    assert answer_event.payload["attempted_resolutions"] == []


@pytest.mark.parametrize(
    "event_kind",
    [
        "dispatcher_metadata_conflict",
        "diagnostic",
        "heartbeat",
        "traceability",
        "unknown",
        "workflow_step",
        "status_changed",
    ],
)
def test_d4_non_audit_events_invalidate_escalation_without_mutation(kanban_home, event_kind):
    board = f"d4-event-{event_kind}"
    _v2_product_board(board)
    with kanban_db_connect.connect(board=board) as conn:
        tid, _, expected = _d4_escalated(conn, board)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (tid, event_kind, json.dumps({"note": "later event"}), int(time.time())),
        )
        conn.commit()
        before_task = kb.get_task(conn, tid)
        before_events = kb.list_events(conn, tid)
        with pytest.raises(kb.TaskSnapshotConflict):
            kb.reenter_resolver_escalation(
                conn, tid, board=board, answer="answer", answered_by="operator",
                expected=expected,
            )
        assert kb.get_task(conn, tid) == before_task
        assert kb.list_events(conn, tid) == before_events
























def test_qualification_attempt_budget_counts_all_historical_runs_and_preserves_history(
    kanban_home,
):
    board = "qualification-attempt-budget"
    kb.ensure_product_board_defaults(board)
    metadata_path = kb.board_metadata_path(board)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["max_total_attempts"] = 3
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with kanban_db_connect.connect(board=board) as conn:
        intake_id = kb.create_qualification_intake(
            conn, raw_request="budget me", source="chat", created_at=10
        )
        runtime = {"provider": "test", "model": "test", "effort": "low"}
        runs = []
        for now in (20, 30, 40):
            run = kb.claim_qualification_intake(
                conn,
                intake_id,
                profile="productowner",
                runtime_identity=runtime,
                now=now,
            )
            assert run is not None
            runs.append(run)
            assert kb.finish_qualification_intake_run(
                conn,
                intake_id=intake_id,
                run_id=run["id"],
                claim_lock=run["claim_lock"],
                intake_status="attention_required",
                outcome="attention_required",
                now=now + 1,
            )
            if now != 40:
                assert kb.retry_qualification_intake(conn, intake_id, now=now + 2)
        state = kb.qualification_retry_state(conn, intake_id, 3)
        assert state.attempts_used == 3
        assert state.attempts_limit == 3
        assert state.allowed is False
        assert state.reason == "attempt_budget_exhausted"
        before_runs = conn.execute(
            "SELECT id, status, outcome FROM qualification_intake_runs "
            "WHERE intake_id = ? ORDER BY id",
            (intake_id,),
        ).fetchall()
        with pytest.raises(ValueError, match="attempt_budget_exhausted"):
            kb.retry_qualification_intake(conn, intake_id, now=50)
        after_runs = conn.execute(
            "SELECT id, status, outcome FROM qualification_intake_runs "
            "WHERE intake_id = ? ORDER BY id",
            (intake_id,),
        ).fetchall()
    assert [tuple(row) for row in after_runs] == [tuple(row) for row in before_runs]


def test_configure_task_atomically_clears_contract_and_records_exact_event(
    kanban_home,
):
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="configure existing card",
            assignee="developer",
            source_commit_required=True,
            max_retries=3,
            max_runtime_seconds=900,
            goal_mode=True,
        )
        before = kb.get_task(conn, task_id)
        assert before is not None

        assert kb.configure_task(
            conn,
            task_id,
            expected=_configure_task_expected(before),
            source_policy="none",
            max_retries=None,
            max_runtime_seconds=None,
            goal_mode=False,
        ) is True

        after = kb.get_task(conn, task_id)
        configured = kb.list_events(conn, task_id)[-1]

    assert after is not None
    assert after.source_commit_required is False
    assert after.source_commit_forbidden is False
    assert after.max_retries is None
    assert after.max_runtime_seconds is None
    assert after.goal_mode is False
    assert configured.kind == "execution_contract_configured"
    assert configured.payload == {
        "before": {
            "source_policy": "required",
            "max_retries": 3,
            "max_runtime_seconds": 900,
            "goal_mode": True,
        },
        "after": {
            "source_policy": "none",
            "max_retries": None,
            "max_runtime_seconds": None,
            "goal_mode": False,
        },
    }


@pytest.mark.parametrize(
    ("status", "blocked"),
    [
        ("triage", 0),
        ("todo", 0),
        ("scheduled", 0),
        ("ready", 0),
        ("blocked", 1),
        ("review", 0),
    ],
)
def test_configure_task_allows_each_eligible_status(kanban_home, status, blocked):
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(conn, title=f"eligible {status}")
        conn.execute(
            "UPDATE tasks SET status = ?, blocked = ? WHERE id = ?",
            (status, blocked, task_id),
        )
        conn.commit()
        task = kb.get_task(conn, task_id)
        assert task is not None

        assert kb.configure_task(
            conn,
            task_id,
            expected=_configure_task_expected(task),
            source_policy="forbidden",
            max_retries=2,
            max_runtime_seconds=120,
            goal_mode=True,
        ) is True


@pytest.mark.parametrize("stale_field", ["title", "max_retries"])
def test_configure_task_cas_rejects_stale_lifecycle_and_execution_fields(
    kanban_home, stale_field
):
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(conn, title="stale card")
        task = kb.get_task(conn, task_id)
        assert task is not None
        expected = _configure_task_expected(task)
        if stale_field == "title":
            conn.execute(
                "UPDATE tasks SET title = ? WHERE id = ?", ("changed", task_id)
            )
        else:
            conn.execute(
                "UPDATE tasks SET max_retries = ? WHERE id = ?", (4, task_id)
            )
        conn.commit()
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(kb.TaskSnapshotConflict):
            kb.configure_task(
                conn,
                task_id,
                expected=expected,
                source_policy="required",
                max_retries=1,
                max_runtime_seconds=300,
                goal_mode=True,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


def test_configure_task_refuses_second_write_with_same_expectation(kanban_home):
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(conn, title="single CAS write")
        task = kb.get_task(conn, task_id)
        assert task is not None
        expected = _configure_task_expected(task)
        values = {
            "source_policy": "required",
            "max_retries": 1,
            "max_runtime_seconds": 300,
            "goal_mode": True,
        }
        assert kb.configure_task(
            conn, task_id, expected=expected, **values
        ) is True
        before_replay = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(kb.TaskSnapshotConflict):
            kb.configure_task(conn, task_id, expected=expected, **values)

        assert _configure_task_row_and_events(conn, task_id) == before_replay


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_policy", "sometimes"),
        ("max_retries", 0),
        ("max_retries", True),
        ("max_runtime_seconds", 0),
        ("max_runtime_seconds", True),
        ("goal_mode", "false"),
    ],
)
def test_configure_task_rejects_invalid_values_without_mutation(
    kanban_home, field, value
):
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(conn, title="invalid contract")
        task = kb.get_task(conn, task_id)
        assert task is not None
        values = {
            "source_policy": "none",
            "max_retries": None,
            "max_runtime_seconds": None,
            "goal_mode": False,
        }
        values[field] = value
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(ValueError):
            kb.configure_task(
                conn,
                task_id,
                expected=_configure_task_expected(task),
                **values,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


def test_configure_task_rejects_incomplete_expected_snapshot_without_mutation(
    kanban_home,
):
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(conn, title="incomplete expectation")
        task = kb.get_task(conn, task_id)
        assert task is not None
        expected = _configure_task_expected(task)
        expected.pop("goal_mode")
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(ValueError, match="expected"):
            kb.configure_task(
                conn,
                task_id,
                expected=expected,
                source_policy="none",
                max_retries=None,
                max_runtime_seconds=None,
                goal_mode=False,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


@pytest.mark.parametrize("status", ["running", "done", "archived"])
def test_configure_task_refuses_terminal_status_without_mutation(
    kanban_home, status
):
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(conn, title=f"terminal {status}")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()
        task = kb.get_task(conn, task_id)
        assert task is not None
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(RuntimeError, match="status"):
            kb.configure_task(
                conn,
                task_id,
                expected=_configure_task_expected(task),
                source_policy="required",
                max_retries=1,
                max_runtime_seconds=300,
                goal_mode=True,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


def test_configure_task_refuses_active_current_run_without_mutation(kanban_home):
    with kanban_db_connect.connect() as conn:
        task_id = kb.create_task(conn, title="active run")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        before = _configure_task_row_and_events(conn, task_id)

        with pytest.raises(RuntimeError, match="active/current run"):
            kb.configure_task(
                conn,
                task_id,
                expected=_configure_task_expected(claimed),
                source_policy="required",
                max_retries=1,
                max_runtime_seconds=300,
                goal_mode=True,
            )

        assert _configure_task_row_and_events(conn, task_id) == before


def test_engine_owned_integration_pending_refuses_public_lifecycle_paths(
    kanban_home, monkeypatch
):
    board = "engine-owned-guards"
    _v2_product_board(board)
    monkeypatch.setattr(
        kb, "_integration_git",
        lambda *_args, **_kwargs: pytest.fail("guarded path must not call Git"),
    )
    monkeypatch.setattr(
        kb.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("guarded path must not spawn Git"),
    )
    with kanban_db_connect.connect(board=board) as conn:
        story_id = kb.create_task(
            conn, title="Story", board=board,
            workflow_template_id="product", current_step_key="review",
        )
        conn.execute(
            "UPDATE tasks SET current_step_key='integration_pending', status='ready', "
            "assignee=NULL WHERE id=?", (story_id,),
        )
        assert kb.claim_task(conn, story_id, board=board) is None
        assert kb.complete_task(conn, story_id, board=board) is False
        assert kb.set_phase(conn, story_id, "development", board=board) is False
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (story_id,))
        promoted, reason = kb.promote_task(conn, story_id, actor="operator", force=True)
        assert promoted is False
        assert reason == "engine-owned integration state cannot be promoted"
        assert kb.recompute_ready(conn) == 0
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (story_id,))
        assert kb.unblock_task(conn, story_id) is False
        with pytest.raises(kb.ReleaseEvidenceError) as exc:
            kb.release_product_task(
                conn, story_id, board,
                candidate_verify_fn=None, release_adapter=None,
            )
        assert exc.value.missing == ["engine_owned_state"]
        with pytest.raises(ValueError, match="engine-owned"):
            kb.create_task(
                conn, title="Fabricated inbox delivery", board=board,
                workflow_template_id="product", current_step_key="integration_pending",
            )


def test_product_epic_and_legacy_reconcile_are_structurally_refused_without_git(
    kanban_home, monkeypatch
):
    board = "product-epic-guards"
    _v2_product_board(board)
    monkeypatch.setattr(
        kb, "_integration_git",
        lambda *_args, **_kwargs: pytest.fail("guarded path must not call Git"),
    )
    monkeypatch.setattr(
        kb.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("guarded path must not spawn Git"),
    )
    with kanban_db_connect.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story_id = kb.create_task(conn, title="Story", board=board)
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product_epic', "
            "current_step_key='collecting_members', status='todo' WHERE id=?", (epic_id,),
        )
        conn.execute(
            "UPDATE tasks SET workflow_template_id='product', current_step_key='done', "
            "status='done', completed_at=1 WHERE id=?", (story_id,),
        )
        assert kb.merge_epic_to_main(conn, epic_id, board=board) == "not_ready"
        assert kb.complete_task(conn, epic_id, board=board) is False
        assert kb.promote_task(conn, epic_id, actor="operator", force=True)[0] is False
        with pytest.raises(kb.ReleaseEvidenceError) as exc:
            kb.release_product_task(
                conn, epic_id, board,
                candidate_verify_fn=lambda _path: True,
                release_adapter=None,
                completion_metadata={
                    "workflow_outcome": {"verdict": "approved"},
                    "candidate_sha": "f" * 40,
                },
            )
        assert exc.value.missing == ["engine_owned_state"]
        result = kb.reconcile(conn, board=board, spawn_ready=False)
        facts = conn.execute(
            "SELECT COUNT(*) FROM epic_story_integrations WHERE story_id=?", (story_id,),
        ).fetchone()[0]
    assert result.integrated == []
    assert facts == 0


def test_integration_enqueued_accepts_test_by_development_provider_without_test_writer(
    kanban_home, tmp_path
):
    board = "member-review-enqueue"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    base_sha = _git_output(repo, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "story/member"],
        check=True, capture_output=True, text=True,
    )
    source_sha = _commit_file(repo, "member.txt", "member\n", "member")
    _v2_product_board(board)
    now = int(time.time())
    developer_executor = {
        "profile": "developer",
        "provider": "openrouter",
        "model": "developer-model",
        "effort": "high",
        "surface": "hermes-primary",
    }
    reviewer_executor = {
        "profile": "reviewer",
        "provider": "claude-cli",
        "model": "reviewer-model",
        "effort": "high",
        "surface": "hermes-primary",
    }
    test_metadata = {
        "workflow_outcome": {"verdict": "passed"},
        "ai_provenance": {
            "tester": {"agent": "openrouter", "result": "passed"},
        },
        "test_branch": "story/member",
        "test_head_sha": source_sha,
    }
    review_pins = {
        "review_branch": "story/member",
        "review_base_sha": base_sha,
        "review_head_sha": source_sha,
        "executor": reviewer_executor,
    }
    completion_metadata = {
        "workflow_outcome": {"verdict": "approved"},
        "ai_provenance": {
            "writer": {"agent": "developer"},
            "reviewer": {"agent": "reviewer"},
        },
    }
    with kanban_db_connect.connect(board=board) as conn:
        epic_id = kb.create_task(conn, title="Epic", board=board, work_item_kind="epic")
        story_id = kb.create_task(
            conn, title="Story", board=board, assignee="reviewer",
            workflow_template_id="product", current_step_key="review",
            workspace_kind="worktree", workspace_path=str(repo),
            branch_name="story/member",
        )
        kb.add_epic_membership(conn, epic_id=epic_id, task_id=story_id)
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'development', 'completed', 'advanced', ?, ?, ?)",
            (
                story_id,
                json.dumps({"executor": developer_executor}),
                now - 4,
                now - 3,
            ),
        )
        conn.execute(
            "INSERT INTO task_runs "
            "(task_id, step_key, status, outcome, metadata, started_at, ended_at) "
            "VALUES (?, 'test', 'completed', 'advanced', ?, ?, ?)",
            (story_id, json.dumps(test_metadata), now - 2, now - 1),
        )
        review_run_id = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, step_key, status, metadata, started_at) "
            "VALUES (?, 'reviewer', 'review', 'running', ?, ?)",
            (story_id, json.dumps(review_pins), now),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status='running', running=1, current_run_id=? WHERE id=?",
            (review_run_id, story_id),
        )

        assert kb.complete_task(
            conn, story_id, board=board, expected_run_id=review_run_id,
            summary="approved", metadata=completion_metadata,
        ) is True
        task = kb.get_task(conn, story_id)
        intents = conn.execute(
            "SELECT * FROM story_integration_intents WHERE story_id=?", (story_id,),
        ).fetchall()

    assert task is not None
    assert task.current_step_key == "integration_pending"
    assert task.status == "review"
    assert task.assignee is None
    assert len(intents) == 1
    assert intents[0]["source_sha"] == source_sha

