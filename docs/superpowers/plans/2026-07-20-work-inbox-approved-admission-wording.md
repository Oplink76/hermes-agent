# Work Inbox Approved Admission Wording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the public Work Inbox guide state that its caller is a local AI outside Hermes, that Ole already approved the work to enter the framework, and that qualification plus a signed Work Contract are still required before execution.

**Architecture:** Preserve the deployed Work Inbox interface and all framework behavior. Change only the copy returned by `build_inbox_guide()` and prove the clarified authority model through the existing guide contract test.

**Tech Stack:** Python 3, FastAPI guide payload, pytest, existing Hermes test runner.

## Global Constraints

- Keep guide version `2` and both existing request shapes unchanged.
- Keep `POST /api/plugins/kanban/work-inbox?board=<board-slug>` unchanged.
- Keep `202 qualification_required` and the existing `qi_...` receipt unchanged.
- Do not change authentication, qualification, requalification, Work Contracts, task creation, assigned delivery, or Normal Handover.
- Add no database field, lifecycle, receipt engine, queue, retry, daemon, or watcher.
- The machine credential remains absent from the public guide.

---

### Task 1: Clarify the public Work Inbox authority wording

**Files:**
- Modify: `tests/hermes_cli/test_kanban_inbox_guide.py:13-43`
- Modify: `hermes_cli/kanban_inbox_guide.py:22-49`

**Interfaces:**
- Consumes: `build_inbox_guide(*, board: str, origin: str) -> dict[str, Any]`
- Produces: the same guide-version-2 dictionary and endpoint contract, with clarified `purpose`, first `authority.allowed` item, and `copy_ready_prompt` text

- [ ] **Step 1: Add failing authority-language assertions**

In `test_guide_names_one_minimal_work_inbox_route()`, add these assertions after `assert body["qualification_required"] is True`:

```python
    assert body["purpose"] == (
        "Submit Ole-approved local work for framework qualification or hand "
        "over an exact assigned delivery."
    )
    assert body["authority"]["allowed"][0] == (
        "submit Ole-approved work for framework processing"
    )
    prompt = body["copy_ready_prompt"]
    assert "local AI outside the Hermes-managed framework" in prompt
    assert "Ole has approved it to enter Hermes" in prompt
    assert "Admission does not authorize execution" in prompt
    assert "qualification and a signed Work Contract remain required" in prompt
```

- [ ] **Step 2: Run the focused test and verify the semantic mismatch is exposed**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_kanban_inbox_guide.py -q
```

Expected: FAIL in `test_guide_names_one_minimal_work_inbox_route` because the current `purpose` says only “Submit new work for qualification...” and the current prompt does not state local caller, Admission Approval, or Work Contract authority.

- [ ] **Step 3: Implement the minimum guide-copy correction**

In `build_inbox_guide()`, replace `copy_ready_prompt` with:

```python
    copy_ready_prompt = (
        "You are a local AI outside the Hermes-managed framework. "
        f"Read {guide_url} before doing any work. Treat it as the authority "
        "for how to deliver development work to Hermes. Submit new work only "
        "after Ole has approved it to enter Hermes. Admission does not "
        "authorize execution; Hermes qualification and a signed Work Contract "
        "remain required. Use the fixed Work Inbox submission route exactly as "
        "documented. Do not create, edit, assign, route, qualify, or override "
        "Kanban cards directly."
    )
```

Replace the returned `purpose` value with:

```python
        "purpose": (
            "Submit Ole-approved local work for framework qualification or hand "
            "over an exact assigned delivery."
        ),
```

Replace the first `authority.allowed` item while leaving the second item and the complete forbidden list unchanged:

```python
            "allowed": [
                "submit Ole-approved work for framework processing",
                "hand over assigned delivery",
            ],
```

- [ ] **Step 4: Run the guide contract test and verify it passes**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_kanban_inbox_guide.py -q
```

Expected: all tests in the file PASS.

- [ ] **Step 5: Run the focused compatibility suite**

Run:

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_inbox_guide.py \
  tests/hermes_cli/test_kanban_qualifier.py \
  tests/hermes_cli/test_kanban_intake_db.py \
  tests/hermes_cli/test_work_inbox_auth_security.py \
  tests/plugins/dashboard_auth/test_work_inbox_provider.py \
  -q
```

Expected: all tests PASS with zero failures. Then run:

```bash
git diff --check
git diff --name-only HEAD
```

Expected: `git diff --check` exits `0`; the implementation diff names only `hermes_cli/kanban_inbox_guide.py` and `tests/hermes_cli/test_kanban_inbox_guide.py`.

- [ ] **Step 6: Commit the implementation**

```bash
git add hermes_cli/kanban_inbox_guide.py tests/hermes_cli/test_kanban_inbox_guide.py
git commit -m "docs(kanban): clarify Work Inbox admission semantics"
```

Expected: one implementation commit containing only the guide-copy correction and its test.
