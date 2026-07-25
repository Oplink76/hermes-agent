"""Primary-turn adapters for the three reserved local agent providers.

Claude and Codex run their own native autonomous loops in the active project
directory. Cowork is invoked through the existing generic MCP registry. None
of these paths exposes the external agent's internal tool calls as Hermes
tools; Hermes receives only the final assistant text.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from contextvars import copy_context
from pathlib import Path
from typing import Any, Callable

from agent.cli_emulated_provider import (
    CliCancelledError,
    CliConfigurationError,
    CliInvocationError,
    CliProcessError,
    CliTimeoutError,
    _executable_for,
    _parse_output,
    _probe_capability,
    _render_messages,
    _run_process,
)
from tools.mcp_tool import discover_mcp_tools
from tools.registry import registry


COWORK_TOOL_NAME = "mcp__cowork_mcp__cowork_run"
_DEFAULT_TIMEOUTS = {
    "claude-cli": 600.0,
    "codex-cli": 900.0,
    "cowork": 900.0,
}
_ACTING_BACKENDS: dict[str, dict[str, Any]] = {
    "claude-cli": {
        "provider": "claude-cli",
        "command": "claude",
        "required_help": (
            "--print",
            "--output-format",
            "--no-session-persistence",
            "--permission-mode",
        ),
    },
    "codex-cli": {
        "provider": "codex-cli",
        "command": "codex",
        "required_help": (
            "--json",
            "--ephemeral",
            "--sandbox",
            "--ask-for-approval",
            "--skip-git-repo-check",
            "--color",
        ),
    },
}


class LocalAgentInvocationError(RuntimeError):
    """A safe public failure from a primary local-agent invocation."""


def _provider_config(provider: str) -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        providers = (load_config() or {}).get("providers") or {}
        block = providers.get(provider)
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _provider_enabled(provider: str) -> bool:
    try:
        from hermes_cli.config import is_provider_enabled

        return is_provider_enabled(_provider_config(provider))
    except Exception:
        return False


def provider_timeout(provider: str) -> float:
    """Return a finite per-provider timeout without introducing env config."""
    block = _provider_config(provider)
    raw = block.get("timeout", block.get("request_timeout_seconds"))
    if raw is None:
        return _DEFAULT_TIMEOUTS[provider]
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise LocalAgentInvocationError(
            f"providers.{provider}.timeout must be a positive number"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise LocalAgentInvocationError(
            f"providers.{provider}.timeout must be a positive number"
        )
    return value


def _acting_argv(executable: str, provider: str, model: str) -> list[str]:
    if provider == "claude-cli":
        argv = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            "bypassPermissions",
        ]
        if model and model != "default":
            argv.extend(["--model", model])
        return argv

    argv = [
        executable,
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-",
    ]
    if model and model != "default":
        argv[-1:-1] = ["--model", model]
    return argv


def run_cli_acting(
    *,
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    cwd: str,
    timeout: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Run one native Claude/Codex acting turn using bounded subprocess IO."""
    selected = _ACTING_BACKENDS.get(provider)
    if selected is None:
        raise CliConfigurationError(f"Unsupported primary CLI provider: {provider}")
    if not _provider_enabled(provider):
        raise CliConfigurationError(f"{provider} provider is disabled")
    project_cwd = str(Path(cwd).resolve())
    if not Path(project_cwd).is_dir():
        raise CliConfigurationError(f"Active project directory does not exist: {project_cwd}")
    prompt = _render_messages(messages)
    effective_timeout = float(timeout) if timeout is not None else provider_timeout(provider)
    deadline = time.monotonic() + max(0.01, effective_timeout)
    executable = _executable_for(selected)
    _probe_capability(
        executable,
        selected,
        cancel_check,
        timeout=max(0.01, deadline - time.monotonic()),
    )
    if cancel_check is not None and cancel_check():
        raise CliCancelledError(f"{provider} invocation cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CliTimeoutError(f"{provider} invocation timed out")
    returncode, stdout, stderr = _run_process(
        _acting_argv(executable, provider, model),
        prompt=prompt,
        cwd=project_cwd,
        timeout=remaining,
        cancel_check=cancel_check,
    )
    if returncode != 0:
        raise CliProcessError(
            f"{provider} invocation failed",
            stderr_tail=stderr[-4096:],
        )
    return _parse_output(selected, stdout)


def _dispatch_cowork(
    messages: list[dict[str, Any]], cwd: str
) -> str:
    discover_mcp_tools()
    definitions = registry.get_definitions({COWORK_TOOL_NAME}, quiet=True)
    if not definitions:
        raise LocalAgentInvocationError(
            "Cowork MCP tool is unavailable; configure and enable the "
            "'cowork-mcp' server with its 'cowork_run' tool"
        )
    raw = registry.dispatch(
        COWORK_TOOL_NAME,
        {"prompt": _render_messages(messages), "cwd": cwd},
    )
    if not isinstance(raw, str):
        raise LocalAgentInvocationError("Cowork MCP returned a malformed result")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LocalAgentInvocationError("Cowork MCP returned a malformed result") from exc
    if not isinstance(payload, dict):
        raise LocalAgentInvocationError("Cowork MCP returned a malformed result")
    error = payload.get("error")
    if error:
        raise LocalAgentInvocationError(f"Cowork MCP failed: {error}")
    if "result" not in payload:
        raise LocalAgentInvocationError("Cowork MCP result is missing final text")
    result = payload.get("result")
    if not isinstance(result, str):
        raise LocalAgentInvocationError("Cowork MCP returned a malformed result")
    if not result.strip():
        raise LocalAgentInvocationError("Cowork MCP returned an empty final result")
    return result


def run_cowork(
    *,
    messages: list[dict[str, Any]],
    cwd: str,
    timeout: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> str:
    """Invoke Cowork through the generic registry with a bounded local wait.

    MCP does not expose a portable remote-run cancellation operation. On
    timeout or cancellation Hermes stops waiting, while the remote Cowork run
    may continue until the MCP server's own timeout.
    """
    if not _provider_enabled("cowork"):
        raise LocalAgentInvocationError("cowork provider is disabled")
    project_cwd = str(Path(cwd).resolve())
    if not Path(project_cwd).is_dir():
        raise LocalAgentInvocationError(
            f"Active project directory does not exist: {project_cwd}"
        )
    effective_timeout = float(timeout) if timeout is not None else provider_timeout("cowork")
    deadline = time.monotonic() + max(0.01, effective_timeout)
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put((True, _dispatch_cowork(messages, project_cwd)))
        except Exception as exc:  # thread boundary: propagate on owner thread
            result_queue.put((False, exc))

    worker_context = copy_context()
    worker = threading.Thread(
        target=lambda: worker_context.run(invoke),
        name="hermes-cowork-primary",
        daemon=True,
    )
    worker.start()
    while True:
        if cancel_check is not None and cancel_check():
            raise LocalAgentInvocationError(
                "Cowork invocation cancelled; the remote run may still continue"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LocalAgentInvocationError(
                "Cowork invocation timed out; the remote run may still continue"
            )
        try:
            ok, value = result_queue.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if ok:
            return str(value)
        if isinstance(value, LocalAgentInvocationError):
            raise value
        raise LocalAgentInvocationError(f"Cowork MCP invocation failed: {value}") from value


def _turn_messages(
    messages: list[dict[str, Any]], active_system_prompt: str
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    if active_system_prompt:
        projected.append({"role": "system", "content": active_system_prompt})
    for message in messages:
        if not isinstance(message, dict):
            continue
        item = {
            key: value
            for key, value in message.items()
            if key in {"role", "content", "tool_calls", "tool_call_id"}
        }
        api_content = message.get("api_content")
        if isinstance(api_content, str):
            item["content"] = api_content
        projected.append(item)
    return projected


def _active_cwd(agent: Any) -> str:
    session_cwd = getattr(agent, "session_cwd", None)
    if session_cwd:
        return str(session_cwd)
    from agent.runtime_cwd import resolve_agent_cwd

    return str(resolve_agent_cwd())


def run_local_agent_turn(
    agent: Any,
    *,
    messages: list[dict[str, Any]],
    active_system_prompt: str,
    conversation_history: list[dict[str, Any]],
    effective_task_id: str,
    turn_id: str,
    user_message: Any,
    original_user_message: Any,
    should_review_memory: bool,
) -> dict[str, Any]:
    """Own and finalize one primary local-agent turn before HTTP dispatch."""
    provider = str(agent.provider)
    prompt_messages = _turn_messages(messages, active_system_prompt)
    cancel_check = lambda: bool(getattr(agent, "_interrupt_requested", False))
    failed = False
    interrupted = False
    try:
        if provider in {"claude-cli", "codex-cli"}:
            final_response = run_cli_acting(
                provider=provider,
                model=str(agent.model or "default"),
                messages=prompt_messages,
                cwd=_active_cwd(agent),
                timeout=provider_timeout(provider),
                cancel_check=cancel_check,
            )
        elif provider == "cowork":
            final_response = run_cowork(
                messages=prompt_messages,
                cwd=_active_cwd(agent),
                timeout=provider_timeout("cowork"),
                cancel_check=cancel_check,
            )
        else:
            raise LocalAgentInvocationError(f"Unsupported local agent provider: {provider}")
        agent._turn_received_provider_response = True
    except (CliInvocationError, LocalAgentInvocationError) as exc:
        failed = True
        interrupted = bool(getattr(agent, "_interrupt_requested", False))
        final_response = f"{provider} primary agent failed: {exc}"

    from agent.turn_finalizer import finalize_turn

    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=1,
        interrupted=interrupted,
        failed=failed,
        messages=messages,
        conversation_history=conversation_history,
        effective_task_id=effective_task_id,
        turn_id=turn_id,
        user_message=user_message,
        original_user_message=original_user_message,
        _should_review_memory=should_review_memory,
        _turn_exit_reason=(
            f"text_response({provider})"
            if not failed
            else f"local_agent_error({provider})"
        ),
    )
