from pathlib import Path
from subprocess import CalledProcessError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import config as hermes_config
from hermes_cli import main as hermes_main


# ---------------------------------------------------------------------------
# Managed-uv compatibility for tests that patch shutil.which
# ---------------------------------------------------------------------------
# The production code now uses ``ensure_uv()`` / ``update_managed_uv()``
# instead of ``shutil.which("uv")``.  Many tests in this file patch
# ``shutil.which`` to control whether uv is "available" — these autouse
# fixtures make the managed_uv functions delegate to the patched
# ``shutil.which`` so the existing test setup keeps working without
# per-test changes.
@pytest.fixture(autouse=True)
def _patch_managed_uv(request):
    """Make managed_uv helpers follow shutil.which mocking in tests."""
    import shutil

    # resolve_uv delegates to shutil.which("uv") so that test patches
    # on shutil.which flow through naturally.
    def _fake_resolve_uv(**kwargs):
        return shutil.which("uv")

    def _fake_ensure_uv(**kwargs):
        return shutil.which("uv")

    def _fake_update_managed_uv(**kwargs):
        return None  # never actually self-update in tests

    with patch("hermes_cli.managed_uv.resolve_uv", side_effect=_fake_resolve_uv), \
         patch("hermes_cli.managed_uv.ensure_uv", side_effect=_fake_ensure_uv), \
         patch("hermes_cli.managed_uv.update_managed_uv", side_effect=_fake_update_managed_uv):
        yield













# ---------------------------------------------------------------------------
# Update uses .[all] with fallback to .
# ---------------------------------------------------------------------------

def _setup_update_mocks(monkeypatch, tmp_path):
    """Common setup for cmd_update tests."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hermes_main, "_stash_local_changes_if_needed", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_restore_stashed_changes", lambda *a, **kw: True)
    monkeypatch.setattr(hermes_config, "get_missing_env_vars", lambda required_only=True: [])
    monkeypatch.setattr(hermes_config, "get_missing_config_fields", lambda: [])
    monkeypatch.setattr(hermes_config, "check_config_version", lambda: (5, 5))
    monkeypatch.setattr(hermes_config, "migrate_config", lambda **kw: {"env_added": [], "config_added": []})
    monkeypatch.setattr(hermes_main, "_upgrade_pip_before_lazy_refresh", lambda *a, **kw: None)
    monkeypatch.setattr(hermes_main, "_refresh_active_lazy_features", lambda *a, **kw: True)




def test_refresh_active_memory_provider_dependencies_reinstalls_active_provider(monkeypatch):
    """#53272/#70636: update must re-run the active provider's dep install."""
    recorded = []

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "mem0"}},
    )
    monkeypatch.setattr(
        "hermes_cli.memory_setup._install_dependencies",
        lambda provider_name, force=False: recorded.append((provider_name, force)),
    )

    hermes_main._refresh_active_memory_provider_dependencies()

    assert recorded == [("mem0", True)]




def test_reload_updated_runtime_modules_restores_new_hermes_constants_symbol(monkeypatch):
    """A pre-pull module object missing a new helper is repaired by reload."""
    import hermes_constants

    monkeypatch.delattr(hermes_constants, "apply_subprocess_home_env", raising=False)
    assert not hasattr(hermes_constants, "apply_subprocess_home_env")

    hermes_main._reload_updated_runtime_modules()

    assert callable(hermes_constants.apply_subprocess_home_env)


def test_restore_keeps_stash_when_durable_ref_cannot_be_created(
    monkeypatch, tmp_path, capsys
):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[1:3] == ["stash", "apply"]:
            return SimpleNamespace(stdout="applied\n", stderr="", returncode=0)
        if cmd[1:3] == ["diff", "--name-only"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd[1] == "update-ref":
            return SimpleNamespace(stdout="", stderr="ref write failed\n", returncode=1)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    restored = hermes_main._restore_stashed_changes(
        ["git"], tmp_path, "abcdef1", prompt_user=False
    )

    assert restored is True
    assert [call[0][1] for call in calls] == ["stash", "diff", "update-ref"]
    out = capsys.readouterr().out
    assert "durable recovery ref" in out
    assert "git update-ref refs/hermes/autostash/abcdef1 abcdef1" in out
    assert "stash was left in place" in out


def test_restore_stashed_changes_always_resets_on_conflict(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[1:3] == ["stash", "apply"]:
            return SimpleNamespace(stdout="conflict output\n", stderr="conflict stderr\n", returncode=1)
        if cmd[1:3] == ["diff", "--name-only"]:
            return SimpleNamespace(stdout="hermes_cli/main.py\n", stderr="", returncode=0)
        if cmd[1:3] == ["reset", "--hard"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda: "y")

    result = hermes_main._restore_stashed_changes(
        ["git"], tmp_path, "abc123", prompt_user=True
    )

    assert result is False
    out = capsys.readouterr().out
    assert "Conflicted files:" in out
    assert "hermes_cli/main.py" in out
    assert "stashed changes are preserved" in out
    assert "Working tree reset to clean state" in out
    assert sum(c[1:3] == ["reset", "--hard"] for c, _ in calls) == 1


def test_restore_stashed_changes_auto_resets_non_interactive(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[1:3] == ["stash", "apply"]:
            return SimpleNamespace(stdout="applied\n", stderr="", returncode=0)
        if cmd[1:3] == ["diff", "--name-only"]:
            return SimpleNamespace(stdout="cli.py\n", stderr="", returncode=0)
        if cmd[1:3] == ["reset", "--hard"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    result = hermes_main._restore_stashed_changes(
        ["git"], tmp_path, "abc123", prompt_user=False
    )

    assert result is False
    assert "Working tree reset to clean state" in capsys.readouterr().out
    assert sum(c[1:3] == ["reset", "--hard"] for c, _ in calls) == 1


def test_stash_local_changes_if_needed_raises_when_stash_ref_missing(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[-2:] == ["status", "--porcelain"]:
            return SimpleNamespace(stdout=" M hermes_cli/main.py\n", returncode=0)
        if cmd[-2:] == ["ls-files", "--unmerged"]:
            return SimpleNamespace(stdout="", returncode=0)
        if cmd[1:4] == ["stash", "push", "--include-untracked"]:
            return SimpleNamespace(stdout="Saved working directory\n", returncode=0)
        if cmd[-3:] == ["rev-parse", "--verify", "refs/stash"]:
            raise CalledProcessError(returncode=128, cmd=cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)

    with pytest.raises(CalledProcessError):
        hermes_main._stash_local_changes_if_needed(["git"], Path(tmp_path))


def test_fetch_is_scoped_to_target_branch(monkeypatch, tmp_path):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[-2:] == ["status", "--porcelain"]:
            return SimpleNamespace(stdout="", returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)
    hermes_main._stash_local_changes_if_needed(["git"], Path(tmp_path))
    assert commands == [["git", "status", "--porcelain"]]


def test_cmd_update_falls_back_to_reset_when_ff_only_fails(monkeypatch, tmp_path, capsys):
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    side_effect, recorded = _make_update_side_effect(ff_only_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)
    hermes_main.cmd_update(SimpleNamespace())
    reset_calls = [c for c in recorded if "reset" in c and "--hard" in c]
    assert reset_calls == [["git", "reset", "--hard", "origin/main"]]
    assert "Fast-forward not possible" in capsys.readouterr().out


def test_cmd_update_switches_to_main_from_detached_head(monkeypatch, tmp_path, capsys):
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    side_effect, recorded = _make_update_side_effect(current_branch="HEAD")
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)
    hermes_main.cmd_update(SimpleNamespace())
    assert [c for c in recorded if "checkout" in c and "main" in c]
    assert "detached HEAD" in capsys.readouterr().out


def test_cmd_update_fetch_is_scoped_to_target_branch(monkeypatch, tmp_path):
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    side_effect, recorded = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)
    hermes_main.cmd_update(SimpleNamespace())
    assert [c for c in recorded if "fetch" in c] == [["git", "fetch", "origin", "main"]]


def test_restore_stashed_changes_keeps_stash_when_durable_ref_cannot_be_created_against_main(
    monkeypatch, tmp_path
):
    """The exact-SHA recovery path remains reachable through main's canonical helper."""
    monkeypatch.setattr(
        hermes_main,
        "_preserve_stash_commit",
        lambda *args, **kwargs: False,
    )

    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ["stash", "apply"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd[1:3] == ["diff", "--name-only"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)
    assert hermes_main._restore_stashed_changes(
        ["git"], tmp_path, "abcdef1", prompt_user=False
    ) is True


def test_restore_stashed_changes_success_preserves_exact_sha_before_drop(
    monkeypatch, tmp_path
):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["stash", "apply"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd[1:3] == ["diff", "--name-only"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd[1] == "update-ref":
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if cmd[1:3] == ["stash", "list"]:
            return SimpleNamespace(stdout="stash@{1} abc1234\n", stderr="", returncode=0)
        if cmd[1:3] == ["stash", "drop"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(hermes_main.subprocess, "run", fake_run)
    assert hermes_main._restore_stashed_changes(
        ["git"], tmp_path, "abc1234", prompt_user=False
    ) is True
    assert [cmd[1] for cmd in calls] == ["stash", "diff", "update-ref", "stash", "stash"]
    assert calls[2] == ["git", "update-ref", "refs/hermes/autostash/abc1234", "abc1234"]






# ---------------------------------------------------------------------------
# ff-only fallback to reset --hard on diverged history
# ---------------------------------------------------------------------------

def _make_update_side_effect(
    current_branch="main",
    commit_count="3",
    ff_only_fails=False,
    reset_fails=False,
    fetch_fails=False,
    fetch_stderr="",
):
    """Build a subprocess.run side_effect for cmd_update tests."""
    recorded = []

    def side_effect(cmd, **kwargs):
        recorded.append(cmd)
        joined = " ".join(str(c) for c in cmd)
        if "fetch" in joined and "origin" in joined:
            if fetch_fails:
                return SimpleNamespace(stdout="", stderr=fetch_stderr, returncode=128)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(stdout=f"{current_branch}\n", stderr="", returncode=0)
        if "checkout" in joined and "main" in joined:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if "rev-list" in joined:
            return SimpleNamespace(stdout=f"{commit_count}\n", stderr="", returncode=0)
        if "--ff-only" in joined:
            if ff_only_fails:
                return SimpleNamespace(
                    stdout="",
                    stderr="fatal: Not possible to fast-forward, aborting.\n",
                    returncode=128,
                )
            return SimpleNamespace(stdout="Updating abc..def\n", stderr="", returncode=0)
        if "reset" in joined and "--hard" in joined:
            if reset_fails:
                return SimpleNamespace(stdout="", stderr="error: unable to write\n", returncode=1)
            return SimpleNamespace(stdout="HEAD is now at abc123\n", stderr="", returncode=0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect, recorded


# ---------------------------------------------------------------------------
# Non-main branch → auto-checkout main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fetch failure — friendly error messages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reset --hard failure — don't attempt stash restore
# ---------------------------------------------------------------------------

def test_cmd_update_skips_stash_restore_when_reset_fails(monkeypatch, tmp_path, capsys):
    """When reset --hard fails, stash restore is skipped with a helpful message."""
    _setup_update_mocks(monkeypatch, tmp_path)
    # Re-enable stash so it actually returns a ref
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )

    side_effect, _ = _make_update_side_effect(ff_only_fails=True, reset_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace())

    # Stash restore should NOT have been called
    assert len(restore_calls) == 0

    out = capsys.readouterr().out
    assert "preserved in stash" in out


# ---------------------------------------------------------------------------
# Non-interactive update.non_interactive_local_changes setting
# (chat app / gateway): "discard" throws stashed changes away, "stash"
# (default) restores them. Interactive terminal updates ignore the setting
# and always go through the restore path.
# ---------------------------------------------------------------------------

def _setup_setting_test(monkeypatch, tmp_path, mode):
    """Common wiring: real stash returns a ref, restore + discard are
    recorded, and load_config reports the given non_interactive_local_changes
    mode."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    discard_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_discard_stashed_changes",
        lambda *a, **kw: discard_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda *a, **kw: {"updates": {"non_interactive_local_changes": mode}},
    )
    side_effect, recorded = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)
    return restore_calls, discard_calls, recorded


# ---------------------------------------------------------------------------
# --keep-stash (desktop updater): stash for the update, never re-apply.
# ---------------------------------------------------------------------------

def _setup_keep_stash_test(monkeypatch, tmp_path):
    """Wiring for --keep-stash tests: stash returns a ref; restore, discard,
    and park are all recorded."""
    _setup_update_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed",
        lambda *a, **kw: "abc123deadbeef",
    )
    restore_calls = []
    discard_calls = []
    park_calls = []
    monkeypatch.setattr(
        hermes_main, "_restore_stashed_changes",
        lambda *a, **kw: restore_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_discard_stashed_changes",
        lambda *a, **kw: discard_calls.append(1) or True,
    )
    monkeypatch.setattr(
        hermes_main, "_park_stashed_changes",
        lambda *a, **kw: park_calls.append(a) or None,
    )
    # Keep the update flow away from the real gateway fleet on this machine —
    # a live gateway PID would trip the test-suite kill guard and turn the
    # run into exit 1 (gateway_fleet_restart_incomplete).
    monkeypatch.setattr(
        "hermes_cli.gateway.find_gateway_pids", lambda **kw: [], raising=False
    )
    return restore_calls, discard_calls, park_calls


def test_update_keep_stash_parks_instead_of_restoring(monkeypatch, tmp_path):
    """--keep-stash: after a successful update, the autostash is parked (left
    in git stash) — never re-applied, never discarded."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=True))

    assert len(park_calls) == 1
    assert park_calls[0][0] == "abc123deadbeef"
    assert restore_calls == []
    assert discard_calls == []


def test_update_without_keep_stash_still_restores(monkeypatch, tmp_path):
    """Regression guard: default behavior (no --keep-stash) is unchanged —
    the autostash is auto-restored under --yes."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect()
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=False))

    assert restore_calls == [1]
    assert park_calls == []
    assert discard_calls == []


def test_update_keep_stash_failure_path_still_preserves(monkeypatch, tmp_path, capsys):
    """--keep-stash + failed update: neither restore nor park runs; the
    existing preserved-in-stash message fires (working tree unknown)."""
    restore_calls, discard_calls, park_calls = _setup_keep_stash_test(monkeypatch, tmp_path)
    side_effect, _ = _make_update_side_effect(ff_only_fails=True, reset_fails=True)
    monkeypatch.setattr(hermes_main.subprocess, "run", side_effect)

    with pytest.raises(SystemExit, match="1"):
        hermes_main.cmd_update(SimpleNamespace(yes=True, keep_stash=True))

    assert restore_calls == []
    assert park_calls == []
    assert discard_calls == []
    assert "preserved in stash" in capsys.readouterr().out


def test_update_parser_accepts_keep_stash():
    """The flag parses and defaults off."""
    import argparse

    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_update_parser(subparsers, cmd_update=lambda args: None)

    args = parser.parse_args(["update", "--keep-stash"])
    assert args.keep_stash is True
    args = parser.parse_args(["update"])
    assert args.keep_stash is False






def test_bootstrap_marker_not_autostashed_by_update(tmp_path):
    """#38529: the Desktop bootstrap marker must be git-ignored so that
    ``hermes update``'s ``git stash push --include-untracked`` does not sweep it
    into an autostash on every run.

    Behavioral + hermetic: build a throwaway repo that adopts the project's real
    ``.gitignore`` (the contract under test), drop the marker, and confirm the
    same stash invocation the updater uses leaves it untouched.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo_gitignore = Path(hermes_main.__file__).resolve().parents[1] / ".gitignore"

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(repo_gitignore.read_text())
    (tmp_path / "tracked.txt").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    marker = tmp_path / ".hermes-bootstrap-complete"
    marker.write_text("")

    # Exact flags used by hermes update (hermes_cli/main.py).
    git("stash", "push", "--include-untracked", "-m", "hermes-update-autostash")

    assert marker.exists(), (
        ".hermes-bootstrap-complete was swept into the update autostash — it must "
        "be listed in .gitignore so `git stash -u` skips it (#38529)."
    )
    # It must not even register as a dirty/untracked change.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert ".hermes-bootstrap-complete" not in status


# ---------------------------------------------------------------------------
# Permission-denied autostash class: undeletable untracked files (root-owned
# packaging/ etc.) must not abort the update when the stash entry was created.
# ---------------------------------------------------------------------------






def test_update_autostash_survives_undeletable_untracked_dir(tmp_path):
    """Behavioral E2E of the whole permission-denied class with real git:
    root-owned-style undeletable untracked dir → stash succeeds, update-style
    reset works, restore round-trips, nothing lost. (#70127 follow-up)"""
    import os
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")
    if os.name == "nt":
        pytest.skip("POSIX permission semantics")
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write bits")

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "tracked.txt").write_text("v1\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    (tmp_path / "tracked.txt").write_text("v2 local change\n")
    pkg = tmp_path / "packaging" / "homebrew"
    pkg.mkdir(parents=True)
    (pkg / "hermes-agent.rb").write_text("formula\n")
    os.chmod(pkg, 0o555)  # undeletable contents, like a root-owned dir
    try:
        stash_ref = hermes_main._stash_local_changes_if_needed(["git"], tmp_path)
        assert stash_ref

        # The tracked change is stashed; simulate the updater's checkout window.
        assert (tmp_path / "tracked.txt").read_text() == "v1\n"

        restored = hermes_main._restore_stashed_changes(
            ["git"], tmp_path, stash_ref, prompt_user=False
        )
        assert restored is True
        assert (tmp_path / "tracked.txt").read_text() == "v2 local change\n"
        assert (pkg / "hermes-agent.rb").read_text() == "formula\n"
    finally:
        os.chmod(pkg, 0o755)
