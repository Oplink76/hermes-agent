"""Kanban project governance at filesystem side-effect boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb
from hermes_constants import get_hermes_home
from tools.approval import (
    shell_command_argvs,
    shell_command_has_redirection,
    shell_command_output_paths,
    shell_command_prefix_environment,
)


_FILE_MUTATORS = {"write_file", "patch"}
_ALWAYS_READ_ONLY_COMMANDS = {
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
_OVERRIDE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_GIT_EXECUTION_ENV = {
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_EXTERNAL_DIFF",
    "GIT_PAGER",
    "PAGER",
}


@dataclass(frozen=True)
class _CommandPolicy:
    read_only: Optional[Callable[[list[str]], bool]] = None
    targets: Optional[Callable[[list[str]], tuple[list[str], bool]]] = None
    privileged: Optional[Callable[[list[str]], bool]] = None


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


def _git_config_key_can_execute(key: str) -> bool:
    normalized = key.strip().lower()
    return bool(
        normalized in {
            "core.fsmonitor",
            "core.pager",
            "diff.external",
            "interactive.difffilter",
        }
        or normalized.startswith(("alias.", "pager."))
        or re.fullmatch(r"diff\.[^.]+\.(?:command|textconv)", normalized)
        or re.fullmatch(r"filter\.[^.]+\.(?:clean|process|smudge)", normalized)
    )


def _git_has_execution_config(argv: list[str]) -> bool:
    index = 1
    while index < len(argv):
        word = argv[index]
        if not word.startswith("-"):
            return False
        if word == "-c":
            if index + 1 >= len(argv):
                return True
            assignment = argv[index + 1]
            key = assignment.split("=", 1)[0]
            if "=" not in assignment or _git_config_key_can_execute(key):
                return True
            index += 2
            continue
        if word.startswith("-c") and word != "-c":
            assignment = word[2:]
            key = assignment.split("=", 1)[0]
            if "=" not in assignment or _git_config_key_can_execute(key):
                return True
            index += 1
            continue
        if word == "--config-env" or word.startswith("--config-env="):
            return True
        option = word.split("=", 1)[0]
        index += 1
        if option in _GIT_OPTIONS_WITH_VALUE and "=" not in word:
            index += 1
    return False


def _git_is_read_only(argv: list[str]) -> bool:
    if _git_has_execution_config(argv):
        return False
    subcommand, subargs = _git_subcommand(argv)
    if subcommand == "branch":
        return _git_branch_is_read_only(subargs)
    if subcommand == "tag":
        return _git_tag_is_read_only(subargs)
    if subcommand == "remote":
        return _git_remote_is_read_only(subargs)
    if subcommand == "diff":
        return (
            _diff_is_read_only(subargs)
            and not any(
                arg in {"--ext-diff", "--textconv"} for arg in subargs
            )
        )
    return subcommand in _READ_ONLY_GIT


def _git_is_privileged(argv: list[str]) -> bool:
    if _git_has_execution_config(argv):
        return True
    known = _READ_ONLY_GIT | {
        "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean",
        "commit", "merge", "mv", "rebase", "remote", "reset", "restore",
        "revert", "rm", "stash", "switch", "tag", "update-ref", "push",
    }
    subcommand, subargs = _git_subcommand(argv)
    if subcommand == "diff" and any(
        arg in {"--ext-diff", "--textconv"} for arg in subargs
    ):
        return True
    if subcommand in {"push", "reset", "update-ref"}:
        return True
    if subcommand in {"switch", "checkout"} and any(
        arg.split("=", 1)[0]
        in {"-b", "-B", "-c", "-C", "--create", "--orphan"}
        for arg in subargs
    ):
        return True
    if subcommand == "branch":
        return not _git_branch_is_read_only(subargs)
    if subcommand == "tag":
        return not _git_tag_is_read_only(subargs)
    if subcommand == "remote":
        return not _git_remote_is_read_only(subargs)
    return subcommand not in known


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
    prefix_environment = shell_command_prefix_environment(command)
    if prefix_environment & _GIT_EXECUTION_ENV and any(
        os.path.basename(argv[0]).lower() == "git" for argv in commands
    ):
        return False
    for argv in commands:
        policy = _command_policy(argv)
        if policy is None or policy.read_only is None:
            return False
        if not policy.read_only(argv):
            return False
    return True


def _is_privileged_worker_command(command: str) -> bool:
    prefix_environment = shell_command_prefix_environment(command)
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
        policy = _command_policy(argv)
        if policy and policy.privileged and policy.privileged(argv):
            return True
        if executable == "git" and prefix_environment & _GIT_EXECUTION_ENV:
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


def _positional_operands(
    args: list[str], *, options_with_values: set[str] | None = None
) -> list[str]:
    options_with_values = options_with_values or set()
    operands: list[str] = []
    index = 0
    options_done = False
    while index < len(args):
        arg = args[index]
        if arg == "--":
            options_done = True
            index += 1
            continue
        option = arg.split("=", 1)[0]
        if not options_done and arg.startswith("-"):
            index += 1
            if option in options_with_values and "=" not in arg:
                index += 1
            continue
        operands.append(arg)
        index += 1
    return operands


def _target_directory(args: list[str]) -> tuple[Optional[str], bool]:
    targets: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-t", "--target-directory"}:
            if index + 1 >= len(args):
                return None, True
            targets.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("--target-directory="):
            value = arg.split("=", 1)[1]
            if not value:
                return None, True
            targets.append(value)
        elif arg.startswith("-t") and len(arg) > 2 and not arg.startswith("-T"):
            targets.append(arg[2:])
        index += 1
    if len(targets) > 1:
        return None, True
    return (targets[0] if targets else None), False


_INSTALL_OPTIONS_WITH_VALUES = {
    "-g", "--group", "-m", "--mode", "-o", "--owner",
    "--strip-program", "--context", "-t", "--target-directory",
}
_TARGET_DIRECTORY_OPTIONS = {"-t", "--target-directory"}


def _copy_like_targets(argv: list[str]) -> tuple[list[str], bool]:
    executable = os.path.basename(argv[0]).lower()
    args = argv[1:]
    target_directory, ambiguous = _target_directory(args)
    if ambiguous:
        return [], True
    options_with_values = (
        _INSTALL_OPTIONS_WITH_VALUES
        if executable == "install"
        else _TARGET_DIRECTORY_OPTIONS
    )
    operands = _positional_operands(args, options_with_values=options_with_values)
    if target_directory is not None:
        targets = [target_directory]
        if executable == "mv":
            targets.extend(operands)
        return targets, False
    if executable == "install" and any(
        arg in {"-d", "--directory"} for arg in args
    ):
        return operands, False
    if executable == "mv":
        return operands, False
    return (operands[-1:] if operands else []), False


def _all_operand_targets(argv: list[str]) -> tuple[list[str], bool]:
    return _positional_operands(argv[1:]), False


def _git_targets(argv: list[str]) -> tuple[list[str], bool]:
    targets: list[str] = []
    for index, arg in enumerate(argv[1:], start=1):
        if arg in {"-C", "--git-dir", "--work-tree"}:
            if index + 1 >= len(argv):
                return [], True
            targets.append(argv[index + 1])
        elif arg.startswith(("--git-dir=", "--work-tree=")):
            targets.append(arg.split("=", 1)[1])
    subcommand, subargs = _git_subcommand(argv)
    if subcommand == "diff":
        targets.extend(_option_paths(subargs, "--output"))
    return targets, False


def _find_targets(argv: list[str]) -> tuple[list[str], bool]:
    args = argv[1:]
    ambiguous = any(
        arg in {"-exec", "-execdir", "-ok", "-okdir"} for arg in args
    )
    return _find_output_paths(args), ambiguous


def _sed_targets(argv: list[str]) -> tuple[list[str], bool]:
    return _sed_in_place_paths(argv[1:]), False


def _diff_targets(argv: list[str]) -> tuple[list[str], bool]:
    return _option_paths(argv[1:], "--output"), False


def _dd_targets(argv: list[str]) -> tuple[list[str], bool]:
    return [arg.split("=", 1)[1] for arg in argv[1:] if arg.startswith("of=")], False


def _always_read_only(_argv: list[str]) -> bool:
    return True


def _awk_read_only(argv: list[str]) -> bool:
    return _awk_is_read_only(argv[1:])


def _find_read_only(argv: list[str]) -> bool:
    return _find_is_read_only(argv[1:])


def _diff_read_only(argv: list[str]) -> bool:
    return _diff_is_read_only(argv[1:])


def _sed_read_only(argv: list[str]) -> bool:
    return not any(
        arg == "--in-place" or (arg.startswith("-") and "i" in arg[1:])
        for arg in argv[1:]
    )


_COMMAND_POLICIES: dict[str, _CommandPolicy] = {
    name: _CommandPolicy(read_only=_always_read_only)
    for name in _ALWAYS_READ_ONLY_COMMANDS
}
_COMMAND_POLICIES.update({
    "awk": _CommandPolicy(read_only=_awk_read_only, targets=lambda _argv: ([], True)),
    "diff": _CommandPolicy(read_only=_diff_read_only, targets=_diff_targets),
    "find": _CommandPolicy(read_only=_find_read_only, targets=_find_targets),
    "git": _CommandPolicy(
        read_only=_git_is_read_only,
        targets=_git_targets,
        privileged=_git_is_privileged,
    ),
    "sed": _CommandPolicy(read_only=_sed_read_only, targets=_sed_targets),
    "cp": _CommandPolicy(targets=_copy_like_targets),
    "install": _CommandPolicy(targets=_copy_like_targets),
    "ln": _CommandPolicy(targets=_copy_like_targets),
    "mv": _CommandPolicy(targets=_copy_like_targets),
    "dd": _CommandPolicy(targets=_dd_targets),
})
for _mutator in {
    "chmod", "chown", "mkdir", "rm", "rmdir", "tee", "touch",
    "truncate", "unlink",
}:
    _COMMAND_POLICIES[_mutator] = _CommandPolicy(targets=_all_operand_targets)

_TRUSTED_TEST_WRAPPER_POLICY = _CommandPolicy(
    targets=lambda argv: ([argv[0]], False)
)


def _command_policy(argv: list[str]) -> Optional[_CommandPolicy]:
    executable = os.path.basename(argv[0]).lower()
    if executable == "run_tests.sh":
        return _TRUSTED_TEST_WRAPPER_POLICY
    return _COMMAND_POLICIES.get(executable)


def _terminal_targets(
    args: dict[str, Any], task_context: str
) -> tuple[list[Path], bool]:
    command = str(args.get("command") or "")
    workdir = _terminal_workdir(args, task_context)
    raw_targets = shell_command_output_paths(command)
    ambiguous = False
    for argv in shell_command_argvs(command):
        policy = _command_policy(argv)
        if policy is None:
            ambiguous = True
            continue
        if policy.targets is not None:
            policy_targets, policy_ambiguous = policy.targets(argv)
            raw_targets.extend(policy_targets)
            ambiguous = ambiguous or policy_ambiguous

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


def _trusted_test_wrapper_error(command: str, workspace: Path) -> Optional[str]:
    expected_lexical = Path(os.path.abspath(workspace / "scripts" / "run_tests.sh"))
    for argv in shell_command_argvs(command):
        if os.path.basename(argv[0]).lower() != "run_tests.sh":
            continue
        raw = Path(argv[0]).expanduser()
        candidate_lexical = Path(
            os.path.abspath(raw if raw.is_absolute() else workspace / raw)
        )
        candidate_resolved = candidate_lexical.resolve(strict=False)
        if candidate_lexical != expected_lexical:
            return "workers may only invoke the repo-local scripts/run_tests.sh wrapper"
        if (
            not _path_is_within(candidate_resolved, workspace)
            or not candidate_resolved.is_file()
        ):
            return "the repo-local test wrapper is missing or resolves outside the workspace"
    return None


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
    if tool_name == "terminal":
        wrapper_error = _trusted_test_wrapper_error(
            str(args.get("command") or ""), workspace
        )
        if wrapper_error:
            return _block(wrapper_error)
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
