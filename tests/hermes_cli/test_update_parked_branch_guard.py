"""Regression tests for the parked-branch guard in ``hermes update``.

Live incident (2026-08-17, Teknium's Linux box): the source checkout was
parked on a stale feature branch (``claude-code-inspired/local-terminal-
memory-limit``, days behind main) left there by earlier tooling. ``hermes
update`` autostashed, refreshed lazy backends, synced skills and printed
"✓ Code updated!" / "✓ Update complete!" — while the checkout stayed on the
stale branch with none of main's new code. Two sessions burned time on
"the fix is missing" confusion that was really this.

The guard (``_assess_parked_branch_switch``):
- clean tree + branch fully merged into origin/<target>  → safe to
  auto-switch back to the target (and STAY there — no switch-back).
- dirty tree, unmerged commits, git failure, or the
  ``updates.auto_switch_parked_branch: false`` opt-out → do NOT touch the
  branch; warn loudly and mark the code update SKIPPED.

These tests run the guard against REAL git repositories (init, commit,
branch, clone) — not mocked subprocess.run — so they exercise the actual
``git status`` / ``git cherry`` semantics the guard depends on.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd


GIT = ["git"]


def _git(cwd, *args, check=True):
    return subprocess.run(
        GIT + list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture()
def repo_pair(tmp_path):
    """A real origin repo + local clone, with main two commits ahead of the
    clone's parked state.

    Returns (clone_path,). The clone starts parked on feature branch
    ``old-feature`` cut from the first commit; origin/main has moved on.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    (origin / "a.txt").write_text("one\n")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-qm", "c1")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    # Park the clone on a feature branch cut at c1.
    _git(clone, "checkout", "-qb", "old-feature")

    # main advances upstream (two commits).
    (origin / "a.txt").write_text("two\n")
    _git(origin, "commit", "-aqm", "c2")
    (origin / "b.txt").write_text("three\n")
    _git(origin, "add", "b.txt")
    _git(origin, "commit", "-qm", "c3")

    _git(clone, "fetch", "-q", "origin", "main")
    return clone


@pytest.fixture(autouse=True)
def _no_config(monkeypatch):
    """Isolate the guard from the machine's real config.yaml."""
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_config, "load_config", lambda: {})


# ---------------------------------------------------------------------------
# _assess_parked_branch_switch against real repos
# ---------------------------------------------------------------------------

def test_clean_fully_merged_branch_is_safe_to_switch(repo_pair):
    """Parked branch == ancestor of origin/main, clean tree → auto-switch."""
    safe, reason = update_cmd._assess_parked_branch_switch(
        GIT, repo_pair, "old-feature", "main"
    )
    assert safe is True
    assert reason == ""


@pytest.mark.parametrize("shape", ["diverged", "ahead", "detached", "parked_target"])
def test_update_refuses_without_touching_local_history_or_services(
    repo_pair, monkeypatch, capsys, shape
):
    """September 2 shape: three local fixes must survive Update unchanged."""
    _git(repo_pair, "checkout", "-q", "main")
    if shape == "ahead":
        _git(repo_pair, "merge", "--ff-only", "origin/main")
    local_commits = []
    for index in range(3):
        path = repo_pair / f"fix-{index}.txt"
        path.write_text(f"local infrastructure fix {index}\n")
        _git(repo_pair, "add", path.name)
        _git(repo_pair, "commit", "-qm", f"local fix {index}")
        local_commits.append(_git(repo_pair, "rev-parse", "HEAD").stdout.strip())
    if shape == "detached":
        _git(repo_pair, "checkout", "-q", "--detach")
    elif shape == "parked_target":
        _git(repo_pair, "checkout", "-q", "old-feature")
    else:
        (repo_pair / "a.txt").write_text("uncommitted user edit\n")
        (repo_pair / "scratch.txt").write_text("untracked user work\n")
    before_head = _git(repo_pair, "rev-parse", "HEAD").stdout
    before_refs = _git(repo_pair, "show-ref", "--heads").stdout
    before_status = _git(repo_pair, "status", "--porcelain").stdout
    before_files = {p.name: p.read_bytes() for p in repo_pair.iterdir() if p.is_file()}
    _patch_update_flow(monkeypatch, repo_pair)
    effects = []
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: effects.append("pause"))
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *a: effects.append("eol"))

    class ReachedDependencies(Exception):
        pass

    monkeypatch.setattr(hermes_main, "_abort_dependency_sync_if_self_locked", lambda *a, **k: (_ for _ in ()).throw(ReachedDependencies()))
    with pytest.raises((SystemExit, ReachedDependencies)):
        hermes_main.cmd_update(SimpleNamespace(yes=True))

    assert _git(repo_pair, "rev-parse", "HEAD").stdout == before_head
    assert _git(repo_pair, "show-ref", "--heads").stdout == before_refs
    assert _git(repo_pair, "status", "--porcelain").stdout == before_status
    assert {p.name: p.read_bytes() for p in repo_pair.iterdir() if p.is_file()} == before_files
    for sha in local_commits:
        assert _git(repo_pair, "merge-base", "--is-ancestor", sha, "main", check=False).returncode == 0
    assert _git(repo_pair, "stash", "list").stdout == ""
    assert effects == []
    output = capsys.readouterr().out
    assert "Update refused" in output
    assert "Update complete" not in output
    from hermes_cli import update_receipt

    receipt = json.loads((update_receipt._receipt_dir() / "latest.json").read_text())
    assert receipt["outcome"] == "refused"
    assert receipt["stop_reason"].startswith("git_")


@pytest.mark.parametrize("relation", ["equal", "fast_forward", "local_commits", "unknown"])
def test_git_relation_probes_real_history_without_mutation(repo_pair, relation):
    local = _git(repo_pair, "rev-parse", "HEAD").stdout.strip()
    remote = _git(repo_pair, "rev-parse", "origin/main").stdout.strip()
    if relation == "equal":
        remote = local
    elif relation == "local_commits":
        local, remote = remote, local
    elif relation == "unknown":
        remote = "0" * 40
    before = _git(repo_pair, "show-ref").stdout
    assert update_cmd._git_update_relation(GIT, repo_pair, local, remote) == relation
    assert _git(repo_pair, "show-ref").stdout == before


def test_git_probe_error_refuses_before_service_pause(repo_pair, monkeypatch):
    _git(repo_pair, "checkout", "-q", "main")
    _patch_update_flow(monkeypatch, repo_pair)
    real_run = subprocess.run
    effects = []

    def fail_probe(cmd, **kwargs):
        if cmd[1:3] == ["merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(cmd, 128, "", "object unavailable")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_probe)
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: effects.append("pause"))
    before = _git(repo_pair, "rev-parse", "HEAD").stdout
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert effects == []
    assert _git(repo_pair, "rev-parse", "HEAD").stdout == before


def test_rewritten_upstream_does_not_discard_patch_equivalent_local_tip(repo_pair, monkeypatch):
    _git(repo_pair, "checkout", "-q", "main")
    _git(repo_pair, "merge", "--ff-only", "origin/main")
    local = _git(repo_pair, "rev-parse", "HEAD").stdout.strip()
    origin = repo_pair.parent / "origin"
    _git(origin, "commit", "--amend", "-qm", "rewritten upstream message")
    _patch_update_flow(monkeypatch, repo_pair)
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert _git(repo_pair, "rev-parse", "HEAD").stdout.strip() == local
    assert _git(repo_pair, "cherry", "origin/main").stdout.startswith("- ")


def test_configured_custom_merge_requires_recovery_tag(repo_pair, monkeypatch):
    import hermes_cli.config as config

    (repo_pair / "feature.txt").write_text("local custom work\n")
    _git(repo_pair, "add", "feature.txt")
    _git(repo_pair, "commit", "-qm", "custom work")
    before = _git(repo_pair, "rev-parse", "HEAD").stdout
    _patch_update_flow(monkeypatch, repo_pair)
    monkeypatch.setattr(config, "load_config", lambda: {"updates": {"parked_branch_strategy": "update_in_place"}})
    real_run = subprocess.run

    def deny_tag(cmd, **kwargs):
        if cmd[1] == "tag":
            return subprocess.CompletedProcess(cmd, 1, "", "ref permission denied")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", deny_tag)
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert _git(repo_pair, "rev-parse", "HEAD").stdout == before
    assert not (repo_pair / "b.txt").exists()


def test_intentional_lockfile_edit_is_preserved_on_safe_update(repo_pair, monkeypatch):
    _git(repo_pair, "checkout", "-q", "main")
    # A tracked lockfile exists on both sides, with a local user-only edit.
    origin = repo_pair.parent / "origin"
    (origin / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    _git(origin, "add", "package-lock.json")
    _git(origin, "commit", "-qm", "add lockfile")
    _git(repo_pair, "fetch", "-q", "origin", "main")
    _git(repo_pair, "merge", "--ff-only", "origin/main")
    (origin / "new.txt").write_text("new remote feature\n")
    _git(origin, "add", "new.txt")
    _git(origin, "commit", "-qm", "new remote feature")
    edited = '{"lockfileVersion":3,"userChange":true}\n'
    (repo_pair / "package-lock.json").write_text(edited)
    _patch_update_flow(monkeypatch, repo_pair)

    class ReachedDependencies(Exception):
        pass

    monkeypatch.setattr(hermes_main, "_abort_dependency_sync_if_self_locked", lambda *a, **k: (_ for _ in ()).throw(ReachedDependencies()))
    with pytest.raises(ReachedDependencies):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert (repo_pair / "package-lock.json").read_text() == edited
    assert (repo_pair / "new.txt").read_text() == "new remote feature\n"


def test_changed_local_ref_after_admission_is_not_overwritten(repo_pair, monkeypatch):
    _git(repo_pair, "checkout", "-q", "main")
    _patch_update_flow(monkeypatch, repo_pair)
    late_sha = []

    def simulate_concurrent_commit():
        (repo_pair / "late.txt").write_text("user commit during update\n")
        _git(repo_pair, "add", "late.txt")
        _git(repo_pair, "commit", "-qm", "concurrent user work")
        late_sha.append(_git(repo_pair, "rev-parse", "HEAD").stdout)

    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", simulate_concurrent_commit)
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert _git(repo_pair, "rev-parse", "HEAD").stdout == late_sha[0]
    assert (repo_pair / "late.txt").read_text() == "user commit during update\n"


def test_parked_lockfile_edit_refuses_with_actionable_unblock(repo_pair, monkeypatch, capsys):
    edited = '{"userPinnedResolution":true}\n'
    (repo_pair / "package-lock.json").write_text(edited)
    _git(repo_pair, "add", "package-lock.json")
    _patch_update_flow(monkeypatch, repo_pair)
    before = _git(repo_pair, "rev-parse", "HEAD").stdout
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert (repo_pair / "package-lock.json").read_text() == edited
    assert _git(repo_pair, "rev-parse", "HEAD").stdout == before
    assert _git(repo_pair, "stash", "list").stdout == ""
    out = capsys.readouterr().out
    assert "Lockfile-only edits" in out
    assert "Commit or stash" in out
    assert f"git -C {repo_pair} status" in out


@pytest.mark.parametrize("concurrent_change", ["commit", "tracked_edit", "branch_switch"])
def test_syntax_failure_never_resets_concurrent_user_work(repo_pair, monkeypatch, concurrent_change):
    _git(repo_pair, "checkout", "-q", "main")
    _patch_update_flow(monkeypatch, repo_pair)
    snapshot = {}

    def check_syntax(root):
        if concurrent_change == "commit":
            (root / "late.txt").write_text("late user work\n")
            _git(root, "add", "late.txt")
            _git(root, "commit", "-qm", "user work during validation")
        elif concurrent_change == "tracked_edit":
            (root / "a.txt").write_text("late tracked edit\n")
        else:
            _git(root, "checkout", "-qb", "unrelated")
        snapshot["head"] = _git(root, "rev-parse", "HEAD").stdout
        snapshot["status"] = _git(root, "status", "--porcelain").stdout
        snapshot["files"] = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
        return False, "hermes_cli/config.py", "bad candidate syntax"

    monkeypatch.setattr(update_cmd, "_validate_critical_files_syntax", check_syntax)
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert _git(repo_pair, "rev-parse", "HEAD").stdout == snapshot["head"]
    assert _git(repo_pair, "status", "--porcelain").stdout == snapshot["status"]
    assert {p.name: p.read_bytes() for p in repo_pair.iterdir() if p.is_file()} == snapshot["files"]


def test_bad_fetched_syntax_refuses_before_source_or_service_mutation(repo_pair, monkeypatch):
    origin = repo_pair.parent / "origin"
    critical = origin / "hermes_cli" / "config.py"
    critical.parent.mkdir()
    critical.write_text("def broken(:\n")
    _git(origin, "add", "hermes_cli/config.py")
    _git(origin, "commit", "-qm", "bad upstream syntax")
    _git(repo_pair, "checkout", "-q", "main")
    _patch_update_flow(monkeypatch, repo_pair)
    before = _git(repo_pair, "rev-parse", "HEAD").stdout
    paused = []
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: paused.append(True))
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert paused == []
    assert _git(repo_pair, "rev-parse", "HEAD").stdout == before
    assert not (repo_pair / "b.txt").exists()
    assert not (repo_pair / "hermes_cli").exists()


def test_deleted_critical_file_in_candidate_does_not_block_update(repo_pair, monkeypatch):
    origin = repo_pair.parent / "origin"
    critical = origin / "hermes_constants.py"
    critical.write_text("OLD_SETTING = True\n")
    _git(origin, "add", "hermes_constants.py")
    _git(origin, "commit", "-qm", "old critical module")
    _git(repo_pair, "checkout", "-q", "main")
    _git(repo_pair, "fetch", "-q", "origin", "main")
    _git(repo_pair, "merge", "--ff-only", "origin/main")
    _git(origin, "rm", "hermes_constants.py")
    _git(origin, "commit", "-qm", "remove obsolete critical module")
    _patch_update_flow(monkeypatch, repo_pair)
    class ReachedDependencies(Exception):
        pass
    monkeypatch.setattr(hermes_main, "_abort_dependency_sync_if_self_locked", lambda *a, **k: (_ for _ in ()).throw(ReachedDependencies()))
    with pytest.raises(ReachedDependencies):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert not (repo_pair / "hermes_constants.py").exists()
    assert _git(repo_pair, "rev-parse", "HEAD").stdout == _git(origin, "rev-parse", "HEAD").stdout


def test_candidate_blob_read_failure_is_unknown_not_missing_file(repo_pair, monkeypatch):
    origin = repo_pair.parent / "origin"
    (origin / "hermes_constants.py").write_text("VALID = True\n")
    _git(origin, "add", "hermes_constants.py")
    _git(origin, "commit", "-qm", "valid critical module")
    _git(repo_pair, "checkout", "-q", "main")
    _patch_update_flow(monkeypatch, repo_pair)
    original = _git(repo_pair, "rev-parse", "HEAD").stdout
    real_run = subprocess.run
    def fail_blob(cmd, **kwargs):
        if cmd[1:3] == ["cat-file", "blob"]:
            raise subprocess.CalledProcessError(128, cmd, stderr="object read failure")
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "run", fail_blob)
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: pytest.fail("paused despite unknown candidate"))
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    from hermes_cli import update_receipt
    receipt = json.loads((update_receipt._receipt_dir() / "latest.json").read_text())
    assert receipt["stop_reason"] == "git_unknown"
    assert _git(repo_pair, "rev-parse", "HEAD").stdout == original
    assert not (repo_pair / "hermes_constants.py").exists()


def test_same_sha_branch_switch_before_merge_refuses_without_updating_wrong_branch(repo_pair, monkeypatch):
    _git(repo_pair, "checkout", "-q", "main")
    _patch_update_flow(monkeypatch, repo_pair)
    original = _git(repo_pair, "rev-parse", "HEAD").stdout.strip()
    real_run = subprocess.run
    switched = False

    def concurrent_switch(cmd, **kwargs):
        nonlocal switched
        result = real_run(cmd, **kwargs)
        if cmd[1] == "rev-list" and not switched:
            switched = True
            _git(repo_pair, "checkout", "-qb", "unrelated-work")
        return result

    monkeypatch.setattr(subprocess, "run", concurrent_switch)
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert switched
    assert _git(repo_pair, "rev-parse", "unrelated-work").stdout.strip() == original
    assert _git(repo_pair, "rev-parse", "main").stdout.strip() == original


def test_invalid_update_config_refuses_with_receipt_before_pause(repo_pair, monkeypatch):
    import hermes_cli.config as config
    from hermes_cli import update_receipt

    _patch_update_flow(monkeypatch, repo_pair)
    before = _git(repo_pair, "show-ref", "--heads").stdout
    def unreadable():
        raise ValueError("invalid config")
    monkeypatch.setattr(config, "load_config", unreadable)
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: pytest.fail("paused before admission"))
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    receipt = json.loads((update_receipt._receipt_dir() / "latest.json").read_text())
    assert receipt["outcome"] == "refused"
    assert receipt["stop_reason"] == "git_config_unavailable"
    assert _git(repo_pair, "show-ref", "--heads").stdout == before


def test_in_place_update_does_not_require_unused_main_to_be_fast_forwardable(repo_pair, monkeypatch):
    import hermes_cli.config as config

    _git(repo_pair, "checkout", "-q", "main")
    (repo_pair / "main-only.txt").write_text("unpublished main work\n")
    _git(repo_pair, "add", "main-only.txt")
    _git(repo_pair, "commit", "-qm", "main work")
    main_before = _git(repo_pair, "rev-parse", "main").stdout
    _git(repo_pair, "checkout", "-q", "old-feature")
    (repo_pair / "custom.txt").write_text("custom work\n")
    _git(repo_pair, "add", "custom.txt")
    _git(repo_pair, "commit", "-qm", "custom work")
    custom_before = _git(repo_pair, "rev-parse", "HEAD").stdout.strip()
    _patch_update_flow(monkeypatch, repo_pair)
    monkeypatch.setattr(config, "load_config", lambda: {"updates": {"parked_branch_strategy": "update_in_place"}})
    class ReachedDependencies(Exception):
        pass
    monkeypatch.setattr(hermes_main, "_abort_dependency_sync_if_self_locked", lambda *a, **k: (_ for _ in ()).throw(ReachedDependencies()))
    with pytest.raises(ReachedDependencies):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert _git(repo_pair, "branch", "--show-current").stdout.strip() == "old-feature"
    assert _git(repo_pair, "rev-parse", "main").stdout == main_before
    assert _git(repo_pair, "merge-base", "--is-ancestor", custom_before, "HEAD").returncode == 0
    assert (repo_pair / "custom.txt").read_text() == "custom work\n"
    assert (repo_pair / "b.txt").read_text() == "three\n"


def test_divergent_parked_target_warning_explains_reconciliation_not_checkout_retry(repo_pair, monkeypatch, capsys):
    _git(repo_pair, "checkout", "-q", "main")
    (repo_pair / "local.txt").write_text("local\n")
    _git(repo_pair, "add", "local.txt")
    _git(repo_pair, "commit", "-qm", "local")
    local = _git(repo_pair, "rev-parse", "HEAD").stdout.strip()
    remote = _git(repo_pair, "rev-parse", "origin/main").stdout.strip()
    _git(repo_pair, "checkout", "-q", "old-feature")
    _patch_update_flow(monkeypatch, repo_pair)
    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    out = capsys.readouterr().out
    assert "local commits" in out.lower()
    assert "main" in out and local in out and remote in out
    assert "checkout main && hermes update" not in out


def test_merge_uses_fetched_commit_even_if_remote_tracking_ref_moves(repo_pair, monkeypatch):
    _git(repo_pair, "checkout", "-q", "main")
    _patch_update_flow(monkeypatch, repo_pair)
    fetched_sha = _git(repo_pair, "rev-parse", "origin/main").stdout.strip()

    def simulate_concurrent_fetch():
        # Another fetch may change the mutable remote-tracking ref. The
        # admitted immutable commit must still be the merge input.
        _git(repo_pair, "update-ref", "refs/remotes/origin/main", "HEAD")

    class ReachedDependencies(Exception):
        pass

    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", simulate_concurrent_fetch)
    monkeypatch.setattr(hermes_main, "_abort_dependency_sync_if_self_locked", lambda *a, **k: (_ for _ in ()).throw(ReachedDependencies()))
    with pytest.raises(ReachedDependencies):
        hermes_main.cmd_update(SimpleNamespace(yes=True))
    assert _git(repo_pair, "rev-parse", "HEAD").stdout.strip() == fetched_sha


def test_dirty_tree_blocks_auto_switch(repo_pair):
    """Uncommitted changes on the parked branch → do not touch it."""
    (repo_pair / "a.txt").write_text("local edit\n")
    safe, reason = update_cmd._assess_parked_branch_switch(
        GIT, repo_pair, "old-feature", "main"
    )
    assert safe is False
    assert reason == "dirty"


def test_untracked_file_blocks_auto_switch(repo_pair):
    """Untracked files count as dirty too — they'd ride along on checkout."""
    (repo_pair / "scratch.py").write_text("wip\n")
    safe, reason = update_cmd._assess_parked_branch_switch(
        GIT, repo_pair, "old-feature", "main"
    )
    assert safe is False
    assert reason == "dirty"


def test_unmerged_commits_switch_with_kept_notice(repo_pair):
    """Commits on the parked branch not in origin/main: still safe to switch
    (checkout keeps them on the branch) — reason carries the count so the
    caller prints the loud 'kept' notice. Non-interactive callers (desktop
    update button, gateway /update, cron) depend on this: they cannot
    resolve a skip."""
    (repo_pair / "feature.txt").write_text("unmerged work\n")
    _git(repo_pair, "add", "feature.txt")
    _git(repo_pair, "commit", "-qm", "feature work")

    safe, reason = update_cmd._assess_parked_branch_switch(
        GIT, repo_pair, "old-feature", "main"
    )
    assert safe is True
    assert reason == "unmerged:1"


def test_equivalent_cherry_picked_commit_is_still_safe(repo_pair):
    """A commit whose patch already landed upstream (git cherry '-') does
    not block the switch — only genuinely unmerged '+' commits do."""
    # Cherry-pick origin/main's c2 onto the parked branch: patch-identical.
    _git(repo_pair, "cherry-pick", "origin/main~1")
    safe, reason = update_cmd._assess_parked_branch_switch(
        GIT, repo_pair, "old-feature", "main"
    )
    assert safe is True
    assert reason == ""


def test_config_opt_out_blocks_auto_switch(repo_pair, monkeypatch):
    """updates.auto_switch_parked_branch: false disables auto-switch even
    when the branch is clean and merged."""
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"updates": {"auto_switch_parked_branch": False}},
    )
    safe, reason = update_cmd._assess_parked_branch_switch(
        GIT, repo_pair, "old-feature", "main"
    )
    assert safe is False
    assert reason == "disabled"


def test_missing_origin_ref_is_unverifiable(repo_pair):
    """If origin/<target> can't be resolved, the guard refuses to switch."""
    safe, reason = update_cmd._assess_parked_branch_switch(
        GIT, repo_pair, "old-feature", "no-such-branch"
    )
    assert safe is False
    assert reason == "unverifiable"


# ---------------------------------------------------------------------------
# Skip warning content
# ---------------------------------------------------------------------------

def test_skip_warning_names_branch_behind_count_and_commands(repo_pair, capsys):
    update_cmd._print_parked_branch_skip_warning(
        GIT, repo_pair, "old-feature", "main", "dirty"
    )
    out = capsys.readouterr().out
    assert "CODE UPDATE SKIPPED" in out
    assert "old-feature" in out
    assert "2 commit(s) BEHIND" in out
    assert f"git -C {repo_pair} status" in out
    assert "Commit or stash" in out


def test_skip_warning_dirty_reason(repo_pair, capsys):
    update_cmd._print_parked_branch_skip_warning(
        GIT, repo_pair, "old-feature", "main", "dirty"
    )
    out = capsys.readouterr().out
    assert "uncommitted changes" in out


def test_kept_notice_names_branch_count_and_recovery(capsys):
    update_cmd._print_parked_branch_kept_notice("old-feature", "main", "3")
    out = capsys.readouterr().out
    assert "parked on 'old-feature'" in out
    assert "3 commit(s) not merged into origin/main" in out
    assert "safe on 'old-feature'" in out
    assert "git checkout old-feature" in out
    assert "CODE UPDATE SKIPPED" not in out


# ---------------------------------------------------------------------------
# Summary branch/HEAD visibility
# ---------------------------------------------------------------------------

def test_branch_head_label_reflects_real_checkout(repo_pair):
    label = update_cmd._branch_head_label(GIT, repo_pair)
    short = _git(repo_pair, "rev-parse", "--short", "HEAD").stdout.strip()
    assert label == f"old-feature @ {short}"


def test_branch_head_label_detached(repo_pair):
    _git(repo_pair, "checkout", "-q", "--detach")
    label = update_cmd._branch_head_label(GIT, repo_pair)
    assert label is not None
    assert label.startswith("detached @ ")


def test_branch_head_suffix_empty_on_non_repo(tmp_path):
    assert update_cmd._branch_head_suffix(GIT, tmp_path / "not-a-repo") == ""


def test_print_update_completion_carries_branch_and_sha(
    repo_pair, monkeypatch, capsys
):
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo_pair)
    update_cmd._print_update_completion("✓ Update complete!")
    out = capsys.readouterr().out
    short = _git(repo_pair, "rev-parse", "--short", "HEAD").stdout.strip()
    assert f"✓ Update complete! [old-feature @ {short}]" in out


# ---------------------------------------------------------------------------
# Full update flow: parked branch dirty/unmerged → SKIPPED, no false success
# ---------------------------------------------------------------------------

def _patch_update_flow(monkeypatch, repo, run_real_git=True):
    """Point _cmd_update_impl at the real repo and neuter the long tail.

    Matches the monkeypatch surface of test_update_head_moved_gate.py, but
    keeps REAL subprocess.run so the git plumbing runs against the fixture
    repo (the whole point of these regressions).
    """
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)
    monkeypatch.setattr(hermes_main, "_resolve_update_branch", lambda args: "main")
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(
        hermes_main, "_get_origin_url",
        lambda *a, **k: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(hermes_main, "_is_fork", lambda *a, **k: False)
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda *a, **k: 0)
    monkeypatch.setattr(hermes_main, "_record_bytecode_fingerprint", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", lambda *a, **k: None)
    monkeypatch.setattr(hermes_main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_capture_active_lazy_features", lambda: [])
    monkeypatch.setattr(hermes_main, "_capture_active_tool_dependencies", lambda: [])


def test_update_skips_and_warns_on_dirty_parked_branch(
    repo_pair, monkeypatch, capsys
):
    """Tonight's incident shape: parked branch + dirty tree. The update must
    NOT print '✓ Code updated!', must warn loudly, and must exit non-zero
    with the branch named in the summary."""
    (repo_pair / "a.txt").write_text("local edit\n")
    _patch_update_flow(monkeypatch, repo_pair)
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "CODE UPDATE SKIPPED" in out
    assert "old-feature" in out
    assert "code update SKIPPED" in out
    assert "✓ Code updated!" not in out
    assert "✓ Update complete!" not in out
    # Branch untouched.
    branch = _git(repo_pair, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "old-feature"
    # No autostash was created — the guard fires before any stash.
    stashes = _git(repo_pair, "stash", "list").stdout.strip()
    assert stashes == ""


def test_update_switches_unmerged_parked_branch_with_kept_notice(
    repo_pair, monkeypatch, capsys
):
    """Default strategy ("switch"): clean tree + unmerged commits → the
    update proceeds (non-interactive callers like the desktop update button
    cannot resolve a skip), prints the loud 'kept' notice, ends on main
    fast-forwarded to origin/main, and the commits stay on the parked
    branch untouched."""
    (repo_pair / "feature.txt").write_text("unmerged work\n")
    _git(repo_pair, "add", "feature.txt")
    _git(repo_pair, "commit", "-qm", "feature work")
    feature_sha = _git(repo_pair, "rev-parse", "old-feature").stdout.strip()
    _patch_update_flow(monkeypatch, repo_pair)

    class _StopFlow(Exception):
        pass

    monkeypatch.setattr(
        hermes_main,
        "_abort_dependency_sync_if_self_locked",
        lambda *a, **k: (_ for _ in ()).throw(_StopFlow()),
    )
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    out = capsys.readouterr().out
    assert "1 commit(s) not merged into origin/main" in out
    assert "safe on 'old-feature'" in out
    assert "CODE UPDATE SKIPPED" not in out
    assert "updating it in place" not in out
    # Ends on main, fast-forwarded.
    assert (
        _git(repo_pair, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        == "main"
    )
    head = _git(repo_pair, "rev-parse", "HEAD").stdout.strip()
    remote = _git(repo_pair, "rev-parse", "origin/main").stdout.strip()
    assert head == remote
    # The unmerged commit is still exactly where it was, on the branch.
    assert (
        _git(repo_pair, "rev-parse", "old-feature").stdout.strip()
        == feature_sha
    )


def test_update_updates_unmerged_branch_in_place_when_configured(
    repo_pair, monkeypatch, capsys
):
    """updates.parked_branch_strategy: update_in_place — a maintained custom
    branch (local patches on top of main) is updated in place from
    origin/<target> instead of switched away from. The running code must
    advance (origin/main's files arrive) AND the local commits must survive,
    with the checkout never moving."""
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"updates": {"parked_branch_strategy": "update_in_place"}},
    )
    (repo_pair / "feature.txt").write_text("unmerged work\n")
    _git(repo_pair, "add", "feature.txt")
    _git(repo_pair, "commit", "-qm", "feature work")
    _patch_update_flow(monkeypatch, repo_pair)

    # Stop right after the pull/branch logic, before dependency install.
    class _StopFlow(Exception):
        pass

    monkeypatch.setattr(
        hermes_main,
        "_abort_dependency_sync_if_self_locked",
        lambda *a, **k: (_ for _ in ()).throw(_StopFlow()),
    )
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    out = capsys.readouterr().out
    assert "updating it in place" in out
    assert "CODE UPDATE SKIPPED" not in out
    # The checkout never moved.
    assert (
        _git(repo_pair, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        == "old-feature"
    )
    # origin/main's code actually arrived (b.txt lands with c3)...
    assert (repo_pair / "b.txt").exists()
    assert (repo_pair / "a.txt").read_text() == "two\n"
    # ...and the branch's own commit survived it.
    assert (repo_pair / "feature.txt").read_text() == "unmerged work\n"
    assert "feature work" in _git(repo_pair, "log", "--oneline").stdout


def test_switch_branch_flag_overrides_in_place_strategy(
    repo_pair, monkeypatch, capsys
):
    """--switch-branch overrides updates.parked_branch_strategy:
    update_in_place for one run: the unmerged branch is LEFT ALONE and the
    update runs on the target instead.

    A long-lived feature branch does not want an update-driven merge commit
    in its history (#89507 review). The branch tip must be byte-identical
    afterwards, while the checkout ends up on the updated target.
    """
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"updates": {"parked_branch_strategy": "update_in_place"}},
    )
    (repo_pair / "feature.txt").write_text("unmerged work\n")
    _git(repo_pair, "add", "feature.txt")
    _git(repo_pair, "commit", "-qm", "feature work")
    branch_tip_before = _git(
        repo_pair, "rev-parse", "old-feature"
    ).stdout.strip()
    _patch_update_flow(monkeypatch, repo_pair)

    class _StopFlow(Exception):
        pass

    monkeypatch.setattr(
        hermes_main,
        "_abort_dependency_sync_if_self_locked",
        lambda *a, **k: (_ for _ in ()).throw(_StopFlow()),
    )
    args = SimpleNamespace(
        branch=None, yes=False, force=False, force_venv=False,
        switch_branch=True,
    )

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    out = capsys.readouterr().out
    assert "1 commit(s) not merged into origin/main" in out
    assert "updating it in place" not in out
    assert "CODE UPDATE SKIPPED" not in out
    # Checkout moved to the target and picked up its code...
    assert (
        _git(repo_pair, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        == "main"
    )
    assert (repo_pair / "b.txt").exists()
    # ...and the feature branch was not written to at all.
    assert (
        _git(repo_pair, "rev-parse", "old-feature").stdout.strip()
        == branch_tip_before
    )


def test_unmerged_branch_still_updates_in_place_without_the_flag(
    repo_pair, monkeypatch, capsys
):
    """--switch-branch is opt-in: with the in-place strategy configured and
    no flag, the update stays in place."""
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"updates": {"parked_branch_strategy": "update_in_place"}},
    )
    (repo_pair / "feature.txt").write_text("unmerged work\n")
    _git(repo_pair, "add", "feature.txt")
    _git(repo_pair, "commit", "-qm", "feature work")
    _patch_update_flow(monkeypatch, repo_pair)

    class _StopFlow(Exception):
        pass

    monkeypatch.setattr(
        hermes_main,
        "_abort_dependency_sync_if_self_locked",
        lambda *a, **k: (_ for _ in ()).throw(_StopFlow()),
    )
    args = SimpleNamespace(
        branch=None, yes=False, force=False, force_venv=False,
        switch_branch=False,
    )

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    out = capsys.readouterr().out
    assert "updating it in place" in out
    assert "--switch-branch" not in out
    assert (
        _git(repo_pair, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        == "old-feature"
    )


def test_update_auto_switches_clean_merged_parked_branch(
    repo_pair, monkeypatch, capsys
):
    """Clean + fully merged parked branch → auto-switch back to main, pull,
    say so, and STAY on main afterwards (sabotage-proven: reverting the
    guard re-parks the checkout and this test fails on the branch assert)."""
    _patch_update_flow(monkeypatch, repo_pair)
    # Stop the flow right after the pull/branch logic: the dependency
    # install phase begins with _abort_dependency_sync_if_self_locked.
    class _StopFlow(Exception):
        pass

    monkeypatch.setattr(
        hermes_main,
        "_abort_dependency_sync_if_self_locked",
        lambda *a, **k: (_ for _ in ()).throw(_StopFlow()),
    )
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    out = capsys.readouterr().out
    assert "parked on 'old-feature'" in out
    assert "fully merged" in out
    assert "switching back to main" in out
    assert "CODE UPDATE SKIPPED" not in out
    # The checkout ends up ON main, fast-forwarded to origin/main.
    assert (
        _git(repo_pair, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        == "main"
    )
    head = _git(repo_pair, "rev-parse", "HEAD").stdout.strip()
    remote = _git(repo_pair, "rev-parse", "origin/main").stdout.strip()
    assert head == remote


def test_update_up_to_date_path_does_not_repark_merged_branch(
    tmp_path, monkeypatch, capsys
):
    """commit_count == 0 path: before this fix, the updater switched BACK to
    the parked feature branch after checking main ("Restore stash and switch
    back to original branch") — silently re-parking the checkout so every
    subsequent update repeated the incident. A clean, fully merged parked
    branch must now END on main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    (origin / "a.txt").write_text("one\n")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-qm", "c1")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "checkout", "-qb", "old-feature")
    # No new upstream commits: local main == origin/main == old-feature tip.

    _patch_update_flow(monkeypatch, clone)

    class _StopFlow(Exception):
        pass

    import hermes_cli.managed_uv as managed_uv

    monkeypatch.setattr(
        managed_uv,
        "update_managed_uv",
        lambda *a, **k: (_ for _ in ()).throw(_StopFlow()),
    )
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    out = capsys.readouterr().out
    assert "switched back to main" in out
    # The regression: old code ran `git checkout old-feature` here.
    assert (
        _git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    )


def test_update_on_main_fast_path_unchanged(repo_pair, monkeypatch, capsys):
    """On the target branch already: no guard prints, normal pull flow."""
    _git(repo_pair, "checkout", "-q", "main")

    _patch_update_flow(monkeypatch, repo_pair)

    class _StopFlow(Exception):
        pass

    monkeypatch.setattr(
        hermes_main,
        "_abort_dependency_sync_if_self_locked",
        lambda *a, **k: (_ for _ in ()).throw(_StopFlow()),
    )
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)

    with pytest.raises(_StopFlow):
        hermes_main.cmd_update(args)

    out = capsys.readouterr().out
    assert "parked on" not in out
    assert "CODE UPDATE SKIPPED" not in out
    assert (
        _git(repo_pair, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        == "main"
    )
    head = _git(repo_pair, "rev-parse", "HEAD").stdout.strip()
    remote = _git(repo_pair, "rev-parse", "origin/main").stdout.strip()
    assert head == remote
