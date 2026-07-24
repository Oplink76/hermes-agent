import ast
import asyncio
import threading
from pathlib import Path

from hermes_cli import web_server


TARGET_HANDLERS = {
    "bulk_delete_sessions_endpoint",
    "count_empty_sessions_endpoint",
    "delete_empty_sessions_endpoint",
    "get_session_latest_descendant",
    "get_session_messages",
    "get_session_stats",
    "delete_session_endpoint",
    "export_session_endpoint",
    "prune_sessions_endpoint",
    "get_usage_analytics",
    "get_models_analytics",
}


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_sessiondb_handlers_open_connections_inside_executor_helpers():
    tree = ast.parse(Path(web_server.__file__).read_text(encoding="utf-8"))
    handlers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in TARGET_HANDLERS
    }
    top_level_helpers = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert handlers.keys() == TARGET_HANDLERS

    for name, handler in handlers.items():
        helpers = {
            **top_level_helpers,
            **{
                node.name: node
                for node in handler.body
                if isinstance(node, ast.FunctionDef)
            },
        }
        offloaded = {
            arg.id
            for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and _call_name(node) in {"to_thread", "_run_session_db_io"}
            for arg in node.args[:1]
            if isinstance(arg, ast.Name)
        }
        db_open_owners = {
            helper_name
            for helper_name, helper in helpers.items()
            if helper_name in offloaded
            and any(
                isinstance(node, ast.Call)
                and _call_name(node) == "_open_session_db_for_profile"
                for node in ast.walk(helper)
            )
        }
        assert db_open_owners, f"{name} does not offload SessionDB open + work"


def test_bulk_delete_sessiondb_work_runs_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    db_threads: list[int] = []

    class _DB:
        def delete_sessions(self, ids):
            db_threads.append(threading.get_ident())
            assert ids == ["one", "two"]
            return 2

        def close(self):
            db_threads.append(threading.get_ident())

    monkeypatch.setattr(web_server, "_open_session_db_for_profile", lambda profile=None: _DB())

    result = asyncio.run(
        web_server.bulk_delete_sessions_endpoint(
            web_server.BulkDeleteSessions(ids=["one", "two"])
        )
    )

    assert result == {"ok": True, "deleted": 2}
    assert db_threads
    assert all(thread_id != loop_thread for thread_id in db_threads)


def test_get_session_stats_sessiondb_work_runs_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    db_threads: list[int] = []

    class _DB:
        def session_count(self, include_archived=False, archived_only=False):
            db_threads.append(threading.get_ident())
            if archived_only:
                return 2
            if include_archived:
                return 9
            return 7

        def message_count(self):
            db_threads.append(threading.get_ident())
            return 41

        def list_sessions_rich(self, limit=None, include_archived=None, compact_rows=None):
            db_threads.append(threading.get_ident())
            assert limit == 10000
            assert include_archived is True
            assert compact_rows is True
            return [{"source": "cli"}, {"source": "web"}, {"source": None}]

        def close(self):
            db_threads.append(threading.get_ident())

    def _open(profile=None):
        db_threads.append(threading.get_ident())
        return _DB()

    monkeypatch.setattr(web_server, "_open_session_db_for_profile", _open)

    result = asyncio.run(web_server.get_session_stats())

    assert result == {
        "total": 9,
        "active_store": 7,
        "archived": 2,
        "messages": 41,
        "by_source": {"cli": 2, "web": 1},
    }
    assert db_threads
    assert all(thread_id != loop_thread for thread_id in db_threads)


def test_get_session_stats_swallows_list_sessions_rich_error_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    db_threads: list[int] = []
    closed: list[bool] = []

    class _DB:
        def session_count(self, include_archived=False, archived_only=False):
            db_threads.append(threading.get_ident())
            if archived_only:
                return 1
            if include_archived:
                return 5
            return 4

        def message_count(self):
            db_threads.append(threading.get_ident())
            return 12

        def list_sessions_rich(self, limit=None, include_archived=None, compact_rows=None):
            db_threads.append(threading.get_ident())
            raise RuntimeError("boom")

        def close(self):
            db_threads.append(threading.get_ident())
            closed.append(True)

    def _open(profile=None):
        db_threads.append(threading.get_ident())
        return _DB()

    monkeypatch.setattr(web_server, "_open_session_db_for_profile", _open)

    result = asyncio.run(web_server.get_session_stats())

    assert result == {
        "total": 5,
        "active_store": 4,
        "archived": 1,
        "messages": 12,
        "by_source": {},
    }
    assert closed == [True]
    assert db_threads
    assert all(thread_id != loop_thread for thread_id in db_threads)
