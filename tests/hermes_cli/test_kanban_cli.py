"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_no_args_shows_usage(kanban_home):
    out = kc.run_slash("")
    assert "kanban" in out.lower()
    assert "create" in out.lower() or "subcommand" in out.lower() or "action" in out.lower()


def test_run_slash_create_and_list(kanban_home):
    out = kc.run_slash("create 'ship feature' --assignee alice")
    assert "Created" in out
    out = kc.run_slash("list")
    assert "ship feature" in out
    assert "alice" in out


@pytest.mark.parametrize(
    ("policy", "required", "forbidden"),
    [("none", False, False), ("required", True, False), ("forbidden", False, True)],
)
def test_run_slash_create_source_policy(kanban_home, policy, required, forbidden):
    payload = json.loads(kc.run_slash(
        f"create 'policy task' --source-policy {policy} --json"
    ))
    with kb.connect() as conn:
        task = kb.get_task(conn, payload["id"])
    assert task.source_commit_required is required
    assert task.source_commit_forbidden is forbidden


def test_run_slash_rejects_source_policy_on_qualified_board(kanban_home):
    kb.ensure_product_board_defaults("strict", switch=True)
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    out = kc.run_slash("create 'policy task' --source-policy required")
    assert "Default-board execution contract" in out


def test_run_slash_create_worktree_path_and_branch(kanban_home, tmp_path):
    target = tmp_path / ".worktrees" / "t6-wire"
    target_arg = target.as_posix()
    out = kc.run_slash(
        f"create 'ship worktree' --workspace worktree:{target_arg} --branch wt/t6-wire"
    )
    assert "Created" in out

    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
    task = tasks[0]
    assert task.workspace_kind == "worktree"
    assert task.workspace_path == target_arg
    assert task.branch_name == "wt/t6-wire"


def test_run_slash_rejects_branch_without_worktree(kanban_home):
    out = kc.run_slash("create 'bad branch' --workspace scratch --branch wt/bad")
    assert "--branch is only valid with --workspace worktree" in out


def test_run_slash_create_with_parent_and_cascade(kanban_home):
    # Parent then child via --parent
    out1 = kc.run_slash("create 'parent' --assignee alice")
    # Extract the "t_xxxx" id from "Created t_xxxx (ready, ...)"
    import re
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    p = m.group(1)
    out2 = kc.run_slash(f"create 'child' --assignee bob --parent {p}")
    assert "todo" in out2  # child starts as todo

    # Complete parent; list should promote child to ready
    kc.run_slash(f"complete {p}")
    # Explicit filter: child should now be ready (was todo before complete).
    ready_list = kc.run_slash("list --status ready")
    assert "child" in ready_list


def test_run_slash_show_includes_comments(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    show = kc.run_slash(f"show {tid}")
    assert "performance section" in show


def test_run_slash_comment_max_len_trims_long_body(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} '{'x' * 30}' --max-len 20")
    show = kc.run_slash(f"show {tid}")
    assert "trimmed to 20 chars by --max-len" in show
    assert "x" * 30 not in show


def test_run_slash_block_unblock_cycle(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    # Claim first so block() finds it running
    kc.run_slash(f"claim {tid}")
    assert "Blocked" in kc.run_slash(f"block {tid} 'need decision'")
    assert "Unblocked" in kc.run_slash(f"unblock {tid}")


def test_manual_claim_rolls_back_when_workspace_provisioning_fails(
    kanban_home, monkeypatch, capsys
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="provisioning failure")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()

    monkeypatch.setattr(
        kb,
        "resolve_workspace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unsafe deps")),
    )

    result = kc._cmd_claim(argparse.Namespace(task_id=task_id, ttl=None))

    assert result == 1
    assert "workspace provisioning failed" in capsys.readouterr().err
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.current_run_id is None


def test_run_slash_json_output(kanban_home):
    out = kc.run_slash("create 'jsontask' --assignee alice --json")
    payload = json.loads(out)
    assert payload["title"] == "jsontask"
    assert payload["assignee"] == "alice"
    assert payload["status"] == "ready"


def test_strict_board_create_returns_inert_intake_receipt(kanban_home):
    kb.ensure_product_board_defaults("strict", switch=True)
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    payload = json.loads(
        kc.run_slash(
            "create 'unqualified request' --assignee developer "
            "--parent t_missing --json"
        )
    )

    assert payload == {
        "status": "qualification_required",
        "intake_id": payload["intake_id"],
        "intake_status": "pending",
    }
    assert payload["intake_id"].startswith("qi_")
    with kb.connect(board="strict") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        record = kb.get_qualification_intake(conn, payload["intake_id"])
    assert "unqualified request" in record["raw_request"]
    assert '"parents":["t_missing"]' in record["raw_request"]


def test_run_slash_intake_show_reports_safe_failure_and_budget(kanban_home):
    kb.ensure_product_board_defaults("strict", switch=True)
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with kb.connect(board="strict") as conn:
        intake_id = kb.create_qualification_intake(
            conn, raw_request="request", source="cli"
        )
        kb.append_qualification_intake_event(
            conn,
            intake_id=intake_id,
            kind="work_contract_verification_failed",
            payload={"failure_path": "io_error", "raw": "secret"},
        )
        conn.execute(
            "UPDATE qualification_intake SET status = 'attention_required' WHERE id = ?",
            (intake_id,),
        )

    output = kc.run_slash(f"intake show {intake_id}")
    assert "io_error" in output
    assert "Attempts: 0/3" in output
    assert "secret" not in output


def test_run_slash_intake_retry_reports_refusal_and_success(kanban_home):
    kb.ensure_product_board_defaults("strict", switch=True)
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with kb.connect(board="strict") as conn:
        intake_id = kb.create_qualification_intake(
            conn, raw_request="request", source="cli"
        )
        conn.execute(
            "UPDATE qualification_intake SET status = 'attention_required' WHERE id = ?",
            (intake_id,),
        )

    assert f"{intake_id}: pending" in kc.run_slash(f"intake retry {intake_id}")
    refusal = kc.run_slash(f"intake retry {intake_id}")
    assert "attention_required" in refusal


def test_run_slash_intake_show_honors_board_override(kanban_home):
    current_board = "intake-current"
    selected_board = "intake-selected"
    kb.ensure_product_board_defaults(current_board, switch=True)
    kb.ensure_product_board_defaults(selected_board)

    with kb.connect(board=selected_board) as conn:
        intake_id = kb.create_qualification_intake(
            conn, raw_request="request", source="cli"
        )
        kb.append_qualification_intake_event(
            conn,
            intake_id=intake_id,
            kind="work_contract_verification_failed",
            payload={"failure_path": "io_error"},
        )
        conn.execute(
            "UPDATE qualification_intake SET status = 'attention_required' WHERE id = ?",
            (intake_id,),
        )

    output = kc.run_slash(
        f"--board {selected_board} intake show {intake_id}"
    )

    assert f"Intake {intake_id}: attention_required" in output
    assert "Failure path: io_error" in output
    assert "unknown qualification intake" not in output
    assert kb.get_current_board() == current_board


def test_run_slash_intake_retry_honors_board_override(kanban_home):
    current_board = "intake-current"
    selected_board = "intake-selected"
    kb.ensure_product_board_defaults(current_board, switch=True)
    kb.ensure_product_board_defaults(selected_board)

    with kb.connect(board=selected_board) as conn:
        intake_id = kb.create_qualification_intake(
            conn, raw_request="request", source="cli"
        )
        conn.execute(
            "UPDATE qualification_intake SET status = 'attention_required' WHERE id = ?",
            (intake_id,),
        )

    output = kc.run_slash(
        f"--board {selected_board} intake retry {intake_id}"
    )

    assert f"{intake_id}: pending" in output
    with kb.connect(board=selected_board) as conn:
        record = kb.get_qualification_intake(conn, intake_id)
        assert record is not None
        assert record["status"] == "pending"
    assert kb.get_current_board() == current_board


def test_run_slash_dispatch_dry_run_counts(kanban_home):
    kc.run_slash("create 'a' --assignee alice")
    kc.run_slash("create 'b' --assignee bob")
    out = kc.run_slash("dispatch --dry-run")
    assert "Spawned:" in out


def test_run_slash_context_output_format(kanban_home):
    out = kc.run_slash("create 'tech spec' --assignee alice --body 'write an RFC'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    ctx = kc.run_slash(f"context {tid}")
    assert "tech spec" in ctx
    assert "write an RFC" in ctx
    assert "performance section" in ctx


def test_run_slash_tenant_filter(kanban_home):
    kc.run_slash("create 'biz-a task' --tenant biz-a --assignee alice")
    kc.run_slash("create 'biz-b task' --tenant biz-b --assignee alice")
    a = kc.run_slash("list --tenant biz-a")
    b = kc.run_slash("list --tenant biz-b")
    assert "biz-a task" in a and "biz-b task" not in a
    assert "biz-b task" in b and "biz-a task" not in b


def test_run_slash_session_filter(kanban_home):
    """`hermes kanban list --session <id>` filters by the originating
    chat session id stamped on tasks created from inside an ACP loop."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="from sess-1 a", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-1 b", assignee="alice", session_id="sess-1"
        )
        kb.create_task(
            conn, title="from sess-2", assignee="alice", session_id="sess-2"
        )
        kb.create_task(conn, title="cli only", assignee="alice")
    out_1 = kc.run_slash("list --session sess-1")
    out_2 = kc.run_slash("list --session sess-2")
    assert "from sess-1 a" in out_1
    assert "from sess-1 b" in out_1
    assert "from sess-2" not in out_1
    assert "cli only" not in out_1
    assert "from sess-2" in out_2
    assert "from sess-1 a" not in out_2


def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------



@pytest.mark.parametrize("alias", ["help", "--help", "-h", "?"])
def test_run_slash_help_aliases_match_bare(kanban_home, alias):
    """Every documented help alias produces the same curated output."""
    bare = kc.run_slash("")
    out = kc.run_slash(alias)
    assert out == bare


def test_run_slash_subcommand_help_returns_help_text(kanban_home):
    """`/kanban show -h` returns the actual subcommand help, not a
    fake `(usage error: 0)` sentinel."""
    out = kc.run_slash("show -h")
    assert "task_id" in out
    assert "/kanban show" in out
    assert not out.startswith("⚠")


def test_run_slash_unknown_action_friendly_error(kanban_home):
    """Unknown subcommand surfaces a single-line usage error prefixed
    with our marker — no `(usage error: 2)` wrapping, no doubled
    `kanban kanban` prog string."""
    out = kc.run_slash("frobnicate")
    assert "/kanban" in out
    assert "frobnicate" in out
    assert "/kanban-wrap" not in out
    assert "/kanban kanban" not in out
    assert "(usage error: " not in out


def test_run_slash_missing_required_arg_friendly_error(kanban_home):
    """Missing positional argument shows the subcommand-scoped usage
    line, not the top-level kanban tree."""
    out = kc.run_slash("show")
    assert "/kanban show" in out
    assert "task_id" in out


def test_run_slash_board_override_restores_prior_env(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")

    kc.run_slash("--board alpha list")

    assert os.environ.get("HERMES_KANBAN_BOARD") == "beta"


def test_run_slash_board_override_does_not_change_boards_show_current(kanban_home):
    kb.create_board("alpha")
    kb.create_board("beta")
    kb.set_current_board("alpha")

    out = kc.run_slash("--board beta boards show")

    assert "Current board: alpha" in out


# ---------------------------------------------------------------------------
# D4 resolver escalation answer/re-entry CLI contract
# ---------------------------------------------------------------------------


def _cli_d4_board(name: str) -> None:
    kb.create_board(name, name="D4 CLI", preset="product")
    metadata_path = kb.board_metadata_path(name)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("product_workflow", {})["handoff_v2"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def _cli_d4_escalated(board: str) -> str:
    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Story: CLI D4",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            board=board,
        )
        first = kb.claim_task(conn, task_id, board=board)
        assert first is not None and first.current_run_id is not None
        assert kb.block_task(
            conn, task_id, reason="Need an operator", kind="needs_input",
            expected_run_id=first.current_run_id, board=board,
            human_escalation_assignee="resolver",
        )
        resolver = kb.claim_task(conn, task_id, board=board)
        assert resolver is not None and resolver.current_run_id is not None
        expected = kb.resolver_expected_snapshot(conn, task_id)
        assert expected is not None
        request = {
            "decision": "escalate",
            "fault_domain": "framework",
            "diagnosis": "The answer is outside the Resolver context",
            "reason": "Ask the operator",
            "expected": expected,
        }
        assert kb.resolve_product_preflight(
            conn, task_id, board=board, request=request,
            resolver_profile="resolver", resolver_model="test-model",
        )
    return task_id


def test_answer_escalation_parser_supports_double_dash_leading_answer():
    wrapper = argparse.ArgumentParser()
    subparsers = wrapper.add_subparsers(dest="root")
    parser = kc.build_parser(subparsers)
    args = parser.parse_args(["answer-escalation", "t_example", "--", "-starts-with-dash"])
    assert args.kanban_action == "answer-escalation"
    assert args.task_id == "t_example"
    assert args.answer == ["-starts-with-dash"]


def test_answer_escalation_cli_success_accepts_leading_dash_and_writes_no_comment(kanban_home):
    board = "d4-cli-success"
    _cli_d4_board(board)
    task_id = _cli_d4_escalated(board)
    with kb.scoped_current_board(board):
        out = kc.run_slash(f"answer-escalation {task_id} -- --use-vendored-fixture")
    assert "Answered" in out
    assert "fresh Resolver" in out
    with kb.connect(board=board) as conn:
        comments = kb.list_comments(conn, task_id)
        events = kb.list_events(conn, task_id)
    assert not [comment for comment in comments if "use-vendored" in comment.body]
    answer_events = [
        event for event in events
        if event.kind == kb.PRODUCT_WORKFLOW_PRECHECK_EVENT
        and event.payload.get("kind") == "resolver_reentry"
    ]
    assert len(answer_events) == 1
    assert answer_events[0].payload["human_answer"] == "--use-vendored-fixture"


def test_answer_escalation_cli_stale_conflict_is_concise_and_public(kanban_home):
    board = "d4-cli-conflict"
    _cli_d4_board(board)
    task_id = _cli_d4_escalated(board)
    with kb.scoped_current_board(board):
        first = kc.run_slash(f"answer-escalation {task_id} first --answered-by operator")
        second = kc.run_slash(f"answer-escalation {task_id} second --answered-by operator")
    assert "Answered" in first
    assert "cannot answer" in second
    assert "task changed" in second
    assert "Traceback" not in second
    assert "sqlite3" not in second


def _cli_clear_terminal_card(board: str) -> tuple[str, int, int]:
    _cli_d4_board(board)
    completed_at = 1_700_000_456
    with kb.connect(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title="Story: clear terminal state",
            assignee="developer",
            workflow_template_id="product",
            current_step_key="development",
            board=board,
        )
        with kb.authorized_governance_write(), kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
                (completed_at, "preserve CLI evidence", task_id),
            )
            event_id = kb._append_event(
                conn,
                task_id,
                "completed",
                {"evidence": "preserve CLI payload"},
            )
    return task_id, completed_at, event_id


def test_clear_terminal_state_parser_requires_every_expected_option():
    wrapper = argparse.ArgumentParser()
    subparsers = wrapper.add_subparsers(dest="root")
    parser = kc.build_parser(subparsers)
    with pytest.raises(SystemExit):
        parser.parse_args(["clear-terminal-state", "t_example"])
    args = parser.parse_args(
        [
            "clear-terminal-state",
            "t_example",
            "--expected-completed-at",
            "1700000000",
            "--expected-phase",
            "development",
            "--expected-latest-event-id",
            "42",
            "--actor",
            "operator",
            "--reason",
            "stale state",
        ]
    )
    assert args.expected_completed_at == 1_700_000_000
    assert args.expected_phase == "development"
    assert args.expected_latest_event_id == 42
    assert args.actor == "operator"
    assert args.reason == "stale state"


def test_clear_terminal_state_cli_success_has_structured_non_evidence_output(kanban_home):
    board = "clear-terminal-cli-success"
    task_id, completed_at, event_id = _cli_clear_terminal_card(board)
    with kb.scoped_current_board(board):
        out = kc.run_slash(
            f"clear-terminal-state {task_id} "
            f"--expected-completed-at {completed_at} "
            "--expected-phase development "
            f"--expected-latest-event-id {event_id} "
            "--actor operator --reason 'clear stale state' --json"
        )
    payload = json.loads(out)
    assert payload == {
        "operation": "clear_terminal_state",
        "task_id": task_id,
        "status": "ready",
        "completed_at": None,
    }
    assert "preserve CLI evidence" not in out
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "ready" and task.completed_at is None


def test_clear_terminal_state_cli_refuses_lost_cas_without_evidence_payload(kanban_home):
    board = "clear-terminal-cli-conflict"
    task_id, completed_at, event_id = _cli_clear_terminal_card(board)
    with kb.scoped_current_board(board):
        out = kc.run_slash(
            f"clear-terminal-state {task_id} "
            f"--expected-completed-at {completed_at} "
            "--expected-phase development "
            f"--expected-latest-event-id {event_id + 1} "
            "--actor operator --reason 'stale event'"
        )
    assert "cannot clear terminal state" in out
    assert "preserve CLI evidence" not in out
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "done" and task.completed_at == completed_at
