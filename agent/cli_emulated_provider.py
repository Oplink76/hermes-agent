"""Bounded CLI completions for MoA-only external-process providers.

This module intentionally supports exactly two private routes.  It is not a
public provider or subprocess extension surface.
"""

from __future__ import annotations

import html
import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, Callable

from agent.redact import redact_sensitive_text
from cli_emulated_routes import CLI_EMULATED_ROUTES


class CliInvocationError(RuntimeError):
    """Safe public error for a failed CLI completion."""

    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(message)
        self._stderr_tail = stderr_tail


class CliConfigurationError(CliInvocationError):
    """The requested private CLI route or payload is unsupported."""


class CliCapabilityError(CliInvocationError):
    """The installed CLI cannot satisfy the frozen safety contract."""


class CliTimeoutError(CliInvocationError):
    """The bounded CLI invocation exceeded its deadline."""


class CliCancelledError(CliInvocationError):
    """The owner cancelled the CLI invocation."""


class CliOutputError(CliInvocationError):
    """The CLI returned malformed, failed, or incomplete output."""


class CliProcessError(CliInvocationError):
    """The CLI process exited unsuccessfully."""


_BACKENDS: dict[str, dict[str, Any]] = {
    CLI_EMULATED_ROUTES["claude-cli"]: {
        "provider": "claude-cli",
        "command": "claude",
        "required_help": (
            "--print",
            "--output-format",
            "--no-session-persistence",
            "--safe-mode",
            "--tools",
        ),
    },
    CLI_EMULATED_ROUTES["codex-cli"]: {
        "provider": "codex-cli",
        "command": "codex",
        "required_help": (
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "--ask-for-approval",
        ),
    },
}

_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
}
_WINDOWS_ENV_ALLOWLIST = {
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "OS",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "USERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
}
_CAPABILITY_CACHE: set[tuple[str, int, int, str]] = set()
_STDOUT_LIMIT_BYTES = 64 * 1024
_STDERR_TAIL_BYTES = 64 * 1024
_PROCESS_POLL_SECONDS = 0.05
_CAPABILITY_TIMEOUT_SECONDS = 10.0
_DEFAULT_TIMEOUT_SECONDS = {"claude-cli": 300.0, "codex-cli": 600.0}


def _safe_env(*, _is_windows: bool | None = None) -> dict[str, str]:
    is_windows = os.name == "nt" if _is_windows is None else _is_windows
    allowed = _ENV_ALLOWLIST | (_WINDOWS_ENV_ALLOWLIST if is_windows else set())
    return {key: value for key, value in os.environ.items() if key in allowed}


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                raise CliConfigurationError("CLI completions accept text-only message payloads")
            text = part.get("text")
            if text is not None and not isinstance(text, str):
                raise CliConfigurationError(
                    "CLI completions accept text-only message payloads"
                )
            parts.append(text or "")
        return "".join(parts)
    if content is None:
        return ""
    raise CliConfigurationError("CLI completions accept text-only message payloads")


def _render_messages(messages: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for message in messages:
        text = _flatten_content(message.get("content"))
        tool_calls = message.get("tool_calls")
        if not text and not tool_calls:
            continue
        role = html.escape(str(message.get("role") or "user"), quote=True)
        fields = [f'<message role="{role}">', f"<content>{html.escape(text)}</content>"]
        if tool_calls:
            try:
                serialized_calls = json.dumps(
                    tool_calls, ensure_ascii=False, sort_keys=True
                )
            except (TypeError, ValueError) as exc:
                raise CliConfigurationError(
                    "CLI completions require serializable tool-call history"
                ) from exc
            fields.append(
                f"<tool-calls>{html.escape(serialized_calls)}</tool-calls>"
            )
        if message.get("tool_call_id") is not None:
            fields.append(
                f"<tool-call-id>{html.escape(str(message['tool_call_id']))}</tool-call-id>"
            )
        fields.append("</message>")
        rendered.append("".join(fields))
    if not rendered:
        raise CliConfigurationError("CLI completion requires at least one text message")
    return (
        "<hermes-conversation>\n"
        + "\n".join(rendered)
        + "\n</hermes-conversation>\n"
        "Respond as the assistant to the conversation."
    )


def _backend(provider: str, base_url: str) -> dict[str, Any]:
    backend = _BACKENDS.get(str(base_url or "").rstrip("/"))
    if backend is None or backend["provider"] != str(provider or "").strip().lower():
        raise CliConfigurationError("Unsupported CLI completion route")
    return backend


def _codex_agentic_advisor_enabled() -> bool:
    """Return the explicit Codex advisor opt-in; missing/invalid config is off."""
    try:
        from hermes_cli.config import load_config

        providers = (load_config() or {}).get("providers") or {}
        codex = providers.get("codex-cli") or {}
        return codex.get("allow_agentic_advisor") is True
    except Exception:
        return False


def _cli_provider_enabled(provider: str) -> bool:
    """Honor the canonical providers.<id>.enabled gate; invalid config is off."""
    try:
        from hermes_cli.config import is_provider_enabled, load_config

        providers = (load_config() or {}).get("providers") or {}
        block = providers.get(provider)
        return not isinstance(block, dict) or is_provider_enabled(block)
    except Exception:
        return False


def _executable_for(backend: dict[str, Any]) -> str:
    command = str(backend["command"])
    executable = shutil.which(command)
    if not executable:
        raise CliCapabilityError(f"{backend['provider']} command is not installed")
    return executable


def _popen_group_kwargs() -> dict[str, Any]:
    from hermes_cli._subprocess_compat import (
        IS_WINDOWS,
        windows_detach_flags_without_breakaway,
    )

    if IS_WINDOWS:
        return {"creationflags": windows_detach_flags_without_breakaway()}
    return {"start_new_session": True}


def _run_capability_command(
    command: list[str],
    *,
    cwd: str,
    provider: str,
    cancel_check: Callable[[], bool] | None,
    timeout: float,
) -> str:
    if cancel_check is not None and cancel_check():
        raise CliCancelledError(f"{provider} invocation cancelled")
    try:
        returncode, stdout, stderr = _run_process(
            command,
            prompt="",
            cwd=cwd,
            timeout=timeout,
            cancel_check=cancel_check,
        )
    except (CliCancelledError, CliTimeoutError):
        raise
    except CliInvocationError as exc:
        raise CliCapabilityError(f"Could not verify {provider} capabilities") from exc
    if returncode != 0:
        raise CliCapabilityError(f"Could not verify {provider} capabilities")
    return f"{stdout}\n{stderr}"


def _probe_capability(
    executable: str,
    backend: dict[str, Any],
    cancel_check: Callable[[], bool] | None,
    timeout: float | None = None,
) -> None:
    try:
        stat_result = os.stat(executable)
    except OSError as exc:
        raise CliCapabilityError(f"Cannot inspect {backend['provider']} command") from exc
    key = (
        executable,
        int(stat_result.st_mtime_ns),
        int(stat_result.st_size),
        str(backend["provider"]),
    )
    if key in _CAPABILITY_CACHE:
        return
    probe_commands = (
        [[executable, "--help"], [executable, "exec", "--help"]]
        if backend["provider"] == "codex-cli"
        else [[executable, "--help"]]
    )
    probe_deadline = time.monotonic() + min(
        _CAPABILITY_TIMEOUT_SECONDS,
        timeout if timeout is not None else _CAPABILITY_TIMEOUT_SECONDS,
    )
    with tempfile.TemporaryDirectory(prefix="hermes-cli-probe-") as cwd:
        help_parts: list[str] = []
        for command in probe_commands:
            remaining = probe_deadline - time.monotonic()
            if remaining <= 0:
                raise CliCapabilityError(
                    f"Could not verify {backend['provider']} capabilities"
                )
            help_parts.append(
                _run_capability_command(
                    command,
                    cwd=cwd,
                    provider=str(backend["provider"]),
                    cancel_check=cancel_check,
                    timeout=remaining,
                )
            )
        help_text = "\n".join(help_parts)
    if any(flag not in help_text for flag in backend["required_help"]):
        raise CliCapabilityError(
            f"Installed {backend['provider']} lacks required safe-mode flags"
        )
    _CAPABILITY_CACHE.add(key)


def _argv(executable: str, backend: dict[str, Any], model: str) -> list[str]:
    if backend["provider"] == "claude-cli":
        argv = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--safe-mode",
            "--tools",
            "",
        ]
    else:
        argv = [
            executable,
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-",
        ]
    if model and model != "default":
        insert_at = -1 if backend["provider"] == "codex-cli" else len(argv)
        argv[insert_at:insert_at] = ["--model", model]
    return argv


def _parse_output(backend: dict[str, Any], stdout: str) -> str:
    try:
        if backend["provider"] == "claude-cli":
            payload = json.loads(stdout)
            if not isinstance(payload, dict):
                raise TypeError("claude-cli output must be an object")
            if payload.get("type") != "result" or payload.get("is_error") is not False:
                raise CliOutputError("claude-cli returned an unsuccessful result")
            text = payload.get("result")
            if isinstance(text, str) and text.strip():
                return text
        else:
            answer = ""
            completed = False
            for line in stdout.splitlines():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise TypeError("codex-cli event must be an object")
                event_type = str(event.get("type") or "")
                if completed:
                    raise CliOutputError("codex-cli emitted an event after completion")
                if event_type in {"error", "turn.failed", "turn.cancelled"}:
                    raise CliOutputError("codex-cli returned an unsuccessful result")
                if event_type == "turn.completed":
                    completed = True
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    raise TypeError("codex-cli item must be an object")
                if event_type == "item.completed" and item.get("type") == "agent_message":
                    text = item.get("text")
                    if not isinstance(text, str):
                        raise CliOutputError("Malformed Codex agent message output")
                    answer = text
            if answer.strip() and completed:
                return answer
            if answer.strip():
                raise CliOutputError("codex-cli returned an incomplete result")
    except CliOutputError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CliOutputError(f"Malformed {backend['provider']} output") from exc
    raise CliOutputError(f"{backend['provider']} returned no completion text")


def _terminate_process_tree(
    process: subprocess.Popen[Any],
    *,
    _is_windows: bool | None = None,
) -> None:
    """Best-effort termination of the invocation's process group."""
    is_windows = os.name == "nt" if _is_windows is None else _is_windows
    if is_windows:
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags

            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=windows_hide_flags(),
                timeout=5,
                check=False,
            )
            if result.returncode != 0 and process.poll() is None:
                process.kill()
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        group_deadline = time.monotonic() + 0.5
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=max(0.0, group_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass

        while time.monotonic() < group_deadline:
            try:
                os.killpg(process.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(_PROCESS_POLL_SECONDS)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _run_process(
    argv: list[str],
    *,
    prompt: str,
    cwd: str,
    timeout: float,
    cancel_check: Callable[[], bool] | None,
) -> tuple[int, str, str]:
    """Run one CLI with file-backed stdio so captured memory stays bounded."""
    if cancel_check is not None and cancel_check():
        raise CliCancelledError("CLI invocation cancelled")
    deadline = time.monotonic() + max(0.01, float(timeout))
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CliConfigurationError("CLI prompt is not valid UTF-8 text") from exc
    with (
        tempfile.TemporaryFile() as stdin_file,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        if stdin_file.write(prompt_bytes) != len(prompt_bytes):
            raise CliProcessError("Could not prepare CLI prompt")
        stdin_file.seek(0)
        try:
            process = subprocess.Popen(
                argv,
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                env=_safe_env(),
                cwd=cwd,
                close_fds=True,
                **_popen_group_kwargs(),
            )
        except OSError as exc:
            raise CliProcessError("Could not start CLI command") from exc

        failure: str | None = None
        try:
            while True:
                now = time.monotonic()
                if cancel_check is not None and cancel_check():
                    failure = "cancelled"
                    break
                if now >= deadline:
                    failure = "timed out"
                    break
                if (
                    os.fstat(stdout_file.fileno()).st_size > _STDOUT_LIMIT_BYTES
                    or os.fstat(stderr_file.fileno()).st_size > _STDERR_TAIL_BYTES
                ):
                    failure = "output limit exceeded"
                    break
                if process.poll() is not None:
                    break
                time.sleep(min(_PROCESS_POLL_SECONDS, deadline - now))
        finally:
            _terminate_process_tree(process)

        if (
            os.fstat(stdout_file.fileno()).st_size > _STDOUT_LIMIT_BYTES
            or os.fstat(stderr_file.fileno()).st_size > _STDERR_TAIL_BYTES
        ):
            failure = "output limit exceeded"
        if failure == "cancelled":
            raise CliCancelledError("CLI invocation cancelled")
        if failure == "timed out":
            raise CliTimeoutError("CLI invocation timed out")
        if failure is not None:
            raise CliOutputError(f"CLI invocation {failure}")

        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(_STDOUT_LIMIT_BYTES)
        stderr = stderr_file.read(_STDERR_TAIL_BYTES)
        return (
            int(process.returncode or 0),
            stdout.decode("utf-8", "replace"),
            redact_sensitive_text(stderr.decode("utf-8", "replace")),
        )


def _response(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                finish_reason="stop",
                message=SimpleNamespace(content=text, role="assistant", tool_calls=None),
            )
        ],
        usage=None,
    )


class _SyntheticStream:
    """Closeable iterator over a completion produced before iteration starts."""

    def __init__(self, text: str) -> None:
        self._chunks = iter(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            index=0,
                            finish_reason="stop",
                            delta=SimpleNamespace(
                                content=text,
                                role="assistant",
                                tool_calls=None,
                            ),
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(choices=[], usage=None),
            ]
        )

    def __iter__(self) -> "_SyntheticStream":
        return self

    def __next__(self) -> Any:
        return next(self._chunks)

    def close(self) -> None:
        return None


def create_cli_completion(
    *,
    provider: str,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    stream: bool = False,
    timeout: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Any:
    """Run one MoA-only CLI completion and return an OpenAI-shaped result."""
    selected = _backend(provider, base_url)
    if cancel_check is not None and cancel_check():
        raise CliCancelledError(f"{selected['provider']} invocation cancelled")
    prompt = _render_messages(messages)
    if not _cli_provider_enabled(str(selected["provider"])):
        raise CliConfigurationError(f"{selected['provider']} provider is disabled")
    if selected["provider"] == "codex-cli" and not _codex_agentic_advisor_enabled():
        raise CliConfigurationError(
            "codex-cli requires explicit opt-in at "
            "providers.codex-cli.allow_agentic_advisor"
        )
    effective_timeout = (
        float(timeout)
        if timeout is not None
        else _DEFAULT_TIMEOUT_SECONDS[selected["provider"]]
    )
    deadline = time.monotonic() + max(0.01, effective_timeout)
    executable = _executable_for(selected)
    _probe_capability(
        executable,
        selected,
        cancel_check,
        timeout=max(0.01, deadline - time.monotonic()),
    )
    if cancel_check is not None and cancel_check():
        raise CliCancelledError(f"{selected['provider']} invocation cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CliTimeoutError(f"{selected['provider']} invocation timed out")
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-cli-completion-") as cwd:
            returncode, stdout, stderr = _run_process(
                _argv(executable, selected, model),
                prompt=prompt,
                cwd=cwd,
                timeout=remaining,
                cancel_check=cancel_check,
            )
    except CliInvocationError:
        raise
    except OSError as exc:
        raise CliProcessError(f"Could not start {selected['provider']} command") from exc
    if returncode != 0:
        raise CliProcessError(
            f"{selected['provider']} invocation failed",
            stderr_tail=stderr[-4096:],
        )
    text = _parse_output(selected, stdout)
    return _SyntheticStream(text) if stream else _response(text)
