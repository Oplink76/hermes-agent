"""Kanban project governance at filesystem side-effect boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from hermes_constants import get_hermes_home
from tools.approval import shell_command_argvs, shell_command_has_redirection


_FILE_MUTATORS = {"write_file", "patch"}
_READ_ONLY_COMMANDS = {
    "awk",
    "cat",
    "cut",
    "diff",
    "du",
    "echo",
    "env",
    "false",
    "fd",
    "find",
    "git",
    "grep",
    "head",
    "jq",
    "ls",
    "pwd",
    "readlink",
    "rg",
    "sed",
    "stat",
    "tail",
    "test",
    "true",
    "wc",
    "which",
}
_READ_ONLY_GIT = {
    "blame",
    "cat-file",
    "diff",
    "for-each-ref",
    "grep",
    "log",
    "ls-files",
    "ls-tree",
    "merge-base",
    "remote",
    "rev-list",
    "rev-parse",
    "show",
    "show-ref",
    "status",
    "tag",
}
_GIT_OPTIONS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}
_OVERRIDE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _normalize_tool_name(tool_name: str) -> str:
    return str(tool_name or "").strip().lower()


def _operation_hash(tool_name: str, args: Any) -> str:
    payload = {
        "tool_name": _normalize_tool_name(tool_name),
        "args": args if isinstance(args, dict) else {},
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolved_path(value: Any, *, base: Optional[Path] = None) -> Path:
    raw = str(value or "").strip()
    path = Path(raw).expanduser() if raw else (base or Path.cwd())
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve(strict=False)


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _git_subcommand(argv: list[str]) -> tuple[str, list[str]]:
    index = 1
    while index < len(argv):
        word = argv[index]
        option = word.split("=", 1)[0]
        if not word.startswith("-"):
            return word.lower(), argv[index + 1:]
        index += 1
        if option in _GIT_OPTIONS_WITH_VALUE and "=" not in word:
            index += 1
    return "", []


def _git_branch_is_read_only(args: list[str]) -> bool:
    read_flags = {
        "--all", "-a", "--contains", "--format", "--list", "-l",
        "--merged", "--no-contains", "--no-merged", "--show-current",
        "--sort", "-r", "--remotes", "-v", "-vv", "--verbose",
    }
    flags_with_value = {
        "--contains", "--format", "--no-contains", "--sort",
    }
    index = 0
    while index < len(args):
        word = args[index]
        option = word.split("=", 1)[0]
        if option not in read_flags:
            return False
        index += 1
        if option in flags_with_value and "=" not in word:
            index += 1
    return True


def _is_read_only_terminal(command: str) -> bool:
    if not str(command or "").strip():
        return True
    if shell_command_has_redirection(command):
        return False
    commands = shell_command_argvs(command)
    if not commands:
        return False
    for argv in commands:
        executable = os.path.basename(argv[0]).lower()
        if executable not in _READ_ONLY_COMMANDS:
            return False
        if executable == "git":
            subcommand, subargs = _git_subcommand(argv)
            if subcommand == "branch":
                if not _git_branch_is_read_only(subargs):
                    return False
            elif subcommand not in _READ_ONLY_GIT:
                return False
        elif executable == "sed" and any(
            arg == "--in-place" or (arg.startswith("-") and "i" in arg[1:])
            for arg in argv[1:]
        ):
            return False
    return True


def _is_privileged_worker_command(command: str) -> bool:
    for argv in shell_command_argvs(command):
        executable = os.path.basename(argv[0]).lower()
        if "deploy" in executable:
            return True
        if executable in {"npm", "pnpm", "yarn", "bun"} and any(
            "deploy" in arg.lower() or "publish" in arg.lower()
            for arg in argv[1:]
        ):
            return True
        if executable != "git":
            continue
        subcommand, subargs = _git_subcommand(argv)
        if subcommand in {"push", "reset", "update-ref"}:
            return True
        if subcommand in {"switch", "checkout"} and any(
            arg in {"-b", "-B", "-c", "-C", "--create", "--orphan"}
            for arg in subargs
        ):
            return True
        if subcommand == "branch" and not _git_branch_is_read_only(subargs):
            return True
    return False


def _effective_target(tool_name: str, args: dict[str, Any]) -> Path:
    if tool_name == "terminal":
        return _resolved_path(args.get("cwd"), base=Path.cwd())
    return _resolved_path(args.get("path"), base=Path.cwd())


def _governance(path: Path) -> Optional[dict]:
    with pdb.connect() as conn:
        return pdb.governance_for_path(conn, str(path))


def _load_worker_task(task_id: str):
    board = str(os.getenv("HERMES_KANBAN_BOARD") or "").strip()
    if not board:
        raise ValueError("worker has no pinned Kanban board")
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError("worker task does not exist on its pinned board")
    return task


def _current_branch(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), "branch", "--show-current"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise ValueError("worker workspace is not a readable Git worktree")
    branch = result.stdout.strip()
    if not branch:
        raise ValueError("worker workspace has a detached HEAD")
    return branch


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": f"Kanban governance: {message}"}


def _new_override(
    tool_name: str,
    args: dict[str, Any],
    governance: dict,
) -> dict[str, Any]:
    created_at = int(time.time())
    return {
        "override_id": secrets.token_hex(16),
        "actor": "human:pending",
        "task_id": str(os.getenv("HERMES_KANBAN_TASK") or ""),
        "project_id": governance["project_id"],
        "tool_name": _normalize_tool_name(tool_name),
        "operation_hash": _operation_hash(tool_name, args),
        "created_at": created_at,
        "expires_at": created_at + 300,
        "reason": "human mutation in a Kanban-governed project",
    }


def _consume_approved_override(
    tool_name: str,
    args: dict[str, Any],
    record: dict[str, Any],
    *,
    actor: str,
) -> Path:
    override_id = str(record.get("override_id") or "")
    if not _OVERRIDE_ID_RE.fullmatch(override_id):
        raise ValueError("invalid override id")
    if str(record.get("tool_name") or "") != _normalize_tool_name(tool_name):
        raise ValueError("tool name mismatch")
    if str(record.get("operation_hash") or "") != _operation_hash(tool_name, args):
        raise ValueError("operation hash mismatch")
    now = int(time.time())
    if now > int(record.get("expires_at") or 0):
        raise ValueError("override expired")

    audit_dir = get_hermes_home() / "audit" / "kanban-governance-overrides"
    audit_dir.mkdir(parents=True, exist_ok=True)
    marker = audit_dir / f"{override_id}.consumed"
    try:
        marker_fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("override already consumed") from exc
    os.close(marker_fd)

    audit_path = get_hermes_home() / "audit" / "kanban-governance-overrides.jsonl"
    audit = dict(record)
    audit.update(actor=str(actor or "human:unknown"), consumed_at=now)
    line = (json.dumps(audit, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    audit_fd = os.open(audit_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(audit_fd, line)
    finally:
        os.close(audit_fd)
    return audit_path


def _validate_worker(
    task_id: str,
    target: Path,
    governance: Optional[dict],
    *,
    tool_name: str,
    args: dict[str, Any],
) -> Optional[dict[str, str]]:
    if tool_name == "terminal" and _is_privileged_worker_command(
        str(args.get("command") or "")
    ):
        return _block("workers may not push, create branches, force-update refs, reset, or deploy")
    try:
        task = _load_worker_task(task_id)
        workspace = _resolved_path(task.workspace_path)
    except Exception as exc:
        return _block(f"worker ownership could not be verified: {exc}")
    if not task.workspace_path or not _path_is_within(target, workspace):
        return _block("worker mutation is outside the card workspace")
    if not task.project_id or not governance or governance.get("project_id") != task.project_id:
        return _block("worker mutation does not match the card project")
    if not task.branch_name:
        return _block("worker task has no assigned branch")
    try:
        branch = _current_branch(workspace)
    except Exception as exc:
        return _block(f"worker branch could not be verified: {exc}")
    if branch != task.branch_name:
        return _block(
            f"worker branch {branch!r} does not match assigned branch {task.branch_name!r}"
        )
    return None


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    task_id: str = "",
    session_id: str = "",
    **_: Any,
) -> Optional[dict[str, Any]]:
    del task_id, session_id
    normalized = _normalize_tool_name(tool_name)
    call_args = args if isinstance(args, dict) else {}
    if normalized == "terminal":
        if _is_read_only_terminal(str(call_args.get("command") or "")):
            return None
    elif normalized not in _FILE_MUTATORS:
        return None

    worker_task_id = str(os.getenv("HERMES_KANBAN_TASK") or "").strip()
    if worker_task_id and normalized in _FILE_MUTATORS and not str(
        call_args.get("path") or ""
    ).strip():
        return _block("worker mutation target is ambiguous")
    target = _effective_target(normalized, call_args)
    try:
        governance = _governance(target)
    except Exception as exc:
        return _block(f"project ownership could not be resolved: {exc}")

    if worker_task_id:
        return _validate_worker(
            worker_task_id,
            target,
            governance,
            tool_name=normalized,
            args=call_args,
        )

    if not governance or not governance.get("kanban_governed"):
        return None

    override = _new_override(normalized, call_args, governance)
    return {
        "action": "approve",
        "message": (
            "This exact mutation targets a Kanban-governed project and requires "
            "a one-shot human override. Session and permanent approval are disabled."
        ),
        "rule_key": f"kanban-governance:{override['operation_hash']}",
        "one_shot_override": override,
        "_consume_approved_override": _consume_approved_override,
    }


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
