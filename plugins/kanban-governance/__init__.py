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
from tools.approval import (
    shell_command_argvs,
    shell_command_has_redirection,
    shell_command_output_paths,
)


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
    "rev-list",
    "rev-parse",
    "show",
    "show-ref",
    "status",
}
_GIT_OPTIONS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}
_KNOWN_MUTATING_COMMANDS = {
    "chmod",
    "chown",
    "cp",
    "dd",
    "install",
    "ln",
    "mkdir",
    "mv",
    "rm",
    "rmdir",
    "tee",
    "touch",
    "truncate",
    "unlink",
}
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


def _git_tag_is_read_only(args: list[str]) -> bool:
    if not args:
        return True
    read_flags = {
        "--column", "--contains", "--format", "--ignore-case", "--list",
        "--merged", "--no-contains", "--no-merged", "--points-at", "--sort",
        "-l", "-n",
    }
    flags_with_value = {
        "--contains", "--format", "--merged", "--no-contains", "--no-merged",
        "--points-at", "--sort",
    }
    index = 0
    listing = False
    while index < len(args):
        word = args[index]
        option = word.split("=", 1)[0]
        if not word.startswith("-"):
            if listing:
                index += 1
                continue
            return False
        if option not in read_flags:
            return False
        if option in {"-l", "--list"}:
            listing = True
        index += 1
        if option in flags_with_value and "=" not in word:
            index += 1
    return True


def _git_remote_is_read_only(args: list[str]) -> bool:
    if not args or all(arg in {"-v", "--verbose"} for arg in args):
        return True
    return args[0] in {"get-url", "show"}


def _find_is_read_only(args: list[str]) -> bool:
    mutating = {
        "-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprintf",
        "-ok", "-okdir",
    }
    return not any(arg in mutating for arg in args)


def _awk_is_read_only(args: list[str]) -> bool:
    program = next((arg for arg in args if not arg.startswith("-")), "")
    return bool(
        program
        and ">" not in program
        and "system(" not in program.replace(" ", "").lower()
        and "|" not in program
    )


def _diff_is_read_only(args: list[str]) -> bool:
    return not any(
        arg == "--output" or arg.startswith("--output=")
        for arg in args
    )


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
        if executable == "awk" and not _awk_is_read_only(argv[1:]):
            return False
        if executable == "find" and not _find_is_read_only(argv[1:]):
            return False
        if executable == "git":
            subcommand, subargs = _git_subcommand(argv)
            if subcommand == "branch":
                if not _git_branch_is_read_only(subargs):
                    return False
            elif subcommand == "tag":
                if not _git_tag_is_read_only(subargs):
                    return False
            elif subcommand == "remote":
                if not _git_remote_is_read_only(subargs):
                    return False
            elif subcommand == "diff":
                if not _diff_is_read_only(subargs):
                    return False
            elif subcommand not in _READ_ONLY_GIT:
                return False
        elif executable == "diff" and not _diff_is_read_only(argv[1:]):
            return False
        elif executable == "sed" and any(
            arg == "--in-place" or (arg.startswith("-") and "i" in arg[1:])
            for arg in argv[1:]
        ):
            return False
    return True


def _is_privileged_worker_command(command: str) -> bool:
    known_git_commands = _READ_ONLY_GIT | {
        "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean",
        "commit", "merge", "mv", "rebase", "remote", "reset", "restore",
        "revert", "rm", "stash", "switch", "tag", "update-ref", "push",
    }
    for argv in shell_command_argvs(command):
        executable = os.path.basename(argv[0]).lower()
        if (
            executable == "busybox"
            and len(argv) > 2
            and argv[1].lower() in {"ash", "bash", "sh"}
            and any(arg.startswith("-") and "c" in arg[1:] for arg in argv[2:])
        ):
            return True
        if executable in {"bash", "dash", "ksh", "sh", "zsh"} and any(
            arg.startswith("-") and "c" in arg[1:] for arg in argv[1:]
        ):
            return True
        if "deploy" in executable:
            return True
        if executable in {"npm", "pnpm", "yarn", "bun"} and any(
            "deploy" in arg.lower() or "publish" in arg.lower()
            for arg in argv[1:]
        ):
            return True
        if executable in {"make", "just"} and any(
            "deploy" in arg.lower() or "publish" in arg.lower()
            for arg in argv[1:]
        ):
            return True
        if executable != "git":
            continue
        if any(arg.lower().startswith("alias.") for arg in argv[1:]):
            return True
        subcommand, subargs = _git_subcommand(argv)
        if subcommand in {"push", "reset", "update-ref"}:
            return True
        if subcommand in {"switch", "checkout"} and any(
            arg.split("=", 1)[0]
            in {"-b", "-B", "-c", "-C", "--create", "--orphan"}
            for arg in subargs
        ):
            return True
        if subcommand == "branch" and not _git_branch_is_read_only(subargs):
            return True
        if subcommand == "tag" and not _git_tag_is_read_only(subargs):
            return True
        if subcommand == "remote" and not _git_remote_is_read_only(subargs):
            return True
        if subcommand not in known_git_commands:
            return True
    return False


def _task_path(value: Any, task_context: str) -> Path:
    raw = str(value or "").strip()
    from tools.file_tools import _resolve_path_for_task

    return Path(str(_resolve_path_for_task(raw or ".", task_context))).resolve(
        strict=False
    )


def _terminal_workdir(args: dict[str, Any], task_context: str) -> Path:
    raw = str(args.get("workdir") or "").strip()
    if not raw:
        return _task_path(".", task_context)
    if Path(raw).expanduser().is_absolute():
        return _resolved_path(raw)
    return _task_path(raw, task_context)


def _option_paths(args: list[str], option_name: str) -> list[str]:
    paths: list[str] = []
    for index, arg in enumerate(args):
        if arg == option_name and index + 1 < len(args):
            paths.append(args[index + 1])
        elif arg.startswith(f"{option_name}="):
            paths.append(arg.split("=", 1)[1])
    return paths


def _sed_in_place_paths(args: list[str]) -> list[str]:
    paths: list[str] = []
    script_seen = False
    index = 0
    while index < len(args):
        arg = args[index]
        option = arg.split("=", 1)[0]
        if option in {"-e", "--expression", "-f", "--file"}:
            script_seen = True
            index += 2 if "=" not in arg else 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if not script_seen:
            script_seen = True
        else:
            paths.append(arg)
        index += 1
    return paths


def _find_output_paths(args: list[str]) -> list[str]:
    paths: list[str] = []
    for index, arg in enumerate(args):
        if arg in {"-fls", "-fprint", "-fprintf"} and index + 1 < len(args):
            paths.append(args[index + 1])
    return paths


def _path_operands(argv: list[str]) -> list[str]:
    executable = os.path.basename(argv[0]).lower()
    operands = [arg for arg in argv[1:] if arg != "--" and not arg.startswith("-")]
    if executable in {"cp", "install"}:
        return operands[-1:] if operands else []
    if executable in {
        "chmod", "chown", "ln", "mkdir", "mv", "rm", "rmdir", "tee",
        "touch", "truncate", "unlink",
    }:
        return operands
    if executable == "dd":
        return [arg.split("=", 1)[1] for arg in argv[1:] if arg.startswith("of=")]
    if executable == "diff":
        return _option_paths(argv[1:], "--output")
    if executable == "find":
        return _find_output_paths(argv[1:])
    if executable == "sed":
        return _sed_in_place_paths(argv[1:])
    if executable == "git":
        targets: list[str] = []
        for index, arg in enumerate(argv[1:], start=1):
            if arg in {"-C", "--git-dir", "--work-tree"} and index + 1 < len(argv):
                targets.append(argv[index + 1])
            if arg.startswith(("--git-dir=", "--work-tree=")):
                targets.append(arg.split("=", 1)[1])
        subcommand, subargs = _git_subcommand(argv)
        if subcommand == "diff":
            targets.extend(_option_paths(subargs, "--output"))
        return targets
    return []


def _terminal_targets(
    args: dict[str, Any], task_context: str
) -> tuple[list[Path], bool]:
    command = str(args.get("command") or "")
    workdir = _terminal_workdir(args, task_context)
    raw_targets = shell_command_output_paths(command)
    ambiguous = False
    for argv in shell_command_argvs(command):
        executable = os.path.basename(argv[0]).lower()
        if executable == "awk":
            ambiguous = True
        if executable == "find" and any(
            arg in {"-exec", "-execdir", "-ok", "-okdir"} for arg in argv[1:]
        ):
            ambiguous = True
        if (
            executable not in _READ_ONLY_COMMANDS
            and executable not in _KNOWN_MUTATING_COMMANDS
        ):
            ambiguous = True
        raw_targets.extend(_path_operands(argv))

    targets = [workdir]
    for raw in raw_targets:
        if not raw or raw == "-":
            continue
        if any(char in raw for char in ("$", "`", "*", "?", "[", "]")):
            ambiguous = True
            continue
        targets.append(_resolved_path(raw, base=workdir))
    return list(dict.fromkeys(targets)), ambiguous


def _effective_targets(
    tool_name: str,
    args: dict[str, Any],
    task_context: str,
) -> tuple[list[Path], bool]:
    if tool_name == "terminal":
        return _terminal_targets(args, task_context)
    if tool_name == "patch" and args.get("mode", "replace") == "patch":
        from tools.file_tools import extract_v4a_patch_paths

        raw_paths = extract_v4a_patch_paths(str(args.get("patch") or ""))
        if not raw_paths:
            return [_task_path(".", task_context)], True
        return [_task_path(path, task_context) for path in raw_paths], False
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return [_task_path(".", task_context)], True
    return [_task_path(raw_path, task_context)], False


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
    targets: list[Path],
    governances: list[Optional[dict]],
    *,
    tool_name: str,
    args: dict[str, Any],
    ambiguous_targets: bool,
) -> Optional[dict[str, str]]:
    if tool_name == "terminal" and _is_privileged_worker_command(
        str(args.get("command") or "")
    ):
        return _block("workers may not push, create branches, force-update refs, reset, or deploy")
    if ambiguous_targets:
        return _block("worker mutation targets could not be verified")
    try:
        task = _load_worker_task(task_id)
        workspace = _resolved_path(task.workspace_path)
    except Exception as exc:
        return _block(f"worker ownership could not be verified: {exc}")
    if not task.workspace_path or any(
        not _path_is_within(target, workspace) for target in targets
    ):
        return _block("worker mutation is outside the card workspace")
    if not task.project_id or any(
        not governance or governance.get("project_id") != task.project_id
        for governance in governances
    ):
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
    normalized = _normalize_tool_name(tool_name)
    call_args = args if isinstance(args, dict) else {}
    if normalized == "terminal":
        if _is_read_only_terminal(str(call_args.get("command") or "")):
            return None
    elif normalized not in _FILE_MUTATORS:
        return None

    worker_task_id = str(os.getenv("HERMES_KANBAN_TASK") or "").strip()
    task_context = str(task_id or session_id or "default")
    try:
        targets, ambiguous_targets = _effective_targets(
            normalized, call_args, task_context
        )
    except Exception as exc:
        return _block(f"mutation target could not be resolved: {exc}")
    try:
        governances = [_governance(target) for target in targets]
    except Exception as exc:
        return _block(f"project ownership could not be resolved: {exc}")

    if worker_task_id:
        return _validate_worker(
            worker_task_id,
            targets,
            governances,
            tool_name=normalized,
            args=call_args,
            ambiguous_targets=ambiguous_targets,
        )

    governance = next(
        (
            item
            for item in governances
            if item and item.get("kanban_governed")
        ),
        None,
    )
    if governance is None:
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
