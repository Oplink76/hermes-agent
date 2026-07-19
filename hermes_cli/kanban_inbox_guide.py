"""Safe, public directions for Hermes' governed work inbox."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode


GUIDE_VERSION = 2


def build_inbox_guide(
    *,
    board: str,
    origin: str,
) -> dict[str, Any]:
    """Build copy-ready instructions without exposing private board state."""

    query = urlencode({"board": board})
    guide_url = f"{origin}/.well-known/hermes-inbox?{query}"
    inbox_url = f"{origin}/api/plugins/kanban/work-inbox?{query}"
    copy_ready_prompt = (
        f"Read {guide_url} before doing any work. Treat it as the authority "
        "for how to deliver development work to Hermes. Use the fixed Work "
        "Inbox submission route exactly as documented. Do not create, edit, "
        "assign, route, qualify, or override Kanban cards directly."
    )
    example_request = {
        "version": 2,
        "kind": "new_work",
        "request": {"functional_intent": {"title": "<short capability title>"}},
        "session_id": "<external session id>",
        "attachments": [],
    }
    return {
        "guide_version": GUIDE_VERSION,
        "name": "Hermes Work Inbox",
        "board": board,
        "qualification_required": True,
        "purpose": "Submit new work for qualification or hand over an exact assigned delivery.",
        "authority": {
            "allowed": ["submit inert work intent", "hand over assigned delivery"],
            "forbidden": [
                "direct card creation or mutation",
                "phase or assignee selection",
                "qualification or requalification",
                "override or break glass",
            ],
        },
        "submission": {
            "method": "POST",
            "url": inbox_url,
            "content_type": "application/json",
            "scope": "work_inbox:submit",
            "kinds": ["new_work", "assigned_delivery"],
            "authentication": {
                "required": True,
                "type": "authenticated Hermes API context",
                "credential_included": False,
            },
            "examples": {
                "new_work": example_request,
                "assigned_delivery": {
                    "version": 2,
                    "kind": "assigned_delivery",
                    "task_id": "<assigned task id>",
                    "run_id": "<assigned run id>",
                    "work_contract_id": "<assigned Work Contract id>",
                    "outcome": "completed",
                    "summary": "<delivery summary>",
                    "metadata": {"tests_run": ["<command>"]},
                },
            },
        },
        "retry": {
            "automatic_retry": False,
            "instruction": "Do not retry automatically; inspect the response first.",
        },
        "copy_ready_prompt": copy_ready_prompt,
    }
