"""Real-import MoA integration for the two closed CLI backends."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _write_fake_clis(tmp_path: Path) -> tuple[Path, Path]:
    claude_record = tmp_path / "claude-record.jsonl"
    claude = tmp_path / "claude"
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"record = pathlib.Path({str(claude_record)!r})\n"
        "if '--help' in sys.argv:\n"
        "    print('--print --output-format --no-session-persistence --safe-mode --tools')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "with record.open('a') as f: f.write(json.dumps({'argv': sys.argv[1:], 'stdin': prompt}) + '\\n')\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'Claude advice'}))\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)

    codex_record = tmp_path / "codex-record.jsonl"
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"record = pathlib.Path({str(codex_record)!r})\n"
        "if '--help' in sys.argv:\n"
        "    print('--json --ephemeral --ignore-user-config --ignore-rules --sandbox --ask-for-approval')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "with record.open('a') as f: f.write(json.dumps({'argv': sys.argv[1:], 'stdin': prompt}) + '\\n')\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Codex synthesis'}}))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    return claude_record, codex_record


def test_moa_routes_reference_and_aggregator_through_cli_backends(monkeypatch, tmp_path):
    from agent import cli_emulated_provider
    from agent.moa_loop import aggregate_moa_context

    claude_record, codex_record = _write_fake_clis(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "providers:\n  codex-cli:\n    allow_agentic_advisor: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    cli_emulated_provider._CAPABILITY_CACHE.clear()

    guidance = aggregate_moa_context(
        user_prompt="What should happen next?",
        api_messages=[{"role": "user", "content": "What should happen next?"}],
        reference_models=[{"provider": "claude-cli", "model": "default"}],
        aggregator={"provider": "codex-cli", "model": "default"},
    )

    assert "Codex synthesis" in guidance
    assert len(claude_record.read_text(encoding="utf-8").splitlines()) == 1
    codex_calls = codex_record.read_text(encoding="utf-8").splitlines()
    assert len(codex_calls) == 1
    codex_call = json.loads(codex_calls[0])
    assert "Claude advice" in codex_call["stdin"]


def test_moa_facade_uses_cli_aggregator_without_forwarding_hermes_tools(
    tmp_path: Path, monkeypatch
) -> None:
    claude_record, codex_record = _write_fake_clis(tmp_path)
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        """
providers:
  codex-cli:
    allow_agentic_advisor: true
moa:
  default_preset: cli
  presets:
    cli:
      reference_models:
        - provider: claude-cli
          model: default
      aggregator:
        provider: codex-cli
        model: default
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    from agent.cli_emulated_provider import _CAPABILITY_CACHE
    from agent.moa_loop import MoAChatCompletions

    _CAPABILITY_CACHE.clear()
    stream = MoAChatCompletions("cli").create(
        messages=[{"role": "user", "content": "Need a concise answer."}],
        stream=True,
        stream_options={"include_usage": True},
        tools=[
            {
                "type": "function",
                "function": {"name": "should_not_reach_cli", "parameters": {}},
            }
        ],
    )

    chunks = list(stream)
    assert chunks[0].choices[0].delta.content == "Codex synthesis"
    assert chunks[0].choices[0].finish_reason == "stop"
    assert chunks[-1].usage is None
    assert len(claude_record.read_text().splitlines()) == 1
    assert len(codex_record.read_text().splitlines()) == 1
