"""Hermes tools exposed through a role-scoped stdio MCP server.

Codex owns the loop and tool list there, so a curated subset of Hermes tools is
exposed over stdio MCP; codex registers it via ``~/.codex/config.toml
[mcp_servers.hermes-tools]``. Run: ``python -m agent.transports.hermes_tools_mcp_server``.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from typing import Annotated, Any, Optional

from pydantic import WithJsonSchema

logger = logging.getLogger(__name__)

# JSON Schema type -> Python type mapping for signature generation
_JSON_TO_PY = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}


def _signature_from_schema(schema: dict | None) -> tuple[inspect.Signature, dict[str, type]]:
    """Build a Python function signature and annotations from a JSON schema.

    Args:
        schema: JSON Schema dict with "properties" and "required" keys.

    Returns:
        (signature, annotations_dict) where signature has KEYWORD_ONLY params
        and annotations maps param names to Python types.
    """
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    params, annots = [], {}

    for pname, pspec in props.items():
        if pname.startswith("_"):
            continue
        py = _JSON_TO_PY.get((pspec or {}).get("type"), Any)
        if set(pspec or {}) - {"type"}:
            py = Annotated[py, WithJsonSchema(dict(pspec))]
        ann, default = (
            (py, inspect.Parameter.empty)
            if pname in required
            else (Optional[py], None)
        )
        annots[pname] = ann
        params.append(
            inspect.Parameter(
                pname, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=default
            )
        )

    return inspect.Signature(params, return_annotation=str), annots


# Each name MUST match a registered Hermes tool ``model_tools.handle_function_call()`` can dispatch.
# NOT exposed: terminal/file/search/process/clarify (codex built-ins + its own approval UI);
# delegate_task/memory/session_search/todo (need the running AIAgent context).
EXPOSED_TOOLS: tuple[str, ...] = (
    "web_search", "web_extract",
    "browser_navigate", "browser_click", "browser_type", "browser_press", "browser_snapshot", "browser_scroll",
    "browser_back", "browser_get_images", "browser_console", "browser_vision",
    "vision_analyze", "image_generate", "skill_view", "skills_list", "text_to_speech",
    # Kanban handoff tools: stateless (read HERMES_KANBAN_TASK, write kanban.db).
    # Without them a codex-runtime worker can't report completion and hangs.
    "kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes", "kanban_comment",
    "kanban_heartbeat", "kanban_show", "kanban_list",
    # Orchestrator-only (the kanban tool gates them on HERMES_KANBAN_TASK unset).
    "kanban_create", "kanban_unblock", "kanban_link",
)

CODEX_APP_TOOLS: tuple[str, ...] = (
    "web_search",
    "web_extract",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_snapshot",
    "browser_scroll",
    "browser_back",
    "browser_get_images",
    "browser_console",
    "browser_vision",
    "vision_analyze",
    "image_generate",
    "skill_view",
    "skills_list",
    "text_to_speech",
    # Kanban worker handoff tools — gated on HERMES_KANBAN_TASK env var
    # (set by the kanban dispatcher when spawning a worker). Without these
    # in the callback, a worker spawned with openai_runtime=codex_app_server
    # could do the work but couldn't report completion back to the kernel,
    # making it hang until timeout. Stateless dispatch — they just read
    # the env var and write to ~/.hermes/kanban.db.
    "kanban_complete",
    "kanban_block",
    "kanban_resolve",
    "kanban_request_review",
    "kanban_request_changes",
    "kanban_comment",
    "kanban_heartbeat",
    "kanban_show",
    "kanban_list",
    # NOTE: kanban_create / kanban_unblock / kanban_link are orchestrator-
    # only — the kanban tool gates them on HERMES_KANBAN_TASK being unset.
    # They're exposed here for orchestrator agents running on the codex
    # runtime that need to dispatch new tasks.
    "kanban_create",
    "kanban_unblock",
    "kanban_link",
)



PRODUCT_OWNER_TOOLS: tuple[str, ...] = (
    "kanban_show",
    "kanban_create",
    "kanban_comment",
    "kanban_heartbeat",
    "kanban_complete",
    "kanban_block",
)

PRODUCT_OWNER_INTAKE_TOOLS: tuple[str, ...] = (
    "work_inbox_show",
    "work_inbox_decide",
    "work_inbox_heartbeat",
)

REVIEWER_TOOLS: tuple[str, ...] = (
    "kanban_show",
    "kanban_comment",
    "kanban_heartbeat",
    "kanban_complete",
    "kanban_block",
    "review_target",
)

CAPABILITY_SETS: dict[str, tuple[str, ...]] = {
    "codex-app": CODEX_APP_TOOLS,
    "product-owner": PRODUCT_OWNER_TOOLS,
    "product-owner-intake": PRODUCT_OWNER_INTAKE_TOOLS,
    "reviewer": REVIEWER_TOOLS,
}

CLAUDE_TASK_CAPABILITY_BY_PROFILE = {
    "productowner": "product-owner",
    "reviewer": "reviewer",
}

CAPABILITY_INSTRUCTIONS = {
    "codex-app": (
        "Hermes Agent tools exposed to an external runtime. Use only the "
        "capabilities present in this server."
    ),
    "product-owner": (
        "You are the task-scoped Product Owner. Your filesystem access is "
        "read-only. Own only the assigned backlog item. You may submit bounded "
        "child-intake proposals with kanban_create, but qualification owns "
        "trusted routing and dependencies; you cannot link cards directly. "
        "This headless run cannot conduct a live interview: post the exact "
        "decision request and call kanban_block when operator input is needed. "
        "You cannot create or upload attachments, and attachment content is "
        "unavailable through this bridge. If the assignment "
        "requires unavailable attachment content or file/attachment creation, "
        "comment on the task and call kanban_block with that missing-capability "
        "reason; do not infer the missing content or broaden access."
    ),
    "product-owner-intake": (
        "You are the first semantic owner of one claimed Work Inbox intake. "
        "Call work_inbox_show before deciding. Finish with one successful terminal "
        "work_inbox_decide disposition. If an accepted proposal returns status "
        "invalid, correct the returned validation errors and retry once in the same "
        "run. Make at most two work_inbox_decide calls total; do not retry after "
        "qualified, rejected, needs_clarification, or attention_required. You cannot "
        "create, edit, claim, or move cards directly and cannot write repository "
        "files. Request clarification when essential information is missing. This "
        "run has no provider fallback; failure must leave the intake inert."
    ),
    "reviewer": (
        "You are the task-scoped Reviewer. Your filesystem access is read-only. "
        "Inspect only the pinned candidate exposed by review_target. You cannot "
        "create or upload attachments, and attachment content is unavailable "
        "through this bridge. If required evidence is unavailable, comment on "
        "the task and call kanban_block with that missing-capability reason."
    ),
}

# Backward compatibility for existing imports and codex_app_server tests.
EXPOSED_TOOLS = CODEX_APP_TOOLS


def selected_tool_names(environ=None) -> tuple[str, ...]:
    """Select a fixed capability set; unknown explicit values fail closed."""
    source = os.environ if environ is None else environ
    raw = source.get("HERMES_MCP_CAPABILITY_SET")
    if raw is None or not str(raw).strip():
        return CODEX_APP_TOOLS
    return CAPABILITY_SETS.get(str(raw).strip(), ())


def _build_server() -> Any:
    """Create the MCP server with Hermes tools attached (lazy imports: importable without ``mcp``)."""
    try:
        # mcp 2.0 renamed `mcp.server.fastmcp` to `mcp.server.MCPServer` (same surface).
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - install hint
        raise ImportError(f"hermes-tools MCP server requires the 'mcp' package: {exc}") from exc

    from model_tools import get_tool_definitions, handle_function_call

    capability_set = (
        os.environ.get("HERMES_MCP_CAPABILITY_SET") or "codex-app"
    ).strip() or "codex-app"
    mcp = MCPServer(
        "hermes-tools",
        instructions=CAPABILITY_INSTRUCTIONS.get(
            capability_set,
            "No audited Hermes capability set was selected. Do not proceed.",
        ),
    )

    # Authoritative Hermes schemas so MCP clients see the same parameter docs the model does.
    all_defs = {
        td["function"]["name"]: td["function"]
        for td in (
            get_tool_definitions(
                quiet_mode=True,
                skip_tool_search_assembly=True,
            )
            or []
        )
        if isinstance(td, dict) and td.get("type") == "function"
    }

    def _make_handler(tool_name: str, schema: dict | None, description: str):
        # The SDK derives the input schema from the callable's signature, so synthesize it from the JSON Schema.
        sig, annots = _signature_from_schema(schema)

        def _dispatch(**kwargs: Any) -> str:
            try:
                # Drop None so unset optionals aren't forwarded to the handler.
                return handle_function_call(tool_name, {k: v for k, v in kwargs.items() if v is not None})
            except Exception as exc:
                logger.exception("tool %s raised", tool_name)
                return json.dumps({"error": str(exc), "tool": tool_name})

        _dispatch.__name__ = tool_name
        _dispatch.__doc__ = description
        _dispatch.__signature__ = sig
        _dispatch.__annotations__ = {**annots, "return": str}
        return _dispatch


    selected = selected_tool_names()
    exposed_count = 0

    for name in selected:
        spec = all_defs.get(name)
        if spec is None:
            logger.debug("skipping %s — not registered in this Hermes process", name)
            continue
        description = spec.get("description") or f"Hermes {name} tool"
        params_schema = spec.get("parameters") or {"type": "object", "properties": {}}
        try:
            mcp.add_tool(_make_handler(name, params_schema, description), name=name, description=description)
        except TypeError:
            # Older mcp SDK: decorator-style registration; __signature__ still drives schema.
            mcp.tool(name=name, description=description)(_make_handler(name, params_schema, description))
        exposed_count += 1

    logger.info("hermes-tools MCP server registered %d/%d tools", exposed_count, len(EXPOSED_TOOLS))
    return mcp


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for `python -m agent.transports.hermes_tools_mcp_server`."""
    argv = argv or sys.argv[1:]
    verbose = "--verbose" in argv or "-v" in argv
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        stream=sys.stderr,  # MCP uses stdio for protocol — logs MUST go to stderr
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Keep Hermes' own banners off stdout (the MCP wire).
    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")

    try:
        server = _build_server()
    except ImportError as exc:
        sys.stderr.write(f"hermes-tools MCP server cannot start: {exc}\n")
        return 2
    try:
        server.run()  # defaults to stdio transport, which codex spawns us on
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("hermes-tools MCP server crashed")
        sys.stderr.write(f"hermes-tools MCP server error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
