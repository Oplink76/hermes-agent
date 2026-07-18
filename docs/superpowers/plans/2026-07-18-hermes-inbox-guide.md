# Hermes Inbox Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a public, board-specific Hermes inbox guide with a copy-ready prompt that tells external AIs how to submit inert work intent through the existing qualified intake.

**Architecture:** A small pure builder owns the safe JSON contract and prompt. The existing Hermes FastAPI server exposes it at `GET /.well-known/hermes-inbox?board=<slug>`, validates the board through existing Kanban metadata helpers, and explicitly allows only that exact read-only path through both dashboard auth gates. The existing authenticated intake and receipt endpoints remain unchanged.

**Tech Stack:** Python 3, FastAPI, existing Hermes Kanban SQLite metadata, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Keep this first slice limited to the public guide and copy-ready prompt.
- Reuse `POST /api/plugins/kanban/intake` and `GET /api/plugins/kanban/intake/{intake_id}`; create no second inbox.
- Do not implement Agent Memory, scoped bearer tokens, idempotency enforcement, qualification, routing, or board mutation in this slice.
- The public response must contain no credential, dashboard session token, signing material, filesystem path, task content, or board listing.
- Return `400` for a missing or malformed board slug, `404` for an unknown board, and `409` when the board does not require qualified intake.
- State the current authentication boundary honestly: submission requires an authenticated Hermes API context, and the public guide never returns credentials.
- Use `scripts/run_tests.sh`, never direct `pytest`.

---

### Task 1: Define the safe guide and prompt contract

**Files:**
- Create: `hermes_cli/kanban_inbox_guide.py`
- Create: `tests/hermes_cli/test_kanban_inbox_guide.py`

**Interfaces:**
- Consumes: normalized `board: str` and request `origin: str`.
- Produces: `build_inbox_guide(*, board: str, origin: str) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing pure-contract test**

```python
from __future__ import annotations

import json

from hermes_cli.kanban_inbox_guide import build_inbox_guide


def test_build_inbox_guide_is_copy_ready_and_contains_no_credential():
    guide_url = "https://hermes.example/.well-known/hermes-inbox?board=strict"

    body = build_inbox_guide(
        board="strict",
        origin="https://hermes.example",
    )

    assert body["guide_version"] == 1
    assert body["board"] == "strict"
    assert body["submission"]["method"] == "POST"
    assert body["submission"]["url"] == (
        "https://hermes.example/api/plugins/kanban/intake?board=strict"
    )
    assert body["receipt"]["url_template"] == (
        "https://hermes.example/api/plugins/kanban/intake/{intake_id}?board=strict"
    )
    assert body["submission"]["example"]["request"]["client_request_id"]
    assert guide_url in body["copy_ready_prompt"]
    assert "Do not create, edit, assign, route, qualify, or override" in (
        body["copy_ready_prompt"]
    )
    serialized = json.dumps(body)
    assert "secret" not in serialized.lower()
    assert "canonical_json" not in serialized
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_kanban_inbox_guide.py -q
```

Expected: FAIL because `hermes_cli.kanban_inbox_guide` does not exist.

- [ ] **Step 3: Implement the minimal pure builder**

Create `hermes_cli/kanban_inbox_guide.py` with one public builder. It must:

```python
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode


GUIDE_VERSION = 1


def build_inbox_guide(
    *,
    board: str,
    origin: str,
) -> dict[str, Any]:
    query = urlencode({"board": board})
    guide_url = f"{origin}/.well-known/hermes-inbox?{query}"
    intake_url = f"{origin}/api/plugins/kanban/intake?{query}"
    receipt_url = (
        f"{origin}/api/plugins/kanban/intake/{{intake_id}}?{query}"
    )
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
```

- [ ] **Step 4: Run the test and verify GREEN**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_kanban_inbox_guide.py -q
```

Expected: PASS with no warnings.

- [ ] **Step 5: Commit the contract**

```bash
git add hermes_cli/kanban_inbox_guide.py tests/hermes_cli/test_kanban_inbox_guide.py
git commit -m "feat: define Hermes inbox guide contract"
```

---

### Task 2: Expose the guide through the public Hermes route

**Files:**
- Modify: `tests/hermes_cli/test_kanban_inbox_guide.py`
- Modify: `hermes_cli/dashboard_auth/public_paths.py`
- Modify: `hermes_cli/web_server.py`

**Interfaces:**
- Consumes: `build_inbox_guide(...)`, existing `kanban_db` board helpers, and `kanban_intake.qualification_required(...)`.
- Produces: `GET /.well-known/hermes-inbox?board=<board-slug>`.

- [ ] **Step 1: Add failing endpoint behavior tests**

Extend the imports and append these fixtures/tests to `tests/hermes_cli/test_kanban_inbox_guide.py`:

```python
import pytest
from fastapi.testclient import TestClient

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db, web_server


@pytest.fixture
def strict_board() -> str:
    kanban_db.ensure_product_board_defaults("strict")
    metadata_path = kanban_db.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )
    return "strict"


@pytest.fixture
def client():
    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
    }
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    yield TestClient(
        web_server.app,
        base_url="http://127.0.0.1:9119",
    )
    for key, value in previous.items():
        setattr(web_server.app.state, key, value)


def test_public_endpoint_returns_guide_without_dashboard_auth(client, strict_board):
    response = client.get(
        "/.well-known/hermes-inbox", params={"board": strict_board}
    )

    assert response.status_code == 200
    assert response.json()["board"] == strict_board
    assert web_server._SESSION_TOKEN not in response.text
    assert str(get_hermes_home()) not in response.text


def test_public_endpoint_bypasses_oauth_gate(strict_board):
    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
    }
    web_server.app.state.bound_host = "hermes.example"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    try:
        gated_client = TestClient(
            web_server.app,
            base_url="https://hermes.example",
        )
        response = gated_client.get(
            "/.well-known/hermes-inbox", params={"board": strict_board}
        )
    finally:
        for key, value in previous.items():
            setattr(web_server.app.state, key, value)

    assert response.status_code == 200


def test_public_endpoint_rejects_missing_or_malformed_board(client):
    assert client.get("/.well-known/hermes-inbox").status_code == 400
    assert client.get(
        "/.well-known/hermes-inbox", params={"board": "../bad"}
    ).status_code == 400


def test_public_endpoint_rejects_unknown_board(client):
    response = client.get(
        "/.well-known/hermes-inbox", params={"board": "missing"}
    )
    assert response.status_code == 404


def test_public_endpoint_rejects_non_strict_board(client):
    kanban_db.create_board("plain")
    response = client.get(
        "/.well-known/hermes-inbox", params={"board": "plain"}
    )
    assert response.status_code == 409
```

- [ ] **Step 2: Run the endpoint tests and verify RED**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_kanban_inbox_guide.py -q
```

Expected: endpoint assertions FAIL with `404` because the route is absent.

- [ ] **Step 3: Make the exact route public**

Change the module description to `"""Shared allowlist of exact paths that bypass dashboard auth.` and add the exact path near the top of `PUBLIC_API_PATHS`:

```python
PUBLIC_API_PATHS: frozenset[str] = frozenset({
    # Public, read-only directions for governed external work intake.
    # Board validation happens in the handler and the response contains no
    # credential, task content, signing material, or board listing.
    "/.well-known/hermes-inbox",
    "/api/status",
    # Existing entries remain unchanged below.
})
```

Do not add a public prefix.

- [ ] **Step 4: Implement the endpoint**

Add a synchronous route near `/api/status` in `hermes_cli/web_server.py`:

```python
@app.get("/.well-known/hermes-inbox")
def get_hermes_inbox_guide(request: Request, board: Optional[str] = None):
    from hermes_cli import kanban_db, kanban_intake
    from hermes_cli.kanban_inbox_guide import build_inbox_guide

    if not board:
        raise HTTPException(status_code=400, detail="board query parameter is required")
    try:
        normalized = kanban_db._normalize_board_slug(board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not normalized:
        raise HTTPException(status_code=400, detail="board query parameter is required")
    if not kanban_db.board_exists(normalized):
        raise HTTPException(status_code=404, detail=f"board {normalized!r} does not exist")
    metadata = kanban_db.read_board_metadata(normalized)
    if not kanban_intake.qualification_required(metadata):
        raise HTTPException(
            status_code=409,
            detail="This board does not require qualified intake",
        )
    origin = str(request.base_url).rstrip("/")
    return build_inbox_guide(
        board=normalized,
        origin=origin,
    )
```

- [ ] **Step 5: Run the endpoint tests and verify GREEN**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_kanban_inbox_guide.py -q
```

Expected: all guide tests PASS.

- [ ] **Step 6: Run focused auth and intake regressions**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_dashboard_auth_gate.py -q
scripts/run_tests.sh tests/hermes_cli/test_dashboard_auth_middleware.py -q
scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py -q
```

Expected: all existing tests PASS; no authentication or intake behavior changes.

- [ ] **Step 7: Commit the endpoint**

```bash
git add hermes_cli/dashboard_auth/public_paths.py hermes_cli/web_server.py tests/hermes_cli/test_kanban_inbox_guide.py
git commit -m "feat: expose public Hermes inbox guide"
```

---

### Task 3: Verify the finished first slice

**Files:**
- Verify only; no production files should change.

**Interfaces:**
- Consumes: committed public guide endpoint.
- Produces: review evidence and the exact copy-ready prompt for Ole.

- [ ] **Step 1: Run change hygiene checks**

```bash
git diff origin/main...HEAD --check
git status --short
```

Expected: no whitespace errors and no uncommitted source/test changes.

- [ ] **Step 2: Review against the written design**

Confirm the diff implements only the guide slice, exposes no sensitive values, reuses the existing intake, and does not touch Kanban state, Agent Memory, Trading, Cockpit, or worker orchestration.

- [ ] **Step 3: Record the operator prompt**

Return the endpoint's `copy_ready_prompt` verbatim to Ole. If no public Hermes origin is deployed yet, state that explicitly rather than substituting a Cockpit URL.
