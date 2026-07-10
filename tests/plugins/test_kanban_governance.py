"""Policy tests for the opt-in Kanban governance pre-tool hook."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


def _load_plugin():
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "kanban-governance"
    if "hermes_plugins" not in sys.modules:
        namespace = types.ModuleType("hermes_plugins")
        namespace.__path__ = []
        sys.modules["hermes_plugins"] = namespace
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.kanban_governance",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "hermes_plugins.kanban_governance"
    module.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.kanban_governance"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def governed_workspace(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    repo = tmp_path / "governed"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "task-branch", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("governed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    board = "governed-board"
    kb.create_board(board, name="Governed")
    with pdb.connect() as conn:
        project_id = pdb.create_project(
            conn,
            name="Governed",
            folders=[str(repo)],
            board_slug=board,
        )
    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Governed task",
            assignee="developer",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="task-branch",
            project_id=project_id,
            board=board,
        )
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board)))
    return {
        "home": home,
        "repo": repo,
        "outside": outside,
        "board": board,
        "project_id": project_id,
        "task_id": task_id,
    }


def test_manifest_declares_only_pre_tool_call():
    manifest = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban-governance"
        / "plugin.yaml"
    ).read_text(encoding="utf-8")
    assert "hooks:\n  - pre_tool_call" in manifest
    assert "post_tool_call" not in manifest


def test_reads_are_always_allowed(governed_workspace):
    mod = _load_plugin()
    repo = governed_workspace["repo"]
    assert mod._on_pre_tool_call("read_file", {"path": str(repo / "README.md")}) is None
    assert mod._on_pre_tool_call(
        "terminal", {"command": "git status", "workdir": str(repo)}
    ) is None
    assert mod._on_pre_tool_call(
        "terminal", {"command": "sed -n '1,3p' README.md", "workdir": str(repo)}
    ) is None
    assert mod._on_pre_tool_call(
        "terminal", {"command": "echo 'git push > output'", "workdir": str(repo)}
    ) is None
    assert mod._on_pre_tool_call(
        "terminal", {"command": "awk '{print $1}' README.md", "workdir": str(repo)}
    ) is None
    assert mod._on_pre_tool_call(
        "terminal", {"command": "git tag --list 'v*'", "workdir": str(repo)}
    ) is None


def test_terminal_redirection_is_a_governed_mutation(governed_workspace):
    mod = _load_plugin()
    repo = governed_workspace["repo"]
    decision = mod._on_pre_tool_call(
        "terminal", {"command": "echo changed > generated.txt", "workdir": str(repo)}
    )
    assert decision["action"] == "approve"


def test_terminal_uses_real_workdir_not_synthetic_cwd(governed_workspace):
    mod = _load_plugin()
    decision = mod._on_pre_tool_call(
        "terminal",
        {
            "command": "touch generated.txt",
            "workdir": str(governed_workspace["outside"]),
            "cwd": str(governed_workspace["repo"]),
        },
    )
    assert decision is None


def test_relative_file_path_uses_task_environment_workdir(governed_workspace):
    from tools.terminal_tool import clear_task_env_overrides, register_task_env_overrides

    mod = _load_plugin()
    session_task_id = "governance-session"
    register_task_env_overrides(
        session_task_id, {"cwd": str(governed_workspace["repo"])}
    )
    try:
        decision = mod._on_pre_tool_call(
            "write_file",
            {"path": "README.md", "content": "changed"},
            task_id=session_task_id,
        )
    finally:
        clear_task_env_overrides(session_task_id)
    assert decision["action"] == "approve"


def test_terminal_without_workdir_uses_task_environment_workdir(governed_workspace):
    from tools.terminal_tool import clear_task_env_overrides, register_task_env_overrides

    mod = _load_plugin()
    session_task_id = "governance-terminal-session"
    register_task_env_overrides(
        session_task_id, {"cwd": str(governed_workspace["repo"])}
    )
    try:
        decision = mod._on_pre_tool_call(
            "terminal",
            {"command": "touch generated.txt"},
            task_id=session_task_id,
        )
    finally:
        clear_task_env_overrides(session_task_id)
    assert decision["action"] == "approve"


def test_relative_path_resolution_failure_blocks(governed_workspace, monkeypatch):
    mod = _load_plugin()

    def _fail(*args, **kwargs):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr("tools.file_tools._resolve_path_for_task", _fail)
    decision = mod._on_pre_tool_call(
        "write_file",
        {"path": "README.md", "content": "changed"},
        task_id="broken-session",
    )
    assert decision["action"] == "block"
    assert "resolved" in decision["message"].lower()


@pytest.mark.parametrize("command", [
    "find . -delete",
    "git tag new-tag",
    "git tag -d old-tag",
    "git remote add backup https://example.invalid/repo.git",
    "git remote remove backup",
    "awk 'BEGIN { print \"changed\" > \"generated.txt\" }'",
])
def test_mutating_command_variants_are_governed(governed_workspace, command):
    mod = _load_plugin()
    decision = mod._on_pre_tool_call(
        "terminal", {"command": command, "workdir": str(governed_workspace["repo"])}
    )
    assert decision["action"] == "approve"


def test_human_write_outside_governed_project_is_allowed(governed_workspace):
    mod = _load_plugin()
    path = governed_workspace["outside"] / "notes.txt"
    assert mod._on_pre_tool_call("write_file", {"path": str(path), "content": "ok"}) is None


@pytest.mark.parametrize("tool_name,args", [
    ("write_file", {"path": "README.md", "content": "changed"}),
    ("patch", {"path": "README.md", "patch": "*** Begin Patch"}),
    ("terminal", {"command": "touch generated.txt"}),
])
def test_governed_mutation_without_worker_requires_exact_human_approval(
    governed_workspace, tool_name, args
):
    mod = _load_plugin()
    repo = governed_workspace["repo"]
    call_args = dict(args)
    if tool_name == "terminal":
        call_args["workdir"] = str(repo)
    elif not os.path.isabs(call_args["path"]):
        call_args["path"] = str(repo / call_args["path"])
    decision = mod._on_pre_tool_call(tool_name, call_args)
    assert decision["action"] == "approve"
    assert decision["rule_key"].startswith("kanban-governance:")
    override = decision["one_shot_override"]
    assert override["project_id"] == governed_workspace["project_id"]
    assert override["operation_hash"] == mod._operation_hash(tool_name, call_args)
    assert override["expires_at"] > override["created_at"]


def test_valid_worker_write_in_its_workspace_and_branch_is_allowed(
    governed_workspace, monkeypatch
):
    mod = _load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", governed_workspace["task_id"])
    path = governed_workspace["repo"] / "README.md"
    assert mod._on_pre_tool_call("write_file", {"path": str(path), "content": "ok"}) is None


def test_worker_write_outside_card_workspace_is_blocked(governed_workspace, monkeypatch):
    mod = _load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", governed_workspace["task_id"])
    decision = mod._on_pre_tool_call(
        "write_file",
        {"path": str(governed_workspace["outside"] / "escape.txt"), "content": "no"},
    )
    assert decision["action"] == "block"
    assert "workspace" in decision["message"].lower()


def test_worker_ambiguous_file_mutation_fails_closed(governed_workspace, monkeypatch):
    mod = _load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", governed_workspace["task_id"])
    decision = mod._on_pre_tool_call(
        "patch", {"mode": "patch", "patch": "*** Begin Patch"}
    )
    assert decision["action"] == "block"
    assert "target" in decision["message"].lower()


def test_worker_wrong_branch_is_blocked(governed_workspace, monkeypatch):
    mod = _load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", governed_workspace["task_id"])
    subprocess.run(
        ["git", "-C", str(governed_workspace["repo"]), "switch", "-c", "wrong-branch"],
        check=True,
        capture_output=True,
    )
    decision = mod._on_pre_tool_call(
        "write_file",
        {"path": str(governed_workspace["repo"] / "README.md"), "content": "no"},
    )
    assert decision["action"] == "block"
    assert "branch" in decision["message"].lower()


@pytest.mark.parametrize("command", [
    "git push origin HEAD",
    "git switch -c surprise",
    "git branch surprise",
    "git update-ref refs/heads/main HEAD",
    "git reset --hard HEAD^",
    "./deploy.sh production",
    "npm run deploy",
    "git switch --create=surprise",
    "git checkout --orphan=surprise",
    "git -c alias.ship=push ship origin HEAD",
    "sh -c 'git push origin HEAD'",
])
def test_worker_privileged_git_and_deploy_actions_are_blocked(
    governed_workspace, monkeypatch, command
):
    mod = _load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", governed_workspace["task_id"])
    decision = mod._on_pre_tool_call(
        "terminal", {"command": command, "workdir": str(governed_workspace["repo"])}
    )
    assert decision["action"] == "block"


@pytest.mark.parametrize("command", [
    "touch {target}",
    "echo changed > {target}",
    "git --git-dir={target}/.git add file.txt",
])
def test_worker_terminal_write_target_outside_workspace_is_blocked(
    governed_workspace, monkeypatch, command
):
    mod = _load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", governed_workspace["task_id"])
    target = governed_workspace["outside"] / "escape.txt"
    decision = mod._on_pre_tool_call(
        "terminal",
        {
            "command": command.format(target=target),
            "workdir": str(governed_workspace["repo"]),
        },
    )
    assert decision["action"] == "block"
    assert "workspace" in decision["message"].lower()


def test_worker_find_exec_target_fails_closed(governed_workspace, monkeypatch):
    mod = _load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", governed_workspace["task_id"])
    target = governed_workspace["outside"] / "escape.txt"
    decision = mod._on_pre_tool_call(
        "terminal",
        {
            "command": f"find . -exec touch {target} {{}} +",
            "workdir": str(governed_workspace["repo"]),
        },
    )
    assert decision["action"] == "block"
    assert "target" in decision["message"].lower()


def test_human_terminal_target_in_governed_project_requires_approval(
    governed_workspace,
):
    mod = _load_plugin()
    target = governed_workspace["repo"] / "generated.txt"
    decision = mod._on_pre_tool_call(
        "terminal",
        {
            "command": f"touch {target}",
            "workdir": str(governed_workspace["outside"]),
        },
    )
    assert decision["action"] == "approve"


def test_one_shot_override_record_is_exact_and_consumed_once(governed_workspace):
    mod = _load_plugin()
    args = {
        "path": str(governed_workspace["repo"] / "README.md"),
        "content": "approved exact content",
    }
    decision = mod._on_pre_tool_call("write_file", args)
    record = decision["one_shot_override"]
    audit_path = mod._consume_approved_override(
        "write_file", args, record, actor="human:test"
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert audit["operation_hash"] == mod._operation_hash("write_file", args)
    assert audit["actor"] == "human:test"
    assert audit["consumed_at"] >= audit["created_at"]
    with pytest.raises(ValueError, match="already consumed"):
        mod._consume_approved_override("write_file", args, record, actor="human:test")
    changed = dict(args, content="different")
    with pytest.raises(ValueError, match="operation hash"):
        mod._consume_approved_override("write_file", changed, record, actor="human:test")
