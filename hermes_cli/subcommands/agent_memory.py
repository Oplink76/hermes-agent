"""Strict JSON CLI boundary for governed worker memory."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Callable

from hermes_cli.agent_memory_protocol import (
    WorkerRecallRequest,
    WorkerWriteRequest,
    configured_outbox_status,
    recall_for_worker,
    reconcile_configured_outbox,
    write_worker_gist,
)


_MAX_INPUT_BYTES = 65_536
_ARGUMENT_ERROR = "agent-memory: invalid arguments"


def _redacted_argument_error(_message: str) -> None:
    print(_ARGUMENT_ERROR, file=sys.stderr)
    raise SystemExit(2)


class _AgentMemoryArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _redacted_argument_error(message)

    def parse_known_args(self, args=None, namespace=None):
        parsed, extras = super().parse_known_args(args, namespace)
        if extras:
            self.error("unrecognized arguments")
        return parsed, extras


def _make_parser_strict(parser: argparse.ArgumentParser) -> None:
    parser.error = _redacted_argument_error
    parse_known_args = parser.parse_known_args

    def strict_parse_known_args(args=None, namespace=None):
        parsed, extras = parse_known_args(args, namespace)
        if extras:
            _redacted_argument_error("unrecognized arguments")
        return parsed, extras

    parser.parse_known_args = strict_parse_known_args


def build_agent_memory_parser(subparsers, *, cmd_agent_memory: Callable) -> None:
    """Attach governed worker-memory commands to ``subparsers``."""
    parser = subparsers.add_parser(
        "agent-memory", help="Recall and record governed worker memory"
    )
    _make_parser_strict(parser)
    verbs = parser.add_subparsers(
        dest="agent_memory_action",
        required=True,
        parser_class=_AgentMemoryArgumentParser,
    )
    for name in ("recall", "write"):
        child = verbs.add_parser(name)
        child.add_argument("--input", default="-")
    verbs.add_parser("reconcile")
    verbs.add_parser("status")
    parser.set_defaults(func=cmd_agent_memory)


def _read_input(source: str) -> object:
    if source == "-":
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw = stream.read(_MAX_INPUT_BYTES + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
    else:
        path = Path(source)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError("input must be an absolute regular file")
        with path.open("rb") as handle:
            raw = handle.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("input exceeds the maximum size")
    try:
        return json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input is not valid JSON") from exc


def _print_result(result: dict[str, object]) -> None:
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


def cmd_agent_memory(args) -> int:
    """Execute one strict Agent Memory protocol operation."""
    action = getattr(args, "agent_memory_action", None)
    try:
        if action == "recall":
            request = WorkerRecallRequest.from_mapping(_read_input(args.input))
            matches, receipt = recall_for_worker(request)
            _print_result(
                {
                    "matches": [asdict(match) for match in matches],
                    "receipt": receipt.to_mapping(),
                }
            )
        elif action == "write":
            request = WorkerWriteRequest.from_mapping(_read_input(args.input))
            _print_result({"receipt": write_worker_gist(request).to_mapping()})
        elif action == "reconcile":
            _print_result(reconcile_configured_outbox().to_mapping())
        elif action == "status":
            _print_result(configured_outbox_status().to_mapping())
        else:
            raise ValueError("unsupported action")
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        print("agent-memory: invalid request", file=sys.stderr)
        return 2
    return 0
