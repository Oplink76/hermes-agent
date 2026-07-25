"""Primary local-agent provider contracts (no live vendor calls)."""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
import time

import pytest


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", 0, -1])
def test_provider_timeout_must_be_positive_and_finite(
    monkeypatch: pytest.MonkeyPatch, value
) -> None:
    from agent.local_agent_provider import LocalAgentInvocationError, provider_timeout

    monkeypatch.setattr(
        "agent.local_agent_provider._provider_config",
        lambda _provider: {"timeout": value},
    )

    with pytest.raises(LocalAgentInvocationError, match="positive number"):
        provider_timeout("claude-cli")


def _write_acting_cli(tmp_path: Path, name: str, answer: str) -> tuple[Path, Path]:
    record = tmp_path / f"{name}-record.json"
    executable = tmp_path / name
    if name == "claude":
        output = json.dumps({"type": "result", "is_error": False, "result": answer})
    else:
        output = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": answer},
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            ]
        )
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"record = {str(record)!r}\n"
        "if '--help' in sys.argv:\n"
        "    print('--print --output-format --no-session-persistence "
        "--permission-mode --model --json --ephemeral --sandbox "
        "--ask-for-approval --skip-git-repo-check --color')\n"
        "    raise SystemExit(0)\n"
        "open(record, 'w').write(json.dumps({"
        "'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd()}))\n"
        f"print({output!r})\n"
    )
    executable.chmod(0o755)
    return executable, record


@pytest.mark.parametrize(
    ("provider", "command", "answer"),
    [
        ("claude-cli", "claude", "Claude acted"),
        ("codex-cli", "codex", "Codex acted"),
    ],
)
def test_primary_cli_uses_native_acting_loop_stdin_and_project_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    command: str,
    answer: str,
) -> None:
    from agent.local_agent_provider import run_cli_acting

    executable, record = _write_acting_cli(tmp_path, command, answer)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == command else None,
    )

    result = run_cli_acting(
        provider=provider,
        model="default",
        messages=[
            {"role": "system", "content": "Follow project instructions."},
            {"role": "user", "content": "Make the requested change."},
        ],
        cwd=str(project),
        timeout=5,
    )

    assert result == answer
    invocation = json.loads(record.read_text())
    assert invocation["cwd"] == str(project)
    assert "Make the requested change." in invocation["stdin"]
    assert "Follow project instructions." in invocation["stdin"]
    assert invocation["argv"][-1:] != ["Make the requested change."]
    if provider == "claude-cli":
        assert invocation["argv"] == [
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            "bypassPermissions",
        ]
    else:
        assert "--sandbox" in invocation["argv"]
        assert invocation["argv"][invocation["argv"].index("--sandbox") + 1] == "workspace-write"
        assert "--ignore-rules" not in invocation["argv"]
        assert "--ignore-user-config" not in invocation["argv"]
        assert invocation["argv"][-1] == "-"


def test_primary_cli_timeout_terminates_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.cli_emulated_provider import CliTimeoutError
    from agent.local_agent_provider import run_cli_acting

    executable = tmp_path / "claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "if '--help' in sys.argv:\n"
        "    print('--print --output-format --no-session-persistence --permission-mode')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == "claude" else None,
    )

    started = time.monotonic()
    with pytest.raises(CliTimeoutError, match="timed out"):
        run_cli_acting(
            provider="claude-cli",
            model="default",
            messages=[{"role": "user", "content": "Wait."}],
            cwd=str(tmp_path),
            timeout=0.1,
        )
    assert time.monotonic() - started < 3


def test_primary_cli_cancellation_terminates_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.cli_emulated_provider import CliCancelledError
    from agent.local_agent_provider import run_cli_acting

    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "if '--help' in sys.argv:\n"
        "    print('--json --ephemeral --sandbox --ask-for-approval "
        "--skip-git-repo-check --color')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(
        "agent.cli_emulated_provider.shutil.which",
        lambda name: str(executable) if name == "codex" else None,
    )
    started = time.monotonic()

    with pytest.raises(CliCancelledError, match="cancelled"):
        run_cli_acting(
            provider="codex-cli",
            model="default",
            messages=[{"role": "user", "content": "Wait."}],
            cwd=str(tmp_path),
            timeout=5,
            cancel_check=lambda: time.monotonic() - started > 0.1,
        )
    assert time.monotonic() - started < 3


def test_cowork_dispatches_normalized_generic_mcp_tool_with_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.local_agent_provider import run_cowork

    calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        "agent.local_agent_provider.discover_mcp_tools",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda names=None, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        lambda name, args: calls.append((name, args))
        or json.dumps({"result": "Cowork finished"}),
    )

    result = run_cowork(
        messages=[
            {"role": "system", "content": "Use installed finance skills."},
            {"role": "user", "content": "Build the model."},
        ],
        cwd=str(tmp_path),
        timeout=5,
    )

    assert result == "Cowork finished"
    assert len(calls) == 1
    assert calls[0][0] == "mcp__cowork_mcp__cowork_run"
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert "Use installed finance skills." in calls[0][1]["prompt"]
    assert "Build the model." in calls[0][1]["prompt"]


def test_cowork_uses_actual_generic_registry_dispatch_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent import local_agent_provider
    from tools.registry import ToolRegistry

    isolated_registry = ToolRegistry()
    received = {}

    def handler(args):
        received.update(args)
        return json.dumps({"result": "registry result"})

    isolated_registry.register(
        name="mcp__cowork_mcp__cowork_run",
        toolset="mcp-cowork-mcp",
        schema={
            "name": "mcp__cowork_mcp__cowork_run",
            "description": "Run Cowork",
            "parameters": {"type": "object"},
        },
        handler=handler,
    )
    monkeypatch.setattr(local_agent_provider, "registry", isolated_registry)
    monkeypatch.setattr(local_agent_provider, "discover_mcp_tools", lambda: None)

    result = local_agent_provider.run_cowork(
        messages=[{"role": "user", "content": "Use the finance skill."}],
        cwd=str(tmp_path),
        timeout=5,
    )

    assert result == "registry result"
    assert received["cwd"] == str(tmp_path)
    assert "Use the finance skill." in received["prompt"]


def test_cowork_worker_inherits_profile_and_session_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.local_agent_provider import run_cowork

    profile_context: ContextVar[str] = ContextVar(
        "test_cowork_profile", default="default"
    )
    profile_context.set("isolated-profile")
    observed: dict[str, str] = {}
    monkeypatch.setattr("agent.local_agent_provider.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda *_args, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )

    def dispatch(_name, _args):
        observed["profile"] = profile_context.get()
        return json.dumps({"result": "context preserved"})

    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        dispatch,
    )

    assert run_cowork(
        messages=[{"role": "user", "content": "Work."}],
        cwd=str(tmp_path),
        timeout=5,
    ) == "context preserved"
    assert observed == {"profile": "isolated-profile"}


def test_cowork_missing_tool_and_timeout_are_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.local_agent_provider import LocalAgentInvocationError, run_cowork

    monkeypatch.setattr("agent.local_agent_provider.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda *_args, **_kwargs: [],
    )
    with pytest.raises(LocalAgentInvocationError, match="tool is unavailable"):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=1,
        )

    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda *_args, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        lambda *_args, **_kwargs: time.sleep(2),
    )
    with pytest.raises(LocalAgentInvocationError, match="remote run may still continue"):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=0.05,
        )


def test_cowork_disabled_and_cancelled_fail_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.local_agent_provider import LocalAgentInvocationError, run_cowork

    monkeypatch.setattr(
        "agent.local_agent_provider._provider_enabled",
        lambda _provider: False,
    )
    with pytest.raises(LocalAgentInvocationError, match="provider is disabled"):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=1,
        )

    monkeypatch.setattr(
        "agent.local_agent_provider._provider_enabled",
        lambda _provider: True,
    )
    monkeypatch.setattr("agent.local_agent_provider.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda *_args, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        lambda *_args, **_kwargs: time.sleep(2),
    )
    with pytest.raises(LocalAgentInvocationError, match="remote run may still continue"):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=1,
            cancel_check=lambda: True,
        )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not-json", "malformed"),
        (json.dumps({}), "missing"),
        (json.dumps({"result": "  "}), "empty"),
        (json.dumps({"error": "remote failed"}), "remote failed"),
    ],
)
def test_cowork_rejects_bad_generic_mcp_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    match: str,
) -> None:
    from agent.local_agent_provider import LocalAgentInvocationError, run_cowork

    monkeypatch.setattr("agent.local_agent_provider.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.get_definitions",
        lambda names=None, **_kwargs: [
            {"function": {"name": "mcp__cowork_mcp__cowork_run"}}
        ],
    )
    monkeypatch.setattr(
        "agent.local_agent_provider.registry.dispatch",
        lambda _name, _args: payload,
    )

    with pytest.raises(LocalAgentInvocationError, match=match):
        run_cowork(
            messages=[{"role": "user", "content": "Work."}],
            cwd=str(tmp_path),
            timeout=5,
        )
