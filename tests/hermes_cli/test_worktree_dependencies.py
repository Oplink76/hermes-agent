"""Dispatcher-side Node dependency provisioning for Kanban worktrees."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import worktree_dependencies as wd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo_with_node_dependencies(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    verify_script = (
        "node -e \"if (!require('./node_modules/fixture-dependency')) "
        "process.exit(1)\""
    )
    package = {
        "name": "fixture",
        "version": "1.0.0",
        "scripts": {
            "build": verify_script,
            "test": verify_script,
        },
    }
    (repo / "package.json").write_text(
        json.dumps(package, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    (repo / "package-lock.json").write_bytes(
        b'{"name":"fixture","version":"1.0.0","lockfileVersion":3,'
        b'"packages":{"":{"name":"fixture","version":"1.0.0"}}}\n'
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    _git(
        repo,
        "add",
        ".gitignore",
        "package.json",
        "package-lock.json",
        "README.md",
    )
    _git(repo, "commit", "-m", "initial project")
    node_modules = repo / "node_modules" / "fixture-dependency"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text(
        "module.exports = 'from primary';\n", encoding="utf-8"
    )
    return repo


def _task_for(repo: Path, conn, *, branch: str = "wt/fixture") -> tuple[str, Path]:
    target = repo / ".worktrees" / branch.replace("/", "-")
    task_id = kb.create_task(
        conn,
        title="Node worktree",
        workspace_kind="worktree",
        workspace_path=str(target),
        branch_name=branch,
    )
    return task_id, target


def test_matching_manifests_copy_isolated_node_modules(kanban_home, tmp_path):
    repo = _repo_with_node_dependencies(tmp_path)
    with kb.connect() as conn:
        task_id, target = _task_for(repo, conn)
        task = kb.get_task(conn, task_id)
        assert task is not None
        workspace, _branch = kb._resolve_worktree_workspace(task, conn=conn)

    copied = workspace / "node_modules" / "fixture-dependency" / "index.js"
    source = repo / "node_modules" / "fixture-dependency" / "index.js"
    assert copied.read_text(encoding="utf-8") == "module.exports = 'from primary';\n"
    assert os.stat(copied).st_ino != os.stat(source).st_ino
    for script in ("build", "test"):
        result = subprocess.run(
            ["npm", "run", script],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout
    copied.write_text(
        "module.exports = 'changed in worktree';\n", encoding="utf-8"
    )
    assert source.read_text(encoding="utf-8") == "module.exports = 'from primary';\n"
    assert target == workspace


def test_trailing_slash_node_modules_ignore_is_accepted(kanban_home, tmp_path):
    """A conventional directory-only ignore must not block provisioning."""
    repo = _repo_with_node_dependencies(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "Use conventional directory ignore")

    with kb.connect() as conn:
        task_id, _target = _task_for(repo, conn)
        task = kb.get_task(conn, task_id)
        workspace, _branch = kb._resolve_worktree_workspace(task, conn=conn)

    assert (workspace / "node_modules" / "fixture-dependency" / "index.js").is_file()


def test_stale_primary_dependency_is_installed_instead_of_certified(
    kanban_home, tmp_path, monkeypatch
):
    """Matching manifests do not prove the primary installed version."""
    repo = _repo_with_node_dependencies(tmp_path)
    package = json.loads((repo / "package.json").read_text())
    package["dependencies"] = {"fixture-dependency": "2.0.0"}
    lock = json.loads((repo / "package-lock.json").read_text())
    lock["packages"][""]["dependencies"] = package["dependencies"]
    lock["packages"]["node_modules/fixture-dependency"] = {"version": "2.0.0"}
    (repo / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (repo / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    _git(repo, "add", "package.json", "package-lock.json")
    _git(repo, "commit", "-m", "Require dependency version two")
    installed = repo / "node_modules" / "fixture-dependency"
    (installed / "package.json").write_text(
        '{"name":"fixture-dependency","version":"1.0.0"}\n', encoding="utf-8"
    )
    install_calls = []

    def install(target):
        install_calls.append(target)
        dependency = target / "node_modules" / "fixture-dependency"
        dependency.mkdir(parents=True)
        (dependency / "package.json").write_text(
            '{"name":"fixture-dependency","version":"2.0.0"}\n', encoding="utf-8"
        )

    monkeypatch.setattr(wd, "_run_real_install", install)
    with kb.connect() as conn:
        task_id, _target = _task_for(repo, conn)
        task = kb.get_task(conn, task_id)
        workspace, _branch = kb._resolve_worktree_workspace(task, conn=conn)

    assert install_calls == [workspace]
    installed_version = json.loads(
        (workspace / "node_modules" / "fixture-dependency" / "package.json").read_text()
    )["version"]
    assert installed_version == "2.0.0"
    assert not (workspace / "node_modules" / "fixture-dependency" / "index.js").exists()


def test_manifest_mismatch_runs_real_install_in_dispatcher_context(
    kanban_home, tmp_path, monkeypatch
):
    repo = _repo_with_node_dependencies(tmp_path)
    _git(repo, "switch", "-c", "feature")
    (repo / "package.json").write_bytes(b'{"name":"fixture","version":"2.0.0"}\n')
    feature_lock = {
        "name": "fixture",
        "version": "2.0.0",
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "fixture", "version": "2.0.0"},
            "node_modules/fsevents": {
                "version": "2.3.3",
                "dev": True,
                "optional": True,
                "os": ["darwin"],
                "hasInstallScript": True,
            },
        },
    }
    (repo / "package-lock.json").write_text(
        json.dumps(feature_lock, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "package.json", "package-lock.json")
    _git(repo, "commit", "-m", "feature manifest")
    _git(repo, "switch", "main")

    real_which = wd.shutil.which
    real_run = wd.subprocess.run
    install_calls: list[tuple[list[str], dict]] = []

    def fake_which(name: str, *args, **kwargs):
        if name == "npm":
            return "/dispatcher/npm"
        return real_which(name, *args, **kwargs)

    def fake_run(command, *args, **kwargs):
        if Path(command[0]).name == "npm":
            install_calls.append((list(command), dict(kwargs)))
            node_modules = Path(kwargs["cwd"]) / "node_modules"
            node_modules.mkdir(parents=True, exist_ok=True)
            (node_modules / "installed-by-dispatcher").write_text(
                "yes\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(wd.shutil, "which", fake_which)
    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "worker-must-not-install")

    with kb.connect() as conn:
        task_id, target = _task_for(repo, conn, branch="feature")
        task = kb.get_task(conn, task_id)
        assert task is not None
        workspace, _branch = kb._resolve_worktree_workspace(task, conn=conn)

    assert len(install_calls) == 1
    command, kwargs = install_calls[0]
    assert command[1:3] == ["ci", "--include=dev"]
    assert "--ignore-scripts" in command
    assert "install" not in command[1:]
    assert "HERMES_KANBAN_TASK" not in kwargs["env"]
    assert (workspace / "node_modules" / "installed-by-dispatcher").is_file()
    assert not (
        workspace / "node_modules" / "fixture-dependency" / "index.js"
    ).exists()
    assert target == workspace


@pytest.mark.parametrize(
    ("package_path", "overrides"),
    [
        ("node_modules/other", {}),
        ("node_modules/fsevents", {"version": "2.3.2"}),
        ("node_modules/fsevents", {"dev": False}),
        ("node_modules/fsevents", {"optional": False}),
        ("node_modules/fsevents", {"os": ["linux"]}),
    ],
)
def test_real_fallback_refuses_other_dependency_install_scripts(
    tmp_path, package_path, overrides
):
    primary = tmp_path / "primary"
    target = tmp_path / "target"
    primary.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    package = {"name": "fixture", "version": "1.0.0"}
    scripted_dependency = {
        "version": "2.3.3",
        "dev": True,
        "optional": True,
        "os": ["darwin"],
        "hasInstallScript": True,
        **overrides,
    }
    lock = {
        "name": "fixture",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "packages": {
            "": package,
            package_path: scripted_dependency,
        },
    }
    for project in (primary, target):
        (project / "package.json").write_text(
            json.dumps(package) + "\n", encoding="utf-8"
        )
        (project / "package-lock.json").write_text(
            json.dumps(lock) + "\n", encoding="utf-8"
        )
    (target / ".gitignore").write_text("node_modules\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="require lifecycle scripts"):
        wd.provision_node_dependencies(primary, target)

    assert not (target / "node_modules").exists()


def test_worktree_cleanup_removes_provisioned_dependencies(kanban_home, tmp_path):
    repo = _repo_with_node_dependencies(tmp_path)
    with kb.connect() as conn:
        task_id, _target = _task_for(repo, conn)
        task = kb.get_task(conn, task_id)
        assert task is not None
        workspace, _branch = kb._resolve_worktree_workspace(task, conn=conn)
        assert (workspace / "node_modules").is_dir()

        assert kb.complete_task(conn, task_id, result="done") is True

    assert workspace.is_dir()
    assert not (workspace / "node_modules").exists()


def test_running_task_blocks_shared_worktree_reprovisioning(kanban_home, tmp_path):
    repo = _repo_with_node_dependencies(tmp_path)
    with kb.connect() as conn:
        first_id, target = _task_for(repo, conn)
        first = kb.get_task(conn, first_id)
        assert first is not None
        workspace, _branch = kb._resolve_worktree_workspace(first, conn=conn)
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (first_id,))
        conn.commit()
        second_id = kb.create_task(
            conn,
            title="Second Node worktree consumer",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name="wt/fixture",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (second_id,))
        conn.commit()
        second = kb.get_task(conn, second_id)
        assert second is not None

        with pytest.raises(RuntimeError, match="another running task"):
            kb._resolve_worktree_workspace(second, conn=conn)

    assert (workspace / "node_modules").is_dir()


def test_completion_keeps_dependencies_for_other_running_consumer(
    kanban_home, tmp_path
):
    repo = _repo_with_node_dependencies(tmp_path)
    with kb.connect() as conn:
        first_id, target = _task_for(repo, conn)
        first = kb.get_task(conn, first_id)
        assert first is not None
        workspace, _branch = kb._resolve_worktree_workspace(first, conn=conn)
        second_id = kb.create_task(
            conn,
            title="Second Node worktree consumer",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name="wt/fixture",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (second_id,))
        conn.commit()

        assert kb.complete_task(conn, first_id, result="done") is True

    assert (workspace / "node_modules").is_dir()


def test_active_provisioning_lock_prevents_concurrent_replacement(tmp_path):
    repo = _repo_with_node_dependencies(tmp_path)
    target = tmp_path / "target"
    subprocess.run(
        ["git", "init", "-b", "main", str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    for name in ("package.json", "package-lock.json"):
        (target / name).write_bytes((repo / name).read_bytes())
    (target / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    existing = target / "node_modules" / "keep"
    existing.mkdir(parents=True)
    (existing.parent / wd._PROVISION_MARKER).write_text(
        json.dumps(wd._manifest_state(target), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lock = wd._acquire_project_lock(target)
    try:
        with pytest.raises(RuntimeError, match="already active"):
            wd.provision_node_dependencies(repo, target)

        assert existing.is_dir()
        wd.cleanup_provisioned_node_dependencies(target)
        assert existing.is_dir()
    finally:
        lock.release()


def test_materialize_does_not_clean_another_provisioner(tmp_path):
    repo = _repo_with_node_dependencies(tmp_path)
    target = repo / ".worktrees" / "existing"
    assert kb._ensure_git_worktree(repo, target, "wt/existing") is True
    existing = target / "node_modules" / "keep"
    existing.mkdir(parents=True)
    lock = wd._acquire_project_lock(target)
    try:
        with pytest.raises(RuntimeError, match="already active"):
            kb._materialize_worktree_with_dependencies(
                repo, target, "wt/existing", base="HEAD"
            )

        assert existing.is_dir()
    finally:
        lock.release()


def test_failed_new_worktree_provisioning_removes_linked_checkout(tmp_path, monkeypatch):
    repo = _repo_with_node_dependencies(tmp_path)
    target = repo / ".worktrees" / "failed"
    monkeypatch.setattr(
        kb,
        "_provision_node_dependencies",
        lambda _primary, _target: (_ for _ in ()).throw(RuntimeError("install failed")),
    )

    with pytest.raises(RuntimeError, match="install failed"):
        kb._materialize_worktree_with_dependencies(
            repo, target, "wt/failed", base="HEAD"
        )

    assert not target.exists()
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(target) not in listed


@pytest.mark.skipif(wd.shutil.which("npm") is None, reason="npm unavailable")
@pytest.mark.parametrize("lifecycle_trigger", ["declared", "binding-gyp"])
def test_real_fallback_refuses_project_lifecycle_scripts(
    tmp_path, lifecycle_trigger
):
    primary = tmp_path / "primary"
    target = tmp_path / "target"
    primary.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    package = {"name": "fixture", "version": "1.0.0"}
    if lifecycle_trigger == "declared":
        package["scripts"] = {
            "preinstall": "node -e \"require('fs').writeFileSync('RAN', 'yes')\""
        }
    for project in (primary, target):
        (project / "package.json").write_text(
            json.dumps(package) + "\n", encoding="utf-8"
        )
        if lifecycle_trigger == "binding-gyp":
            (project / "binding.gyp").write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ["npm", "install", "--package-lock-only", "--ignore-scripts"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        )
    (target / ".gitignore").write_text("node_modules\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="require lifecycle scripts"):
        wd.provision_node_dependencies(primary, target)

    assert not (target / "RAN").exists()
    assert not (target / "node_modules").exists()


def test_unsupported_package_manager_fails_instead_of_claiming_ready(tmp_path):
    primary = tmp_path / "primary"
    target = tmp_path / "target"
    for project in (primary, target):
        project.mkdir()
        (project / "package.json").write_text(
            '{"name":"fixture","packageManager":"pnpm@10"}\n',
            encoding="utf-8",
        )
        (project / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")

    with pytest.raises(RuntimeError, match="supports npm lockfiles only"):
        wd.provision_node_dependencies(primary, target)

    assert not (target / "node_modules").exists()


def test_root_and_independent_nested_npm_projects_are_both_provisioned(tmp_path):
    primary = _repo_with_node_dependencies(tmp_path)
    nested_primary = primary / "website"
    nested_primary.mkdir()
    (nested_primary / "package.json").write_text(
        '{"name":"website","version":"1.0.0"}\n', encoding="utf-8"
    )
    (nested_primary / "package-lock.json").write_text(
        '{"name":"website","version":"1.0.0","lockfileVersion":3,'
        '"packages":{"":{"name":"website","version":"1.0.0"}}}\n',
        encoding="utf-8",
    )
    nested_dependency = nested_primary / "node_modules" / "nested-dependency"
    nested_dependency.mkdir(parents=True)
    (nested_dependency / "index.js").write_text("module.exports = true;\n")
    _git(primary, "add", "website/package.json", "website/package-lock.json")
    _git(primary, "commit", "-m", "add independent website project")

    target = tmp_path / "target"
    _git(primary, "worktree", "add", "--detach", str(target), "HEAD")
    wd.provision_node_dependencies(primary, target)

    assert (target / "node_modules" / wd._PROVISION_MARKER).is_file()
    assert (
        target
        / "website"
        / "node_modules"
        / "nested-dependency"
        / "index.js"
    ).is_file()
    assert (
        target / "website" / "node_modules" / wd._PROVISION_MARKER
    ).is_file()


def test_workspace_member_dependencies_are_copied_marked_and_cleaned(tmp_path):
    primary = _repo_with_node_dependencies(tmp_path)
    root_package = json.loads((primary / "package.json").read_text(encoding="utf-8"))
    root_package["workspaces"] = ["apps/*"]
    (primary / "package.json").write_text(
        json.dumps(root_package, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    member = primary / "apps" / "desktop"
    member.mkdir(parents=True)
    (member / "package.json").write_text(
        '{"name":"desktop","version":"1.0.0"}\n', encoding="utf-8"
    )
    member_dependency = member / "node_modules" / "desktop-dependency"
    member_dependency.mkdir(parents=True)
    (member_dependency / "index.js").write_text("module.exports = true;\n")
    _git(primary, "add", "package.json", "apps/desktop/package.json")
    _git(primary, "commit", "-m", "add npm workspace member")

    target = primary / ".worktrees" / "workspace-target"
    _git(primary, "worktree", "add", "--detach", str(target), "HEAD")
    wd.provision_node_dependencies(primary, target)

    assert (
        target
        / "apps"
        / "desktop"
        / "node_modules"
        / "desktop-dependency"
        / "index.js"
    ).is_file()
    assert (
        target / "apps" / "desktop" / "node_modules" / wd._PROVISION_MARKER
    ).is_file()

    wd.cleanup_provisioned_node_dependencies(target)

    assert not (target / "node_modules").exists()
    assert not (target / "apps" / "desktop" / "node_modules").exists()


def test_existing_marker_never_bypasses_dependency_revalidation(tmp_path):
    primary = _repo_with_node_dependencies(tmp_path)
    target = tmp_path / "target"
    _git(primary, "worktree", "add", "--detach", str(target), "HEAD")
    wd.provision_node_dependencies(primary, target)
    copied = target / "node_modules" / "fixture-dependency" / "index.js"
    copied.write_text("module.exports = 'tampered';\n", encoding="utf-8")

    wd.provision_node_dependencies(primary, target)

    assert copied.read_text(encoding="utf-8") == "module.exports = 'from primary';\n"


def test_unignored_dependency_tree_fails_before_provisioning(tmp_path):
    repo = _repo_with_node_dependencies(tmp_path)
    (repo / ".gitignore").unlink()
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "remove node_modules ignore rule")
    target = tmp_path / "target"
    _git(repo, "worktree", "add", "--detach", str(target), "HEAD")

    with pytest.raises(RuntimeError, match="unignored dependency tree"):
        wd.provision_node_dependencies(repo, target)

    assert not (target / "node_modules").exists()


@pytest.mark.parametrize(
    "pattern",
    [
        r"apps\{desktop,shared}",
        "apps/[^d]*",
        "apps/[[:alpha:]]*",
        "apps/[![:alpha:]]*",
        "apps/[a[:digit:]]*",
    ],
)
def test_workspace_patterns_with_different_python_glob_semantics_fail_closed(
    tmp_path, pattern
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": [pattern]}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unsupported npm workspace pattern"):
        wd._workspace_package_dirs(project)


def test_copy_path_refuses_binding_gyp_without_project_build_output(tmp_path):
    primary = _repo_with_node_dependencies(tmp_path)
    (primary / "binding.gyp").write_text("{}\n", encoding="utf-8")
    _git(primary, "add", "binding.gyp")
    _git(primary, "commit", "-m", "add native addon metadata")
    target = tmp_path / "target"
    _git(primary, "worktree", "add", "--detach", str(target), "HEAD")

    with pytest.raises(RuntimeError, match="require lifecycle scripts"):
        wd.provision_node_dependencies(primary, target)

    assert not (target / "node_modules").exists()


def test_copy_path_allows_declared_root_postinstall_already_reflected_in_source(
    tmp_path,
):
    primary = _repo_with_node_dependencies(tmp_path)
    package = json.loads((primary / "package.json").read_text(encoding="utf-8"))
    package["scripts"]["postinstall"] = "echo dependencies ready"
    (primary / "package.json").write_text(
        json.dumps(package, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    _git(primary, "add", "package.json")
    _git(primary, "commit", "-m", "add harmless postinstall")
    target = tmp_path / "target"
    _git(primary, "worktree", "add", "--detach", str(target), "HEAD")

    wd.provision_node_dependencies(primary, target)

    assert (
        target / "node_modules" / "fixture-dependency" / "index.js"
    ).read_text(encoding="utf-8") == "module.exports = 'from primary';\n"


def test_epic_verification_holds_exclusive_consumer_lock(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / ".worktrees" / "epic-verify-epic-demo"

    monkeypatch.setattr(
        kb, "read_board_metadata", lambda _board: {"default_workdir": str(repo)}
    )
    monkeypatch.setattr(kb, "get_current_board", lambda: "fixture")
    monkeypatch.setattr(kb, "_git_toplevel", lambda _path: repo)
    monkeypatch.setattr(kb, "_ensure_epic_branch", lambda *_args, **_kwargs: None)

    def materialize(*_args, **_kwargs):
        (target / "scripts").mkdir(parents=True)
        (target / "scripts" / "run_tests.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )

    monkeypatch.setattr(kb, "_materialize_worktree_with_dependencies", materialize)
    monkeypatch.setattr(kb, "_cleanup_provisioned_node_dependencies", lambda _path: None)

    def run_tests(command, **_kwargs):
        assert command == ["bash", "scripts/run_tests.sh"]
        with pytest.raises(RuntimeError, match="already active"):
            wd._acquire_project_lock(target / ".hermes-epic-verification")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(kb.subprocess, "run", run_tests)

    assert kb._default_epic_verify("epic/demo") is True
