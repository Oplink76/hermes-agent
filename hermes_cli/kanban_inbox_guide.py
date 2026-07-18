"""Safe, public directions for Hermes' governed work inbox."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode


GUIDE_VERSION = 1


def build_inbox_guide(
    *,
    board: str,
    origin: str,
) -> dict[str, Any]:
    """Build copy-ready instructions without exposing private board state."""

    query = urlencode({"board": board})
    guide_url = f"{origin}/.well-known/hermes-inbox?{query}"
    intake_url = f"{origin}/api/plugins/kanban/intake?{query}"
    receipt_url = f"{origin}/api/plugins/kanban/intake/{{intake_id}}?{query}"
    copy_ready_prompt = (
        f"Read {guide_url} before doing any work. Treat it as the authority "
        "for how to deliver development work to Hermes. Submit the intended "
        "outcome through the Hermes inbox exactly as documented. Do not "
        "create, edit, assign, route, qualify, or override Kanban cards "
        "directly. If you cannot authenticate or required information is "
        "missing, tell me what is missing instead of bypassing Hermes."
    )
    example_request = {
        "version": 1,
        "client_request_id": "<stable-id-from-your-system>",
        "functional_intent": {
            "title": "<short capability title>",
            "desired_outcome": "<measurable user-visible outcome>",
            "project": "<project identity>",
            "repository": "<repository URL or path when known>",
            "scope": ["<included behavior>"],
            "out_of_scope": ["<excluded behavior>"],
            "aliases": [],
        },
        "evidence": [],
        "source": {
            "agent": "<agent name>",
            "session_id": "<session id>",
        },
    }
    return {
        "guide_version": GUIDE_VERSION,
        "name": "Hermes Qualified Work Inbox",
        "board": board,
        "qualification_required": True,
        "purpose": (
            "Submit intended work for Hermes qualification without creating "
            "or routing Kanban cards directly."
        ),
        "authority": {
            "allowed": ["submit inert work intent", "read its intake receipt"],
            "forbidden": [
                "direct card creation or mutation",
                "phase or assignee selection",
                "qualification or requalification",
                "override or break glass",
            ],
        },
        "submission": {
            "method": "POST",
            "url": intake_url,
            "content_type": "application/json",
            "authentication": {
                "required": True,
                "type": "authenticated Hermes API context",
                "credential_included": False,
            },
            "example": {
                "request": example_request,
                "session_id": "<session id>",
                "attachments": [],
            },
        },
        "receipt": {
            "method": "GET",
            "url_template": receipt_url,
            "states": ["pending", "qualified", "rejected", "overridden"],
        },
        "retry": {
            "client_request_id_required": True,
            "automatic_retry": False,
            "instruction": (
                "Keep the returned intake_id and check its receipt before "
                "retrying."
            ),
        },
        "copy_ready_prompt": copy_ready_prompt,
    }
