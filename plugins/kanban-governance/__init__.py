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


_FILE_MUTATORS = {"execute_code", "patch", "write_file"}
_ALWAYS_READ_ONLY_COMMANDS = {
    "awk",
    "cat",
    "cut",
    "diff",
    "du",
    "echo",
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
    "for-each-ref",
    "ls-files",
    "ls-tree",
    "merge-base",
    "rev-list",
    "rev-parse",
    "show-ref",
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
_COMMAND_EXECUTION_ENV = _GIT_EXECUTION_ENV | {
    "BASH_ENV",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "ENV",
    "GIT_EXEC_PATH",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PATH",
}
_COMMAND_WRAPPERS = {
    "builtin",
    "command",
    "env",
    "exec",
    "nohup",
    "setsid",
    "sudo",
    "time",
}
_SED_PRINT_ONLY_RE = re.compile(
    r"\s*(?:(?:\d+|\$)\s*(?:,\s*(?:\d+|\$))?\s*)?p\s*"
)
_AWK_PRINT_ONLY_RE = re.compile(r"\s*\{\s*print(?:\s+\$\d+)?\s*\}\s*")
_INLINE_RIPGREP_CONFIG_RE = re.compile(
    r"(?<![A-Za-z0-9_])RIPGREP_CONFIG_PATH="
    r"(?:'(?P<single>[^']*)'|\"(?P<double>[^\"]*)\"|(?P<bare>[^\s;&|]*))"
)


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


def _git_has_execution_option(argv: list[str]) -> bool:
    index = 1
    while index < len(argv):
        word = argv[index]
        option = word.split("=", 1)[0]
        if not word.startswith("-"):
            break
        if option in {"-p", "--paginate", "--exec-path"}:
            return True
        index += 1
        if option in _GIT_OPTIONS_WITH_VALUE and "=" not in word:
            index += 1

    subcommand, subargs = _git_subcommand(argv)
    for arg in subargs:
        option = arg.split("=", 1)[0]
        if option in {
            "--ext-diff",
            "--filters",
            "--show-signature",
            "--textconv",
        }:
            return True
        if subcommand == "grep" and (
            option == "--open-files-in-pager"
            or arg == "-O"
            or (arg.startswith("-O") and len(arg) > 2)
        ):
            return True
    return False


def _git_is_read_only(argv: list[str]) -> bool:
    if _git_has_execution_config(argv) or _git_has_execution_option(argv):
        return False
    subcommand, _subargs = _git_subcommand(argv)
    return subcommand in _READ_ONLY_GIT


def _git_is_privileged(argv: list[str]) -> bool:
    return not _git_is_read_only(argv)


def _find_is_read_only(args: list[str]) -> bool:
    mutating = {
        "-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprint0",
        "-fprintf", "-ok", "-okdir",
    }
    return not any(arg in mutating for arg in args)


def _awk_is_read_only(args: list[str]) -> bool:
    return bool(
        len(args) >= 2
        and _AWK_PRINT_ONLY_RE.fullmatch(args[0])
        and all(
            arg and not arg.startswith("-") and "=" not in arg
            for arg in args[1:]
        )
    )


def _diff_is_read_only(args: list[str]) -> bool:
    return not any(
        arg == "--output" or arg.startswith("--output=")
        for arg in args
    )


def _command_requires_explicit_policy(command: str) -> bool:
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == '"':
                quote = None
                continue
            if char == "`" or command.startswith("$(", index):
                return True
            continue
        if char == "'":
            quote = char
            continue
        if char == '"':
            quote = '"'
            continue
        if char == "`" or command.startswith("$(", index):
            return True

    wrapper_pattern = "|".join(sorted(_COMMAND_WRAPPERS))
    return bool(
        re.search(
            rf"(?:^|[;&|\n])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
            rf"(?:{wrapper_pattern})(?:\s|$)",
            command,
        )
    )


def _rg_is_read_only(args: list[str]) -> bool:
    return not os.getenv("RIPGREP_CONFIG_PATH") and not any(
        arg == "--pre" or arg.startswith("--pre=")
        for arg in args
    )


def _fd_is_read_only(args: list[str]) -> bool:
    action_options = {"-x", "-X", "--exec", "--exec-batch"}
    return not any(
        arg.split("=", 1)[0] in action_options
        or (arg.startswith("-x") and arg != "-x")
        or (arg.startswith("-X") and arg != "-X")
        for arg in args
    )


def _sed_is_read_only(args: list[str]) -> bool:
    return bool(
        len(args) >= 3
        and args[0] == "-n"
        and _SED_PRINT_ONLY_RE.fullmatch(args[1])
        and all(arg and not arg.startswith("-") for arg in args[2:])
    )


def _command_uses_nonempty_ripgrep_config(
    command: str, commands: list[list[str]]
) -> bool:
    if not any(
        os.path.basename(argv[0]).lower() == "rg"
        for argv in commands
        if argv
    ):
        return False
    if os.getenv("RIPGREP_CONFIG_PATH"):
        return True
    if "RIPGREP_CONFIG_PATH" not in shell_command_prefix_environment(command):
        return False
    for match in _INLINE_RIPGREP_CONFIG_RE.finditer(command):
        value = next(
            (
                group
                for group in (
                    match.group("single"),
                    match.group("double"),
                    match.group("bare"),
                )
                if group is not None
            ),
            "",
        )
        if value:
            return True
    return False


def _is_read_only_terminal(command: str, workdir: Path) -> bool:
    if not str(command or "").strip():
        return True
    if shell_command_has_redirection(command):
        return False
    if _command_requires_explicit_policy(command):
        return False
    commands = shell_command_argvs(command)
    if not commands:
        return False
    if _command_uses_nonempty_ripgrep_config(command, commands):
        return False
    prefix_environment = shell_command_prefix_environment(command)
    if prefix_environment & _COMMAND_EXECUTION_ENV:
        return False
    for argv in commands:
        policy = _command_policy(argv)
        if policy is None or policy.read_only is None:
            return False
        if not _policy_executable_is_trusted(argv, workdir):
            return False
        if not policy.read_only(argv):
            return False
    return True


def _is_privileged_worker_command(command: str, workspace: Path) -> bool:
    prefix_environment = shell_command_prefix_environment(command)
    if prefix_environment & _COMMAND_EXECUTION_ENV:
        return True
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
        if policy is not None and not _policy_executable_is_trusted(
            argv, workspace
        ):
            return True
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
        option = arg.split("=", 1)[0]
        long_target_option = (
            option.startswith("--t")
            and "--target-directory".startswith(option)
        )
        if arg == "-t" or (long_target_option and "=" not in arg):
            if index + 1 >= len(args):
                return None, True
            targets.append(args[index + 1])
            index += 2
            continue
        if long_target_option and "=" in arg:
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
    normalized_args = [
        (
            "--target-directory" + (f"={arg.split('=', 1)[1]}" if "=" in arg else "")
            if arg.split("=", 1)[0].startswith("--t")
            and "--target-directory".startswith(arg.split("=", 1)[0])
            else arg
        )
        for arg in args
    ]
    operands = _positional_operands(
        normalized_args, options_with_values=options_with_values
    )
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
    elif subcommand == "apply":
        targets.extend(_option_paths(subargs, "--directory"))
    return targets, False


def _find_targets(argv: list[str]) -> tuple[list[str], bool]:
    args = argv[1:]
    ambiguous = any(
        arg in {"-exec", "-execdir", "-fprint0", "-ok", "-okdir"}
        for arg in args
    )
    return _find_output_paths(args), ambiguous


def _sed_targets(argv: list[str]) -> tuple[list[str], bool]:
    args = argv[1:]
    in_place = any(
        arg == "--in-place" or (arg.startswith("-") and "i" in arg[1:])
        for arg in args
    )
    if not in_place:
        return [], not _sed_is_read_only(args)
    return _sed_in_place_paths(args), False


def _action_capable_targets(argv: list[str]) -> tuple[list[str], bool]:
    policy = _command_policy(argv)
    return [], bool(policy and policy.read_only and not policy.read_only(argv))


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
    return _sed_is_read_only(argv[1:])


def _rg_read_only(argv: list[str]) -> bool:
    return _rg_is_read_only(argv[1:])


def _fd_read_only(argv: list[str]) -> bool:
    return _fd_is_read_only(argv[1:])


_COMMAND_POLICIES: dict[str, _CommandPolicy] = {
    name: _CommandPolicy(read_only=_always_read_only)
    for name in _ALWAYS_READ_ONLY_COMMANDS
}
_COMMAND_POLICIES.update({
    "awk": _CommandPolicy(read_only=_awk_read_only, targets=lambda _argv: ([], True)),
    "diff": _CommandPolicy(read_only=_diff_read_only, targets=_diff_targets),
    "fd": _CommandPolicy(read_only=_fd_read_only, targets=_action_capable_targets),
    "find": _CommandPolicy(read_only=_find_read_only, targets=_find_targets),
    "git": _CommandPolicy(
        read_only=_git_is_read_only,
        targets=_git_targets,
        privileged=_git_is_privileged,
    ),
    "rg": _CommandPolicy(read_only=_rg_read_only, targets=_action_capable_targets),
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

def _command_policy(argv: list[str]) -> Optional[_CommandPolicy]:
    raw_executable = argv[0]
    executable = os.path.basename(raw_executable).lower()
    if "/" in raw_executable or "\\" in raw_executable:
        return None
    return _COMMAND_POLICIES.get(executable)


def _policy_executable_is_trusted(argv: list[str], workspace: Path) -> bool:
    raw_executable = argv[0]
    if "/" in raw_executable or "\\" in raw_executable:
        return False
    search_path = os.environ.get("PATH", os.defpath)
    resolved: Optional[Path] = None
    for entry in search_path.split(os.pathsep):
        directory = Path(entry or ".").expanduser()
        if not directory.is_absolute():
            directory = workspace / directory
        candidate = Path(os.path.abspath(directory / raw_executable))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate
            break
    if resolved is None:
        return False
    lexical = resolved
    actual = lexical.resolve(strict=False)
    return (
        not _path_is_within(lexical, workspace)
        and not _path_is_within(actual, workspace)
        and actual.is_file()
    )


def _terminal_targets(
    args: dict[str, Any], task_context: str
) -> tuple[list[Path], bool]:
    command = str(args.get("command") or "")
    workdir = _terminal_workdir(args, task_context)
    commands = shell_command_argvs(command)
    raw_targets = shell_command_output_paths(command)
    ambiguous = _command_requires_explicit_policy(command) or (
        _command_uses_nonempty_ripgrep_config(command, commands)
    )
    for argv in commands:
        policy = _command_policy(argv)
        if policy is None:
            ambiguous = True
            continue
        if not _policy_executable_is_trusted(argv, workdir):
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
    if tool_name == "execute_code":
        return [_task_path(".", task_context)], False
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
    if tool_name == "terminal" and _is_privileged_worker_command(
        str(args.get("command") or ""), workspace
    ):
        return _block(
            "workers may not push, create branches, force-update refs, reset, "
            "deploy, or execute untrusted command paths"
        )
    if not task.project_id or any(
        not governance or governance.get("project_id") != task.project_id
        for governance in governances
    ):
        return _block("worker mutation does not match the card project")
    if tool_name == "execute_code":
        return _block("workers may not execute arbitrary code")
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
    worker_task_id = str(os.getenv("HERMES_KANBAN_TASK") or "").strip()
    task_context = str(task_id or session_id or "default")
    if normalized == "terminal":
        try:
            workdir = _terminal_workdir(call_args, task_context)
        except Exception as exc:
            return _block(f"mutation target could not be resolved: {exc}")
        if _is_read_only_terminal(
            str(call_args.get("command") or ""), workdir
        ):
            return None
    elif normalized not in _FILE_MUTATORS:
        return None

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
