# Normal-Handover Requalification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Requalify qualified scheduled development cards through Hermes's existing intake, signed-contract, handover, dispatcher, and reconcile flow.

**Architecture:** `kanban_intake` owns the existing-task intake snapshot and atomic successor-contract application. `kanban_qualifier` continues to make and validate the route decision, with requalification-specific guardrails. The existing `kanban_db.reconcile()` loop enqueues at most one eligible scheduled card per pass; the existing gateway qualification sweep processes it on the next pass.

**Tech Stack:** Python 3, SQLite, HMAC-signed Work Contracts, pytest through `scripts/run_tests.sh`.

## Global Constraints

- No new lifecycle status, worker profile, daemon, scheduler, controller, model tool, or dependency.
- Qualification paths remain `po` and `hermes`; `override` remains Ole's authenticated Hermes-only break glass and cannot be used for requalification.
- Codex, Claude, Cockpit, and other clients may submit ordinary inert intake but cannot write requalification intake or contract-owned routing directly.
- Requalification must update the existing task atomically and retain the old immutable Work Contract.
- Development sequencing uses dependencies; age alone never changes a card.
- The first automatic recovery slice covers qualified `scheduled` cards on strict handoff-v2 product boards only.
- Valid dependency waits, explicit blockers, active workers, `release_measure`, `done`, `archived`, generic boards, and Trading-board activation are out of scope.
- All tests run through `scripts/run_tests.sh`, never direct `pytest`.

---

## File Structure

- Modify `hermes_cli/kanban_intake.py`: parse intake kind, capture bounded authoritative evidence, submit Hermes-owned requalification intake, and atomically apply a successor contract to the existing card.
- Modify `hermes_cli/kanban_qualifier.py`: tell the existing qualifier when intake targets an existing card and deterministically reject identity or break-glass misuse.
- Modify `hermes_cli/kanban_db.py`: enforce the service-only intake boundary and add the one-card bounded reconcile trigger.
- Modify `tests/hermes_cli/test_kanban_intake_db.py`: cover service authority, same-card materialization, audit history, routing/dependency/Epic replacement, state derivation, and rollback.
- Modify `tests/hermes_cli/test_kanban_qualifier.py`: cover prompt context and normal qualifier processing for an existing card.
- Modify `tests/hermes_cli/test_kanban_intake_db.py`: also cover bounded automatic enqueue and exclusions against real signed contracts.
- Modify `docs/plans/2026-07-18-normal-handover-requalification-design.md`: mark the reviewed design implemented only after verification.

---

### Task 1: Hermes-Owned Existing-Task Intake

**Files:**
- Modify: `tests/hermes_cli/test_kanban_intake_db.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/kanban_intake.py`

**Interfaces:**
- Produces: `intake_payload(intake: Mapping[str, Any]) -> dict[str, Any]`
- Produces: `submit_requalification(conn, *, task_id: str, reason: str) -> dict[str, Any]`
- Receipt: `{"status": "requalification_required", "intake_id": str, "intake_status": "pending", "task_id": str}`
- Raw intake keys: `kind`, `target_task_id`, `reason`, and `evidence`; evidence contains `task`, `contract`, `dependencies`, `epic_id`, `runs`, `events`, and `comments`.

- [ ] **Step 1: Write the failing authority and idempotency tests**

```python
@pytest.fixture
def strict_board_connection(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.ensure_product_board_defaults("strict")
    metadata_path = kb.board_metadata_path("strict")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with kb.connect(board="strict") as connection:
        yield connection


def _materialized_scheduled_card(connection, board="strict"):
    request_id = kb.create_qualification_intake(
        connection,
        raw_request=json.dumps({
            "kind": "task_create",
            "request": {"evidence": ["backlog-artifact", "architecture-artifact"]},
        }),
        source="hermes",
        attachments=[{"name": "backlog-artifact"}, {"name": "architecture-artifact"}],
    )
    task_id = intake.materialize_contract(
        connection,
        board=board,
        signed_contract=_signed_contract(request_id),
        secret=b"test-only-secret",
    )
    assert kb.schedule_task(connection, task_id, reason="no wake action")
    return task_id


def test_requalification_intake_requires_hermes_service_authority(strict_board_connection):
    conn = strict_board_connection
    task_id = _materialized_scheduled_card(conn)
    raw = json.dumps({"kind": "task_requalification", "target_task_id": task_id})
    with pytest.raises(sqlite3.IntegrityError, match="Hermes service authority"):
        kb.create_qualification_intake(conn, raw_request=raw, source="codex")


def test_submit_requalification_is_inert_durable_and_idempotent(strict_board_connection):
    conn = strict_board_connection
    task_id = _materialized_scheduled_card(conn)
    first = intake.submit_requalification(conn, task_id=task_id, reason="no wake action")
    second = intake.submit_requalification(conn, task_id=task_id, reason="no wake action")
    assert first == second
    assert kb.get_task(conn, task_id).status == "scheduled"
    assert len(kb.list_qualification_intakes(conn, status="pending")) == 1
    assert intake.intake_payload(kb.get_qualification_intake(conn, first["intake_id"]))[
        "target_task_id"
    ] == task_id
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake_db.py -q`

Expected: failure because the service-only trigger and `submit_requalification` do not exist.

- [ ] **Step 3: Add the strict-board trigger and minimal intake functions**

Add a `strict_requalification_intake_service_insert` trigger beside the other qualification-boundary triggers:

```sql
CREATE TRIGGER IF NOT EXISTS strict_requalification_intake_service_insert
BEFORE INSERT ON qualification_intake
WHEN (SELECT qualification_required FROM board_governance WHERE id = 1) = 1
 AND json_valid(NEW.raw_request) = 1
 AND json_extract(NEW.raw_request, '$.kind') = 'task_requalification'
 AND hermes_governance_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'requalification intake requires Hermes service authority');
END;
```

Add these functions in `kanban_intake.py`:

```python
def intake_payload(intake: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(intake.get("raw_request") or ""))
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def submit_requalification(conn: Any, *, task_id: str, reason: str) -> dict[str, Any]:
    # Validate a qualified, nonterminal, idle card; return any existing pending
    # intake for the same target. Capture the task snapshot, current contract,
    # dependencies, Epic membership, and the latest 50 runs/events/comments.
    # Insert under authorized_governance_write(), append
    # requalification_requested, wake the existing qualifier, and return receipt.
```

- [ ] **Step 4: Run the test file and confirm GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake_db.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the intake boundary**

```bash
git add hermes_cli/kanban_db.py hermes_cli/kanban_intake.py tests/hermes_cli/test_kanban_intake_db.py
git commit -m "feat(kanban): add Hermes-owned requalification intake"
```

---

### Task 2: Atomic Successor Contract on the Same Card

**Files:**
- Modify: `tests/hermes_cli/test_kanban_intake_db.py`
- Modify: `hermes_cli/kanban_intake.py`

**Interfaces:**
- Consumes: `intake_payload(...)` and the existing `materialization_fields(...)`.
- Produces: `_apply_requalification(conn, *, board: str, intake_record: Mapping[str, Any], contract_id: str, signed_contract: Mapping[str, Any], fields: Mapping[str, Any]) -> str`.
- Changes: `materialize_contract(...)` chooses task creation for `task_create` and same-task application for `task_requalification`.

- [ ] **Step 1: Write failing same-card, state, and rollback tests**

```python
def test_successor_contract_requalifies_same_card_and_preserves_audit(
    strict_board_connection,
):
    conn = strict_board_connection
    task_id = _materialized_scheduled_card(conn)
    old_contract_id = kb.get_task(conn, task_id).work_contract_id
    receipt = intake.submit_requalification(conn, task_id=task_id, reason="resume governed flow")
    contract = _signed_contract(receipt["intake_id"])["contract"]
    contract["work"]["title"] = "Requalified card"
    contract["routing"]["entry_phase"] = "development"
    contract["routing"]["assignee"] = "developer"
    successor = intake.sign_work_contract(contract, secret=b"test-only-secret")
    assert intake.materialize_contract(conn, board="strict", signed_contract=successor,
                                       secret=b"test-only-secret") == task_id
    card = kb.get_task(conn, task_id)
    assert card.work_contract_id != old_contract_id
    assert kb.get_work_contract(conn, old_contract_id) is not None
    assert card.current_step_key == "development" and card.assignee == "developer"
    assert card.title == "Requalified card"
    assert card.status == "ready"
    event = [e for e in kb.list_events(conn, task_id) if e.kind == "requalified"][-1]
    assert event.payload["old_work_contract_id"] == old_contract_id
    assert event.payload["new_work_contract_id"] == card.work_contract_id


def test_requalification_waits_in_todo_when_successor_dependency_is_open(
    strict_board_connection,
):
    # Materialize a second qualified card as the unfinished parent, include its
    # id in the successor contract, apply the successor, and inspect the target.
    assert kb.get_task(conn, task_id).status == "todo"


def test_requalification_rejects_override_and_rolls_back(strict_board_connection):
    conn = strict_board_connection
    task_id = _materialized_scheduled_card(conn)
    receipt = intake.submit_requalification(conn, task_id=task_id, reason="resume")
    contract = _signed_contract(receipt["intake_id"])["contract"]
    contract["qualification_path"] = "override"
    contract["override_authority"] = {
        "reason": "not ordinary requalification",
        "source_session": "session-1",
        "instruction_ref": "message-1",
    }
    signed = intake.sign_work_contract(contract, secret=b"test-only-secret")
    before = kb.get_task(conn, task_id).work_contract_id
    with pytest.raises(intake.WorkContractError, match="break-glass override"):
        intake.materialize_contract(
            conn, board="strict", signed_contract=signed,
            secret=b"test-only-secret",
        )
    assert kb.get_task(conn, task_id).work_contract_id == before
    assert kb.get_qualification_intake(conn, receipt["intake_id"])["status"] == "pending"
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake_db.py -q`

Expected: the existing materializer creates a second card or rejects the new path instead of updating the target.

- [ ] **Step 3: Implement the minimal atomic application**

Inside the existing outer `write_txn` in `materialize_contract(...)`:

```python
payload = intake_payload(intake_record)
if payload.get("kind") == "task_requalification":
    if contract["qualification_path"] == "override":
        raise WorkContractError("requalification cannot use break-glass override")
    task_id = _apply_requalification(
        conn,
        board=board,
        intake_record=intake_record,
        contract_id=contract_id,
        signed_contract=signed_contract,
        fields=fields,
    )
else:
    task_id = kanban_db.create_task(
        conn,
        title=str(fields["title"]),
        body=str(fields["body"]),
        assignee=fields["assignee"],
        created_by="hermes-qualification",
        parents=tuple(fields["parents"]),
        board=board,
        workflow_template_id=fields["workflow_template_id"],
        current_step_key=fields["current_step_key"],
        work_contract_id=contract_id,
        work_item_kind=fields["work_item_kind"],
    )
```

`_apply_requalification` must require the target's snapshot to still be idle and `scheduled`, require the same `work_item_kind`, replace dependency links and Epic membership under `authorized_governance_write()`, update contract-owned fields, append `requalified`, and call the existing `unblock_task()` to derive `todo` or `ready`. Any validation error must roll back the contract, decision, and task changes together.

- [ ] **Step 4: Run the test file and confirm GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake_db.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit successor application**

```bash
git add hermes_cli/kanban_intake.py tests/hermes_cli/test_kanban_intake_db.py
git commit -m "feat(kanban): apply successor contracts to existing cards"
```

---

### Task 3: Existing Qualifier Processes Requalification

**Files:**
- Modify: `tests/hermes_cli/test_kanban_qualifier.py`
- Modify: `hermes_cli/kanban_qualifier.py`

**Interfaces:**
- Consumes: `intake_payload(...)` and unchanged `qualify_intake(...)` API.
- Produces no new worker, watcher, or model-call surface.

- [ ] **Step 1: Write failing qualifier-flow tests**

```python
def test_qualifier_requalifies_existing_card_through_normal_path(
    strict_board_connection,
):
    conn = strict_board_connection
    task_id = _materialized_scheduled_card(conn)
    receipt = intake.submit_requalification(conn, task_id=task_id, reason="scheduled has no wake")
    decision = _decision(
        routing={
            "entry_phase": "development", "assignee": "developer",
            "epic_id": None, "dependencies": [],
        },
        entry_assessment=_late_assessment("backlog", "architecture"),
        handover={
            "deliverables": ["implementation"], "required_evidence": ["tests"],
            "done_when": ["green"], "next_phase": "test", "next_role": "tester",
        },
    )
    result = qualifier.qualify_intake(
        conn, board="strict", intake_id=receipt["intake_id"],
        model_call=lambda prompt: decision,
        secret=b"test-only-secret", issued_at=1_784_270_001,
    )
    assert result["status"] == "qualified"
    assert result["task_id"] == task_id
    assert kb.get_task(conn, task_id).status in {"todo", "ready"}


def test_requalification_decision_cannot_change_card_identity_or_depend_on_itself(
    strict_board_connection,
):
    conn = strict_board_connection
    task_id = _materialized_scheduled_card(conn)
    receipt = intake.submit_requalification(conn, task_id=task_id, reason="resume")
    record = kb.get_qualification_intake(conn, receipt["intake_id"])
    decision = _decision(
        routing={
            "entry_phase": "development", "assignee": "developer",
            "epic_id": None, "dependencies": [task_id],
        },
        entry_assessment=_late_assessment("backlog", "architecture"),
        handover={
            "deliverables": ["implementation"], "required_evidence": ["tests"],
            "done_when": ["green"], "next_phase": "test", "next_role": "tester",
        },
    )
    decision["work"]["item_kind"] = "epic"
    with pytest.raises(qualifier.QualificationValidationError) as exc:
        qualifier.validate_decision(
            conn,
            board_metadata=kb.read_board_metadata("strict"),
            intake=record,
            decision=decision,
        )
    assert "preserve the existing work item kind" in str(exc.value)
    assert "cannot depend on itself" in str(exc.value)
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_qualifier.py -q`

Expected: prompt/validation does not yet recognize the existing-task contract.

- [ ] **Step 3: Add requalification context and deterministic guards**

Parse the intake once in `build_qualification_prompt()` and `validate_decision()`:

```python
payload = kanban_intake.intake_payload(intake)
target_task_id = payload.get("target_task_id") if payload.get("kind") == "task_requalification" else None
```

For requalification, instruct the existing qualifier to preserve task identity, use only captured evidence, avoid rerunning proven phases, and express sequencing as dependencies. Deterministically require the existing `work_item_kind`, reject self-dependency, and leave ordinary intake behavior unchanged.

- [ ] **Step 4: Run qualifier and intake tests and confirm GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_qualifier.py tests/hermes_cli/test_kanban_intake_db.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit qualifier support**

```bash
git add hermes_cli/kanban_qualifier.py tests/hermes_cli/test_kanban_qualifier.py
git commit -m "feat(kanban): route requalification through existing qualifier"
```

---

### Task 4: Bounded Recovery in Existing Reconcile Loop

**Files:**
- Modify: `tests/hermes_cli/test_kanban_intake_db.py`
- Modify: `hermes_cli/kanban_db.py`

**Interfaces:**
- Consumes: `kanban_intake.submit_requalification(...)`.
- Extends: `ReconcileResult.requalification_requested: list[str]` containing target task IDs whose inert intake was created in this pass.

- [ ] **Step 1: Write failing reconcile tests**

```python
@pytest.fixture
def strict_v2_board_connection(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    board = "strict-v2-requalification"
    kb.ensure_product_board_defaults(board)
    metadata_path = kb.board_metadata_path(board)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("product_workflow", {})["handoff_v2"] = True
    metadata["qualification"]["required"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with kb.connect(board=board) as connection:
        yield connection, board


def test_reconcile_requests_one_scheduled_requalification_per_pass(
    strict_v2_board_connection,
):
    conn, board = strict_v2_board_connection
    first = _materialized_scheduled_card(conn, board)
    second = _materialized_scheduled_card(conn, board)
    result = kb.reconcile(conn, board=board, spawn_ready=False)
    assert result.requalification_requested == [first]
    pending = kb.list_qualification_intakes(conn, status="pending")
    assert len(pending) == 1
    assert intake.intake_payload(pending[0])["target_task_id"] == first


def test_reconcile_does_not_duplicate_pending_requalification(
    strict_v2_board_connection,
):
    conn, board = strict_v2_board_connection
    task_id = _materialized_scheduled_card(conn, board)
    first = kb.reconcile(conn, board=board, spawn_ready=False)
    second = kb.reconcile(conn, board=board, spawn_ready=False)
    assert first.requalification_requested == [task_id]
    assert second.requalification_requested == []
    assert len(kb.list_qualification_intakes(conn, status="pending")) == 1


def test_reconcile_leaves_non_scheduled_work_untouched(strict_v2_board_connection):
    conn, board = strict_v2_board_connection
    task_id = _materialized_scheduled_card(conn, board)
    assert kb.unblock_task(conn, task_id)
    result = kb.reconcile(conn, board=board, spawn_ready=False)
    assert result.requalification_requested == []
    assert kb.list_qualification_intakes(conn, status="pending") == []
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake_db.py -q`

Expected: `ReconcileResult` has no requalification field and no intake is created.

- [ ] **Step 3: Add one bounded step to `reconcile()`**

After recovery/spawn and before integration, query strict qualified v2 card rows:

```sql
SELECT id FROM tasks
 WHERE status = 'scheduled'
   AND work_item_kind = 'card'
   AND work_contract_id IS NOT NULL
   AND current_run_id IS NULL
   AND claim_lock IS NULL
   AND COALESCE(current_step_key, '') != 'release_measure'
 ORDER BY priority DESC, created_at ASC, id ASC
```

Walk candidates until `submit_requalification(conn, task_id=task_id, reason=reason)` creates one new intake, then stop. Idempotency remains owned by the intake function. Non-v2 and non-strict boards remain no-ops.

- [ ] **Step 4: Run Kanban and gateway tests and confirm GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake_db.py tests/hermes_cli/test_kanban_db.py tests/gateway/test_kanban_dispatch_reconcile.py tests/gateway/test_kanban_qualification_watcher.py -q`

Expected: all tests pass; the gateway wrapper needs no new code because it already invokes qualifier-before-dispatch and reconcile-after-dispatch.

- [ ] **Step 5: Commit bounded recovery**

```bash
git add hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_intake_db.py
git commit -m "feat(kanban): requalify stranded scheduled work"
```

---

### Task 5: Verification, Review, and Deployment

**Files:**
- Modify: `docs/plans/2026-07-18-normal-handover-requalification-design.md`

**Interfaces:**
- No new interfaces.

- [ ] **Step 1: Run formatting/static checks on touched Python**

Run: `python -m compileall -q hermes_cli/kanban_db.py hermes_cli/kanban_intake.py hermes_cli/kanban_qualifier.py`

Expected: exit 0.

- [ ] **Step 2: Run the focused qualification and gateway suite**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake.py tests/hermes_cli/test_kanban_intake_db.py tests/hermes_cli/test_kanban_qualifier.py tests/gateway/test_kanban_qualification_watcher.py tests/gateway/test_kanban_dispatch_reconcile.py tests/e2e/test_kanban_qualified_product_flow.py -q`

Expected: zero failures.

- [ ] **Step 3: Run the full repository suite**

Run: `scripts/run_tests.sh`

Expected: zero failures.

- [ ] **Step 4: Review the diff against the design and operating rules**

Run: `git diff origin/main...HEAD --check && git diff --stat origin/main...HEAD`

Review every changed line for: no direct product-board orchestration, no override widening, no unrelated edits, no new service/config/tool, bounded work, atomicity, and explicit evidence.

- [ ] **Step 5: Mark design implemented and commit documentation**

Change `**Status:** Proposed for implementation` to `**Status:** Implemented and verified`, then:

```bash
git add docs/plans/2026-07-18-normal-handover-requalification-design.md
git commit -m "docs(kanban): mark requalification flow implemented"
```

- [ ] **Step 6: Integrate and start the live flow**

Push the branch, open the fork PR, wait for required checks, merge, update the installed detached checkout to the exact merged `origin/main`, restart only the Hermes gateway service, and verify:

```text
installed SHA == fork origin/main SHA
gateway health == healthy
first strict-board reconcile creates <= 1 task_requalification intake
existing qualifier consumes the intake on the next bounded sweep
the same task becomes todo or ready; no duplicate card is created
Trading remains unloaded
```
