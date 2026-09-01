"""Tests for the hermes-tools-as-MCP server module surface.

We don't run a live MCP session in unit tests — that requires the codex
subprocess + client + an event loop. These tests pin the static
contract: the module imports, the EXPOSED_TOOLS list is sane, and the
build helper assembles a server when the SDK is present.
"""

from __future__ import annotations

import inspect
from typing import get_args

from agent.transports.hermes_tools_mcp_server import (
    _signature_from_schema,
)


class TestSignatureFromSchema:
    """Test the JSON Schema -> Python signature conversion."""

    def test_simple_required_string_param(self):
        """A required string param becomes str with no default."""
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        sig, annots = _signature_from_schema(schema)

        assert len(sig.parameters) == 1
        param = sig.parameters["query"]
        assert param.name == "query"
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert annots["query"] == str
        assert param.default is inspect.Parameter.empty



    def test_skip_private_params(self):
        """Params starting with '_' are excluded from the signature."""
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "_internal": {"type": "string"},
            },
            "required": ["query", "_internal"],
        }
        sig, annots = _signature_from_schema(schema)

        assert "_internal" not in sig.parameters
        assert "_internal" not in annots
        assert "query" in sig.parameters

    def test_all_json_types(self):
        """All JSON schema types map to correct Python types."""
        schema = {
            "type": "object",
            "properties": {
                "s": {"type": "string"},
                "i": {"type": "integer"},
                "n": {"type": "number"},
                "b": {"type": "boolean"},
                "a": {"type": "array"},
                "o": {"type": "object"},
            },
            "required": ["s", "i", "n", "b", "a", "o"],
        }
        sig, annots = _signature_from_schema(schema)

        assert annots["s"] == str
        assert annots["i"] == int
        assert annots["n"] == float
        assert annots["b"] == bool
        assert annots["a"] == list
        assert annots["o"] == dict








class TestModuleSurface:
    def test_module_imports_clean(self):
        from agent.transports import hermes_tools_mcp_server as m
        assert callable(m.main)
        assert callable(m._build_server)
        assert isinstance(m.EXPOSED_TOOLS, tuple)
        assert len(m.EXPOSED_TOOLS) > 0

    def test_exposed_tools_are_safe_subset(self):
        """We MUST NOT expose tools codex already has, because codex'
        own builtins are better-integrated with its sandbox + approvals.
        Specifically: no terminal/shell, no read_file/write_file, no
        patch — those are codex's built-in tools."""
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
        forbidden = {
            "terminal", "shell", "read_file", "write_file", "patch",
            "search_files", "process",
        }
        leaked = forbidden & set(EXPOSED_TOOLS)
        assert not leaked, (
            f"these tools must NOT be exposed via the codex callback "
            f"because codex has built-in equivalents: {leaked}"
        )



    def test_kanban_worker_tools_exposed(self):
        """Kanban workers run as `hermes chat -q` subprocesses; if they
        come up on the codex_app_server runtime, the worker can do the
        actual work via codex's shell but needs the kanban tools through
        the MCP callback to report back to the kernel. Without these
        tools available, the worker would hang at completion time."""
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS
        # Worker handoff tools — every dispatched worker uses at least
        # one of {complete, block, comment} to close out its task.
        for worker_tool in (
            "kanban_complete",
            "kanban_block",
            "kanban_resolve",
            "kanban_comment",
            "kanban_heartbeat",
        ):
            assert worker_tool in EXPOSED_TOOLS, (
                f"{worker_tool!r} missing from codex callback — kanban "
                "workers on codex_app_server runtime would hang"
            )



class TestCapabilitySets:
    def test_missing_capability_set_preserves_codex_app_surface(self):
        from agent.transports import hermes_tools_mcp_server as m

        assert m.selected_tool_names({}) == m.CODEX_APP_TOOLS
        assert m.EXPOSED_TOOLS == m.CODEX_APP_TOOLS

    def test_product_owner_capabilities_are_task_lifecycle_only(self):
        from agent.transports import hermes_tools_mcp_server as m

        selected = m.selected_tool_names(
            {"HERMES_MCP_CAPABILITY_SET": "product-owner"}
        )
        assert selected == (
            "kanban_show",
            "kanban_create",
            "kanban_comment",
            "kanban_heartbeat",
            "kanban_complete",
            "kanban_block",
        )
        assert "kanban_list" not in selected

    def test_product_owner_intake_capability_is_intake_only(self):
        from agent.transports import hermes_tools_mcp_server as m

        selected = m.selected_tool_names(
            {"HERMES_MCP_CAPABILITY_SET": "product-owner-intake"}
        )
        assert selected == (
            "work_inbox_show",
            "work_inbox_decide",
            "work_inbox_heartbeat",
        )
        assert not any(name.startswith("kanban_") for name in selected)

    def test_product_owner_intake_authority_allows_one_bounded_invalid_retry(self):
        from agent.transports.hermes_tools_mcp_server import CAPABILITY_INSTRUCTIONS

        prompt = CAPABILITY_INSTRUCTIONS["product-owner-intake"]
        assert "exactly one" not in prompt
        assert "returns status invalid" in prompt
        assert "retry once in the same run" in prompt
        assert "at most two work_inbox_decide calls total" in prompt

    def test_capability_selection_does_not_leak_between_runs(self):
        from agent.transports import hermes_tools_mcp_server as m

        task_tools = m.selected_tool_names(
            {"HERMES_MCP_CAPABILITY_SET": "product-owner"}
        )
        intake_tools = m.selected_tool_names(
            {"HERMES_MCP_CAPABILITY_SET": "product-owner-intake"}
        )
        task_tools_again = m.selected_tool_names(
            {"HERMES_MCP_CAPABILITY_SET": "product-owner"}
        )

        assert task_tools_again == task_tools
        assert intake_tools == m.PRODUCT_OWNER_INTAKE_TOOLS
        assert set(task_tools).isdisjoint(intake_tools)

    def test_reviewer_capabilities_are_read_and_lifecycle_only(self):
        from agent.transports import hermes_tools_mcp_server as m

        selected = m.selected_tool_names(
            {"HERMES_MCP_CAPABILITY_SET": "reviewer"}
        )
        assert selected == (
            "kanban_show",
            "kanban_comment",
            "kanban_heartbeat",
            "kanban_complete",
            "kanban_block",
            "review_target",
        )
        assert {"kanban_create", "kanban_link", "kanban_list"}.isdisjoint(selected)

    def test_capability_sets_expose_no_governed_agent_memory_tools(self):
        from agent.transports.hermes_tools_mcp_server import CAPABILITY_SETS

        for capability, tools in CAPABILITY_SETS.items():
            offenders = [name for name in tools if "agent_memory" in name]
            assert offenders == [], f"{capability}: {offenders}"

    def test_unknown_explicit_capability_set_exposes_nothing(self):
        from agent.transports import hermes_tools_mcp_server as m

        assert m.selected_tool_names(
            {"HERMES_MCP_CAPABILITY_SET": "unknown"}
        ) == ()

    def test_server_registration_honors_fail_closed_selection(
        self, monkeypatch,
    ):
        from agent.transports import hermes_tools_mcp_server as m
        import model_tools

        monkeypatch.setenv("HERMES_MCP_CAPABILITY_SET", "unknown")
        monkeypatch.setattr(
            model_tools,
            "get_tool_definitions",
            lambda **_kwargs: [
                {
                    "type": "function",
                    "function": {
                        "name": "kanban_show",
                        "description": "show",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )

        server = m._build_server()

        assert server._tool_manager._tools == {}

    def test_real_intake_server_registers_all_tools_with_nested_decision_schema(
        self, monkeypatch, tmp_path,
    ):
        from agent.transports import hermes_tools_mcp_server as m
        from model_tools import _clear_tool_defs_cache
        from tools.registry import invalidate_check_fn_cache

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_PROFILE", "productowner")
        monkeypatch.setenv("HERMES_WORK_INBOX_INTAKE", "qi_one")
        monkeypatch.setenv("HERMES_WORK_INBOX_RUN_ID", "7")
        monkeypatch.setenv("HERMES_WORK_INBOX_CLAIM_LOCK", "claim")
        monkeypatch.setenv(
            "HERMES_MCP_CAPABILITY_SET", "product-owner-intake"
        )
        monkeypatch.setattr(
            "tools.kanban_tools._is_delegated_child_context",
            lambda: False,
        )
        invalidate_check_fn_cache()
        _clear_tool_defs_cache()

        server = m._build_server()
        registered = server._tool_manager._tools

        assert set(registered) == set(m.PRODUCT_OWNER_INTAKE_TOOLS)
        decision = registered["work_inbox_decide"].parameters
        assert decision["properties"]["disposition"]["enum"] == [
            "accepted",
            "needs_clarification",
            "rejected",
        ]
        proposal = next(
            item
            for item in decision["properties"]["proposal"]["anyOf"]
            if item.get("type") == "object"
        )
        assert set(proposal["required"]) == {
            "work",
            "routing",
            "handover",
            "rules",
            "sizing",
            "requirement_feasibility",
            "classification",
            "stories",
        }
        assert set(proposal["properties"]["routing"]["required"]) == {
            "entry_phase",
            "assignee",
            "epic_id",
            "dependencies",
        }
        assert set(
            proposal["properties"]["requirement_feasibility"]["required"]
        ) == {
            "rationale",
            "achievable_requirements",
            "deferred_findings",
        }


class TestMain:
    def test_main_returns_2_when_mcp_unavailable(self, monkeypatch):
        """When the mcp package isn't installed, main() should exit
        cleanly with code 2 and an install hint, not crash."""
        import agent.transports.hermes_tools_mcp_server as m

        def boom_build(*a, **kw):
            raise ImportError("mcp not installed")

        monkeypatch.setattr(m, "_build_server", boom_build)
        rc = m.main(["--verbose"])
        assert rc == 2

    def test_main_handles_keyboard_interrupt(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class FakeServer:
            def run(self):
                raise KeyboardInterrupt()

        monkeypatch.setattr(m, "_build_server", lambda: FakeServer())
        rc = m.main([])
        assert rc == 0

    def test_main_returns_1_on_runtime_error(self, monkeypatch):
        import agent.transports.hermes_tools_mcp_server as m

        class CrashingServer:
            def run(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(m, "_build_server", lambda: CrashingServer())
        rc = m.main([])
        assert rc == 1
