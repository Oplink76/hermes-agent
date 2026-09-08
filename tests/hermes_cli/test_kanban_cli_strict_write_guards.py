"""Strict-board routing belongs to the Work Contract, including at the CLI."""
from argparse import Namespace
from unittest.mock import MagicMock, Mock

import pytest

from hermes_cli import kanban


@pytest.mark.parametrize("command,writer", [
    ("assign", "assign_task"), ("reassign", "reassign_task"),
    ("link", "link_tasks"), ("unlink", "unlink_tasks"),
])
@pytest.mark.parametrize("strict", [True, False])
def test_cli_respects_strict_board_routing(command, writer, strict, monkeypatch, capsys):
    monkeypatch.setattr(kanban.kbc, "connect_closing", MagicMock())
    monkeypatch.setattr(kanban.kb, "get_current_board", lambda: "test-board")
    monkeypatch.setattr(kanban.kb, "read_board_metadata", lambda board: {})
    monkeypatch.setattr(kanban.kanban_intake, "qualification_required", lambda metadata: strict)
    write = Mock(return_value=True)
    monkeypatch.setattr(kanban.kb, writer, write)
    args = Namespace(profile="worker", task_id="task", parent_id="parent", child_id="child")
    result = getattr(kanban, "_cmd_" + command)(args)
    if strict:
        assert result == 2
        assert "owned by the Work Contract" in capsys.readouterr().err
        write.assert_not_called()
    else:
        assert result == 0
        write.assert_called_once()
