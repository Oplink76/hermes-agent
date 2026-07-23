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
        "You are a local AI outside the Hermes-managed framework. "
        f"Read {guide_url} before doing any work. Treat it as the authority "
        "for how to deliver development work to Hermes. Submit new work only "
        "after Ole has approved it to enter Hermes. Admission does not "
        "authorize execution; Hermes qualification and a signed Work Contract "
        "remain required. Submit the complete handoff document once "
        "and include its full text in attachments[].content. Do not split it into "
        "cards or choose phases, assignees, Epics, or dependencies; Hermes owns "
        "qualification and decomposition. Use assigned_delivery only when Hermes "
        "provided the exact task, run, and Work Contract identifiers. Use the "
        "fixed Work Inbox submission route exactly as documented. Do not create, "
        "edit, assign, route, qualify, or override Kanban cards directly."
    )
    new_work_instructions = (
        "Submit the complete handoff document once as one new_work request. "
        "Include the full document text in attachments[].content; a path or URL "
        "alone is not sufficient. Do not split the document into cards or choose "
        "phases, assignees, Epics, or dependencies. Hermes owns qualification and "
        "decomposition. You may include suggested segments inside the document as "
        "advisory context only. Attach an externally authored handoff, work brief, "
        "or design specification as source evidence; external authorship is not "
        "Product Owner evidence. Use assigned_delivery only for an exact active "
        "Hermes assignment with task_id, run_id, and work_contract_id."
    )
    example_request = {
        "version": 2,
        "kind": "new_work",
        "request": {
            "functional_intent": {
                "title": "<short capability title>",
                "outcome": "<measurable user or operational outcome>",
            }
        },
        "session_id": "<external session id>",
        "attachments": [
            {
                "kind": "handoff_document",
                "name": "handoff.md",
                "media_type": "text/markdown",
                "content": "<complete handoff document text>",
            }
        ],
    }
    return {
        "guide_version": GUIDE_VERSION,
        "name": "Hermes Work Inbox",
        "board": board,
        "qualification_required": True,
        "purpose": (
            "Submit Ole-approved local work for framework qualification or hand "
            "over an exact assigned delivery."
        ),
        "authority": {
            "allowed": [
                "submit Ole-approved work for framework processing",
                "hand over assigned delivery",
            ],
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
            "new_work_instructions": new_work_instructions,
            "authentication": {
                "required": True,
                "type": "bearer",
                "authorization_header": "Authorization: Bearer <machine credential>",
                "credential_included": False,
            },
            "examples": {
                "new_work": example_request,
                "assigned_delivery": {
                    "version": 2,
                    "kind": "assigned_delivery",
                    "task_id": "<assigned task id>",
                    "run_id": 123,
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
