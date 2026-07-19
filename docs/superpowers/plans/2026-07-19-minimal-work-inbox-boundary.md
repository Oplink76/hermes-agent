# Minimal Work Inbox Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one authenticated Work Inbox POST that sends new work to existing qualification intake and exact assigned delivery to existing Normal Handover.

**Architecture:** Keep the endpoint as a thin adapter in the existing Kanban dashboard plugin. Add one fixed-scope bearer provider, validate two closed request bodies, then call `kanban_intake.submit_intake`, `kanban_db.complete_task`, or `kanban_db.block_task`; persist nothing new and run nothing in the background.

**Tech Stack:** Python 3, FastAPI/Pydantic, existing Hermes dashboard-auth and Kanban APIs, pytest through `scripts/run_tests.sh`.

## Global Constraints

- One implementation slice and one review gate.
- No new database table/column, receipt id, GET receipt route, state machine, lease, queue, watcher, daemon, scheduler, or retry protocol.
- `new_work` calls existing qualification intake and creates no task directly.
- `assigned_delivery` requires the exact running task, run, and Work Contract and calls existing complete/block with `expected_run_id`.
- Requalification, Work Contracts, worker tools, phase transitions, and Normal Handover remain unchanged.
- Fixed route `/api/plugins/kanban/work-inbox` and fixed scope `work_inbox:submit` only.
- Assigned metadata accepts only `ai_provenance`, `changed_files`, `tests_run`, and `workflow_outcome`.
- Use `scripts/run_tests.sh`; do not invoke pytest directly.

---

### Task 1: Add the thin authenticated Work Inbox adapter

**Files:**
- Create: `plugins/dashboard_auth/work_inbox/__init__.py`
- Create: `plugins/dashboard_auth/work_inbox/plugin.yaml`
- Modify: `plugins/kanban/dashboard/plugin_api.py`
- Modify: `hermes_cli/kanban_inbox_guide.py`
- Create: `tests/plugins/dashboard_auth/test_work_inbox_provider.py`
- Modify: `tests/plugins/test_kanban_dashboard_plugin.py`
- Modify: `tests/hermes_cli/test_kanban_inbox_guide.py`
- Create: `tests/hermes_cli/test_work_inbox_auth_security.py`

**Interfaces:**
- Produces: `WORK_INBOX_ROUTE_PATH = "/api/plugins/kanban/work-inbox"`.
- Produces: `WORK_INBOX_SCOPE = "work_inbox:submit"`.
- Produces: `WorkInboxSecretProvider` reading `HERMES_WORK_INBOX_SECRET`.
- Produces: `POST /api/plugins/kanban/work-inbox?board=<strict-board>`.
- Consumes unchanged: `kanban_intake.submit_intake`, `kanban_db.complete_task`, and `kanban_db.block_task`.

- [ ] **Step 1: Write failing provider and real bearer-path tests**

Provider behavior:

```python
def test_work_inbox_provider_has_one_fixed_scope(strong_secret):
    provider = WorkInboxSecretProvider(secret=strong_secret)
    principal = provider.verify_token(token=strong_secret)
    assert principal.principal == "work-inbox"
    assert principal.provider == "work-inbox-secret"
    assert principal.scopes == ("work_inbox:submit",)


def test_provider_rejects_wrong_or_weak_secret(strong_secret):
    provider = WorkInboxSecretProvider(secret=strong_secret)
    assert provider.verify_token(token="wrong") is None
    with pytest.raises(ValueError):
        WorkInboxSecretProvider(secret="weak")
```

Real security-path behavior must boot plugin discovery and use actual
`Authorization: Bearer` headers rather than assigning
`request.state.token_principal`:

```python
def test_exact_work_inbox_route_uses_real_bearer_middleware(app_client, strong_secret):
    accepted = app_client.post(
        "/api/plugins/kanban/work-inbox?board=strict",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={"version": 2, "kind": "new_work", "request": {}},
    )
    assert accepted.status_code != 401
    assert app_client.post(
        "/api/plugins/kanban/work-inbox?board=strict",
        headers={"Authorization": "Bearer wrong"},
        json={"version": 2, "kind": "new_work", "request": {}},
    ).status_code == 401
    assert app_client.post(
        "/api/plugins/kanban/work-inbox/other?board=strict",
        headers={"Authorization": f"Bearer {strong_secret}"},
        json={},
    ).status_code == 401
```

The existing dashboard auth gate rejects an unregistered token subpath before
router lookup, so the real app returns 401 rather than 404.

- [ ] **Step 2: Write failing new-work and assigned-delivery behavior tests**

Use real SQLite board state. Do not mock `submit_intake`, `complete_task`, or
`block_task`.

```python
def test_new_work_delegates_to_existing_intake(client, strict_board, token):
    response = client.post(
        f"/api/plugins/kanban/work-inbox?board={strict_board}",
        headers=token,
        json={
            "version": 2,
            "kind": "new_work",
            "request": {"functional_intent": {"title": "Small change"}},
            "session_id": "external-1",
            "attachments": [],
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "qualification_required"
    with kb.connect(board=strict_board) as conn:
        assert len(kb.list_qualification_intakes(conn, status="pending")) == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
```

```python
def test_exact_assigned_completion_uses_normal_handover(
    client, contracted_running_card, token,
):
    board, task_id, run_id, contract_id = contracted_running_card
    response = client.post(
        f"/api/plugins/kanban/work-inbox?board={board}",
        headers=token,
        json={
            "version": 2,
            "kind": "assigned_delivery",
            "task_id": task_id,
            "run_id": run_id,
            "work_contract_id": contract_id,
            "outcome": "completed",
            "summary": "Delivered the assigned change",
            "metadata": {
                "ai_provenance": {"writer": {"agent": "external"}}
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "handover_applied"
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
        assert task.current_step_key == "test"
```

Also test real assigned blocking, mismatched task/run/contract, Epic,
goal-mode, `release_measure`, non-running task, unknown/private metadata key,
unknown top-level key, and an existing patch sent as `new_work` evidence.
For every rejected assignment compare the task row and event count before and
after to prove no mutation.

- [ ] **Step 3: Write failing guide-v2 tests**

```python
def test_guide_names_one_minimal_work_inbox_route():
    guide = build_inbox_guide(board="strict", origin="https://hermes.example")
    assert guide["guide_version"] == 2
    assert guide["name"] == "Hermes Work Inbox"
    assert guide["submission"]["url"].endswith(
        "/api/plugins/kanban/work-inbox?board=strict"
    )
    assert set(guide["submission"]["kinds"]) == {
        "new_work", "assigned_delivery"
    }
    assert guide["submission"]["scope"] == "work_inbox:submit"
    assert guide["retry"]["automatic_retry"] is False
    assert "receipt" not in guide
```

Assert the guide includes neither a token nor task/contract data, board list,
signing material, or filesystem path.

- [ ] **Step 4: Verify RED**

```bash
scripts/run_tests.sh tests/plugins/dashboard_auth/test_work_inbox_provider.py tests/hermes_cli/test_work_inbox_auth_security.py tests/plugins/test_kanban_dashboard_plugin.py tests/hermes_cli/test_kanban_inbox_guide.py -q
```

Expected: provider/route imports fail and guide assertions report version 1.

- [ ] **Step 5: Implement the fixed-scope provider**

Create a non-interactive `DashboardAuthProvider` that:

```python
WORK_INBOX_ROUTE_PATH = "/api/plugins/kanban/work-inbox"
WORK_INBOX_SCOPE = "work_inbox:submit"
```

- imports and reuses `assess_secret_strength` from the bundled drain provider;
- validates again in `__init__`;
- compares with `hmac.compare_digest`;
- returns principal `work-inbox`, provider `work-inbox-secret`, and the one
  fixed scope;
- reads only `HERMES_WORK_INBOX_SECRET` in `register(ctx)`; and
- registers only `WORK_INBOX_ROUTE_PATH` with `register_token_route`.

No config keys or alternative scopes are added.

- [ ] **Step 6: Implement the closed POST adapter**

Add two Pydantic bodies discriminated by `kind` and forbid extra fields.
Require version 2 and an explicit strict board.

For `new_work`, return the existing intake response with HTTP 202:

```python
return kanban_intake.submit_intake(
    conn,
    request=payload.request,
    source=f"work-inbox:{principal.provider}:{principal.principal}",
    session_id=payload.session_id,
    attachments=tuple(payload.attachments),
)
```

For `assigned_delivery`, verify the exact task/run/contract tuple and active
run, reject all metadata keys outside:

```python
{"ai_provenance", "changed_files", "tests_run", "workflow_outcome"}
```

Force-redact summary/result/metadata using `redact_sensitive_text(...,
force=True)`, then call the existing operation:

```python
ok = kanban_db.complete_task(
    conn,
    payload.task_id,
    result=result,
    summary=summary,
    metadata=metadata or None,
    expected_run_id=payload.run_id,
    board=board,
)
```

or:

```python
ok = kanban_db.block_task(
    conn,
    payload.task_id,
    reason=summary,
    kind=payload.block_kind,
    attempted_resolutions=payload.attempted_resolutions,
    expected_run_id=payload.run_id,
    board=board,
)
```

Return 409 when `ok` is false or a named existing handover policy exception
is raised. Do not add idempotency or recovery code: the existing run CAS is
the mutation guard.

- [ ] **Step 7: Rewrite only the public guide payload**

Set `GUIDE_VERSION = 2`, name it `Hermes Work Inbox`, show the two exact body
examples, state the fixed scope, and explicitly say automatic retry is false.
Remove the new `receipt` section; retain the existing public board/origin
validation in `web_server.py` unchanged.

- [ ] **Step 8: Verify GREEN and full compatibility**

```bash
scripts/run_tests.sh tests/plugins/dashboard_auth/test_work_inbox_provider.py tests/hermes_cli/test_work_inbox_auth_security.py tests/plugins/test_kanban_dashboard_plugin.py tests/hermes_cli/test_kanban_inbox_guide.py tests/hermes_cli/test_dashboard_token_auth.py tests/hermes_cli/test_kanban_intake_db.py tests/hermes_cli/test_kanban_qualifier.py tests/e2e/test_kanban_qualified_product_flow.py -q
git diff --check
```

Expected: every test passes and no database schema or background-processing
file appears in the diff.

- [ ] **Step 9: Commit**

```bash
git add plugins/dashboard_auth/work_inbox plugins/kanban/dashboard/plugin_api.py hermes_cli/kanban_inbox_guide.py tests/plugins/dashboard_auth/test_work_inbox_provider.py tests/plugins/test_kanban_dashboard_plugin.py tests/hermes_cli/test_kanban_inbox_guide.py tests/hermes_cli/test_work_inbox_auth_security.py
git commit -m "feat(kanban): add minimal Hermes Work Inbox"
```

## Self-Review

- Spec coverage: one authenticated door, new-work intake, exact assigned
  delivery, existing Normal Handover, guide v2, and unchanged requalification
  are covered.
- Scope: eight source/test paths; no persistence, GET receipt, recovery, or
  worker-tool refactor.
- Type consistency: one fixed route, one fixed scope, version 2, and existing
  integer `run_id`/string task and Work Contract ids throughout.
