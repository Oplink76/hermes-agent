"""Hermes tools exposed through a role-scoped stdio MCP server.

The default capability set preserves the historical codex_app_server surface.
Task-scoped external runtimes select a smaller immutable surface with
``HERMES_MCP_CAPABILITY_SET``.

This module exposes a curated subset of those Hermes tools to Codex and
task-scoped Claude subprocesses via stdio MCP. Codex registers it as a
normal MCP server (per `~/.codex/config.toml [mcp_servers.hermes-tools]`);
Claude receives a strict inline server entry for one task-scoped turn.

Scope (what we expose):
  - web_search, web_extract              — Firecrawl, no codex equivalent
  - browser_navigate / _click / _type /  — Camofox/Browserbase automation
    _snapshot / _scroll / _back / _press /
    _get_images / _console / _vision
  - vision_analyze                       — image inspection by vision model
  - image_generate                       — image generation
  - skill_view, skills_list              — Hermes' skill library
  - text_to_speech                       — TTS
  - kanban_* (complete/block/comment/    — kanban worker + orchestrator
    heartbeat/show/list/create/            handoff (stateless: read env var,
    unblock/link)                          write ~/.hermes/kanban.db)

What we DO NOT expose:
  - terminal / shell                     — codex's own shell tool
  - read_file / write_file / patch       — codex's apply_patch + shell
  - search_files / process               — codex's shell
  - clarify                              — codex's own UX
  - delegate_task / memory /             — `_AGENT_LOOP_TOOLS` in Hermes
    session_search / todo                  (model_tools.py). They require
                                           the running AIAgent context to
                                           dispatch (mid-loop state), so a
                                           stateless MCP callback can't
                                           drive them. See the inline
                                           comment on EXPOSED_TOOLS below.

Run with: python -m agent.transports.hermes_tools_mcp_server
Spawned by: CodexAppServerSession.ensure_started() or the task-scoped
            Claude primary adapter.
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
_JSON_TO_PY = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


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


# Codex-app compatibility tools. Each name MUST match a registered Hermes tool that
# `model_tools.handle_function_call()` can dispatch.
#
# What we deliberately DO NOT expose:
#   - terminal / shell / read_file / write_file / patch / search_files /
#     process — codex's built-ins cover these and approval routes through
#     codex's own UI.
#   - delegate_task / memory / session_search / todo — these are
#     `_AGENT_LOOP_TOOLS` in Hermes (model_tools.py:493). They require
#     the running AIAgent context to dispatch (mid-loop state), so a
#     stateless MCP callback can't drive them. Hermes' default runtime
#     keeps these working; the codex_app_server runtime cannot.
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
        "Call work_inbox_show before deciding, then finish with exactly one "
        "work_inbox_decide call. You cannot create, edit, claim, or move cards "
        "directly and cannot write repository files. Request clarification "
        "when essential information is missing. This run has no provider "
        "fallback; failure must leave the intake inert."
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
    """Create the MCP server with Hermes tools attached. Lazy imports
    so the module can be imported without the mcp package installed
    (we degrade to a clear error only when actually run)."""
    try:
        # mcp 2.0 removed `mcp.server.fastmcp`; `mcp.server.MCPServer` is the
        # same decorator/add_tool surface under the new name.
        from mcp.server import MCPServer
    except ImportError as exc:  # pragma: no cover - install hint
        raise ImportError(
            f"hermes-tools MCP server requires the 'mcp' package: {exc}"
        ) from exc

    # Discover Hermes tools so dispatch works.
    from model_tools import (
        get_tool_definitions,
        handle_function_call,
    )

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

    # Pull authoritative Hermes tool schemas for the ones we expose, so
    # MCP clients see the same parameter docs Hermes gives the model.
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

    selected = selected_tool_names()
    exposed_count = 0

    for name in selected:
        spec = all_defs.get(name)
        if spec is None:
            logger.debug(
                "skipping %s — not registered in this Hermes process", name
            )
            continue

        description = spec.get("description") or f"Hermes {name} tool"
        params_schema = spec.get("parameters") or {"type": "object", "properties": {}}

        # The SDK wants a Python callable and derives the input schema from
        # its signature — there is no inputSchema parameter on either the
        # decorator or add_tool(). So build a closure that takes the arguments
        # dict, dispatches via handle_function_call, returns the result
        # string, and carries a __signature__ synthesized from the Hermes
        # JSON Schema (see _signature_from_schema) for the SDK to read.
        def _make_handler(tool_name: str, schema: dict | None):
            sig, annots = _signature_from_schema(schema)

            def _dispatch(**kwargs: Any) -> str:
                try:
                    # Filter out None values before dispatch so unset optionals
                    # aren't forwarded to the handler.
                    args = {k: v for k, v in kwargs.items() if v is not None}
                    return handle_function_call(tool_name, args or {})
                except Exception as exc:
                    logger.exception("tool %s raised", tool_name)
                    return json.dumps({"error": str(exc), "tool": tool_name})

            _dispatch.__name__ = tool_name
            _dispatch.__doc__ = description
            _dispatch.__signature__ = sig
            _dispatch.__annotations__ = {**annots, "return": str}
            return _dispatch

        try:
            mcp.add_tool(
                _make_handler(name, params_schema),
                name=name,
                description=description,
            )
        except TypeError:
            # Older mcp SDK signature — fall back to decorator-style. The
            # synthesized __signature__ on the handler still drives schema
            # generation there.
            handler = _make_handler(name, params_schema)
            handler = mcp.tool(name=name, description=description)(handler)

        exposed_count += 1

    logger.info(
        "hermes-tools MCP server registered %d/%d tools",
        exposed_count,
        len(selected),
    )
    return mcp


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for `python -m agent.transports.hermes_tools_mcp_server`."""
    argv = argv or sys.argv[1:]
    verbose = "--verbose" in argv or "-v" in argv

    log_level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        stream=sys.stderr,  # MCP uses stdio for protocol — logs MUST go to stderr
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Quiet mode: keep Hermes' own banners off stdout (which is the MCP wire).
    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")

    try:
        server = _build_server()
    except ImportError as exc:
        sys.stderr.write(f"hermes-tools MCP server cannot start: {exc}\n")
        return 2

    # MCPServer.run() defaults to stdio transport, which is what codex
    # spawns us on.
    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.exception("hermes-tools MCP server crashed")
        sys.stderr.write(f"hermes-tools MCP server error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
