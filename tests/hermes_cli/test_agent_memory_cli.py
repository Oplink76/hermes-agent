"""Strict JSON boundary tests for ``hermes agent-memory``."""

import argparse
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys

import pytest

from hermes_cli import main as main_module
from hermes_cli.subcommands import agent_memory as agent_memory_cli
from hermes_cli.subcommands.agent_memory import (
    _read_input,
    build_agent_memory_parser,
    cmd_agent_memory,
)


_SENTINEL = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"


@pytest.fixture(autouse=True)
def _isolate_agent_memory_environment(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_MEMORY_VAULT", raising=False)
    monkeypatch.delenv("HERMES_AGENT_MEMORY_OUTBOX", raising=False)


def _executor_payload():
    return {
        "agent_id": "codex",
        "model": "test-model",
        "surface": "codex-cli",
        "hermes_role": "developer",
        "execution_id": "exec-123",
        "responsibility": "writer",
        "version": 1,
    }


def _recall_payload():
    return {
        "operation_id": "recall-123",
        "task_id": "task-123",
        "run_id": 7,
        "delegation_id": "delegation-123",
        "function_id": "function-123",
        "title": "Agent Memory CLI",
        "query": "bounded memory work",
        "executor": _executor_payload(),
    }


def _write_payload():
    return {
        "operation_id": "write-123",
        "task_id": "task-123",
        "run_id": 7,
        "delegation_id": "delegation-123",
        "gist_id": "gist-123",
        "occurred_at": datetime(2026, 7, 19, 12, 0).isoformat(),
        "function_id": "function-123",
        "title": "Agent Memory CLI",
        "context": "bounded context",
        "summary": "bounded summary",
        "reused": "none",
        "result": "recorded",
        "maturity": "code_complete",
        "evidence": "focused tests",
        "behavior": "none",
        "decisions": "none",
        "open_loops": "none",
        "executor": _executor_payload(),
    }


def _parse(argv):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_agent_memory_parser(subparsers, cmd_agent_memory=cmd_agent_memory)
    return parser.parse_args(argv)


def _assert_rejected(args, capsys, *, request_text):
    assert cmd_agent_memory(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert _SENTINEL not in captured.err
    assert request_text not in captured.err


def _assert_main_syntax_rejected(monkeypatch, capsys, argv, *, request_text):
    monkeypatch.setattr(sys, "argv", ["hermes", *argv])

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["agent-memory: invalid arguments"]
    assert _SENTINEL not in captured.err
    assert request_text not in captured.err


def test_missing_action_uses_one_scoped_redacted_argparse_error(monkeypatch, capsys):
    _assert_main_syntax_rejected(
        monkeypatch,
        capsys,
        ["agent-memory"],
        request_text="agent_memory_action",
    )


def test_unknown_action_uses_one_scoped_redacted_argparse_error(monkeypatch, capsys):
    malformed = f"unknown-{_SENTINEL}"
    _assert_main_syntax_rejected(
        monkeypatch,
        capsys,
        ["agent-memory", malformed],
        request_text=malformed,
    )


def test_unknown_option_before_action_uses_one_scoped_redacted_argparse_error(
    monkeypatch, capsys
):
    malformed = f"--private-{_SENTINEL}"
    _assert_main_syntax_rejected(
        monkeypatch,
        capsys,
        ["agent-memory", malformed, "recall"],
        request_text=malformed,
    )


def test_unknown_child_option_uses_one_scoped_redacted_argparse_error(
    monkeypatch, capsys
):
    malformed = f"--private-{_SENTINEL}"
    _assert_main_syntax_rejected(
        monkeypatch,
        capsys,
        ["agent-memory", "recall", malformed],
        request_text=malformed,
    )


def test_recall_reads_stdin_and_returns_receipt(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_recall_payload())))
    args = _parse(["agent-memory", "recall", "--input", "-"])

    assert cmd_agent_memory(args) == 0

    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"matches", "receipt"}
    assert result["receipt"]["continue_work"] is True


def test_invalid_json_returns_2_and_one_redacted_error(monkeypatch, capsys):
    request_text = f"{{not-json {_SENTINEL}}}"
    monkeypatch.setattr(sys, "stdin", io.StringIO(request_text))

    _assert_rejected(
        _parse(["agent-memory", "recall", "--input", "-"]),
        capsys,
        request_text=request_text,
    )


def test_unknown_request_key_returns_2_without_echoing_payload(monkeypatch, capsys):
    payload = {**_recall_payload(), "private": _SENTINEL}
    request_text = json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(request_text))

    _assert_rejected(
        _parse(["agent-memory", "recall", "--input", "-"]),
        capsys,
        request_text=request_text,
    )


def test_payload_at_65536_bytes_is_accepted(monkeypatch, capsys):
    encoded = json.dumps(_recall_payload()).encode("utf-8")
    request_text = encoded.decode("utf-8") + " " * (65_536 - len(encoded))
    assert len(request_text.encode("utf-8")) == 65_536
    monkeypatch.setattr(sys, "stdin", io.StringIO(request_text))

    assert cmd_agent_memory(_parse(["agent-memory", "recall"])) == 0
    assert json.loads(capsys.readouterr().out)["receipt"]["status"] == "disabled"


def test_payload_at_65537_bytes_returns_2(monkeypatch, capsys):
    encoded = json.dumps(_recall_payload()).encode("utf-8")
    request_text = encoded.decode("utf-8") + " " * (65_537 - len(encoded))
    assert len(request_text.encode("utf-8")) == 65_537
    monkeypatch.setattr(sys, "stdin", io.StringIO(request_text))

    _assert_rejected(
        _parse(["agent-memory", "recall", "--input", "-"]),
        capsys,
        request_text=request_text,
    )


def test_relative_input_file_is_rejected(capsys):
    _assert_rejected(
        _parse(["agent-memory", "recall", "--input", "request.json"]),
        capsys,
        request_text="request.json",
    )


def test_absolute_regular_input_file_is_accepted(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_recall_payload()), encoding="utf-8")

    assert cmd_agent_memory(
        _parse(["agent-memory", "recall", "--input", str(request)])
    ) == 0

    assert json.loads(capsys.readouterr().out)["receipt"]["status"] == "disabled"


def test_absolute_symlink_input_is_rejected_without_content_echo(tmp_path, capsys):
    payload = {**_recall_payload(), "query": _SENTINEL}
    request_text = json.dumps(payload)
    target = tmp_path / "request-target.json"
    target.write_text(request_text, encoding="utf-8")
    symlink = tmp_path / "request-link.json"
    symlink.symlink_to(target)

    _assert_rejected(
        _parse(["agent-memory", "recall", "--input", str(symlink)]),
        capsys,
        request_text=request_text,
    )


def test_file_input_reads_the_validated_open_fd_when_path_is_replaced(
    tmp_path, monkeypatch
):
    request = tmp_path / "request.json"
    original_payload = _recall_payload()
    replacement_payload = {**_recall_payload(), "operation_id": "recall-replaced"}
    request.write_text(json.dumps(original_payload), encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(replacement_payload), encoding="utf-8")
    real_is_file = Path.is_file
    real_os_open = os.open
    state = {"swapped": False, "flags": None}

    def replace_path() -> None:
        if state["swapped"]:
            return
        request.unlink()
        request.symlink_to(replacement)
        state["swapped"] = True

    def swap_after_legacy_validation(self):
        result = real_is_file(self)
        if self == request and result:
            replace_path()
        return result

    def swap_after_open(path, flags, mode=0o777):
        descriptor = real_os_open(path, flags, mode)
        if Path(path) == request:
            state["flags"] = flags
            replace_path()
        return descriptor

    monkeypatch.setattr(Path, "is_file", swap_after_legacy_validation)
    monkeypatch.setattr(agent_memory_cli, "os", os, raising=False)
    monkeypatch.setattr(agent_memory_cli.os, "open", swap_after_open)

    assert _read_input(str(request)) == original_payload
    if hasattr(os, "O_NOFOLLOW"):
        assert state["flags"] & os.O_NOFOLLOW


def test_unconfigured_recall_returns_empty_disabled_receipt(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_recall_payload())))

    assert cmd_agent_memory(_parse(["agent-memory", "recall"])) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {"matches": [], "receipt": result["receipt"]}
    assert result["receipt"]["status"] == "disabled"
    assert result["receipt"]["continue_work"] is True


def test_status_returns_counts_and_never_content(capsys):
    assert cmd_agent_memory(_parse(["agent-memory", "status"])) == 0

    result = json.loads(capsys.readouterr().out)
    assert set(result) == {
        "attention_required", "enabled", "fingerprint", "notify_ole",
        "oldest_pending_hours", "pending", "reason", "vault_available",
    }
    assert result["pending"] == 0
    rendered = json.dumps(result)
    for private_value in (_SENTINEL, "bounded memory work", "bounded summary"):
        assert private_value not in rendered


def test_reconcile_returns_only_bounded_operational_counts(capsys):
    assert cmd_agent_memory(_parse(["agent-memory", "reconcile"])) == 0

    result = json.loads(capsys.readouterr().out)
    assert set(result) == {
        "closed_incidents", "corrupt", "moved", "pending", "vault_available",
    }
    assert result["pending"] == 0
    assert _SENTINEL not in json.dumps(result)


def test_write_returns_receipt(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_write_payload())))

    assert cmd_agent_memory(_parse(["agent-memory", "write"])) == 0

    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"receipt"}
    assert result["receipt"]["continue_work"] is True


def test_unconfigured_write_returns_disabled_and_creates_no_outbox(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_write_payload())))

    assert cmd_agent_memory(_parse(["agent-memory", "write"])) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["receipt"]["status"] == "disabled"
    assert not (home / "recovery" / "agent-memory-outbox").exists()
