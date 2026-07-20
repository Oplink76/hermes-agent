# Legacy Board and Work Inbox Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide internal migration records from the normal Work Inbox and reconcile the approved 82-card legacy inventory through one exact, auditable, reversible operation.

**Architecture:** Keep the normal Inbox fix at its existing API seam. Add one focused `kanban legacy-reconcile` module and CLI that reads a checked-in exact manifest, validates every guarded field before writing, reuses Hermes' existing board locks and SQLite backup pattern, applies one transaction per board, and restores earlier boards if any later board fails. This is a one-time governed operation, not a cleanup framework.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, pytest, existing `hermes_cli.kanban_db` and `hermes_cli.kanban_qualification_migrate` safety helpers.

## Global Constraints

- Default Work Inbox listings exclude exactly `hermes-migration` and `hermes-reconcile`; an exact `source` query continues to expose either source for audit.
- Preserve all qualification intake rows, decisions, Work Contracts, task-to-contract pointers, events, comments, runs, attachments, Git refs, worktrees, and project/runtime state unless an exact manifest disposition below authorizes a card lifecycle change.
- The manifest contains all 82 live cards across `agentic-os-cockpit`, `the-trading-company`, `llm-memory-wiki-bridge`, `handoff-lab`, `ready-console`, and `useful-tool`.
- Card dispositions are exact: 47 `verify`, 10 `legacy_reconciled`, 12 `keep_open`, 10 `review`, and 3 `archive`.
- Qualification correction is exact: 52 cards receive one idempotent `qualification_history_corrected` event; no qualification row or Work Contract is deleted or rewritten.
- The ten `legacy_reconciled` cards are `t_3977b659`, `t_9fcf9ae8`, `t_abeba772`, `t_d5619b17`, `t_048e12a4`, `t_45e3d9d4`, `t_65c31cde`, `t_4c80fabe`, `t_5eaf5022`, and `t_6a3a456c`.
- The twelve `keep_open` cards are `t_24819d78`, `t_dbe5585c`, `t_4242c2e7`, `t_1a219a0b`, `t_1b11de39`, `t_d3f6806d`, `t_c3eae4c0`, `t_ff743bab`, `t_c91e1eaf`, `t_6077344f`, `t_c42205f4`, and `t_ef5c8b27`.
- The ten `review` cards are `t_fc6fdaa5`, `t_3d68114c`, `t_22391b05`, `t_64d84656`, `t_95340c94`, `t_3dad1da8`, `t_2d7e8829`, `t_9009edc0`, `t_f79667c5`, and `t_ff82bd1f`.
- The three `archive` cards are `handoff-lab/t_c264bc0a`, `handoff-lab/t_9e53eb70`, and `ready-console/t_f6d2420e`.
- Dry-run is the default. Apply requires `--apply` and a Break-glass approval record whose `manifest_sha256` exactly matches the input bytes.
- Validate the entire manifest and all six live boards before the first write. Fail closed on an unknown/missing/duplicate card, guarded-field mismatch, unauthorized disposition, active mutation target, snapshot failure, transaction failure, or integrity failure.
- Hold all six dispatch locks for the full snapshot/apply/verify-or-restore boundary. Stop the gateway before live apply and do not resume it until verification or restoration finishes.
- Use one transaction per board. If a later board fails, restore every previously changed board from its pre-apply SQLite snapshot while the locks remain held.
- A same-manifest re-run is idempotent: it does not duplicate correction or reconciliation events, and already-applied terminal/archive state verifies successfully.
- Do not add a generic cleanup service, dashboard, scheduler, schema migration, stale-run repair, dangling-link repair, or contract-detachment mechanism.
- One non-strict Default-board Framework Maintenance Task owns implementation, tests, review, merge, deployment, dry-run, exact approval evidence, apply, and the final receipt. No unrelated Default-board card changes.

---

### Task 1: Filter internal sources from the normal Work Inbox

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py:720-750`
- Modify: `tests/plugins/test_kanban_dashboard_plugin.py:490-535`

**Interfaces:**
- Consumes: `kanban_db.list_qualification_intakes(conn, status=...)` and the existing `source` query parameter.
- Produces: normal listing semantics that hide the two internal sources while preserving exact explicit-source audit access.

- [ ] **Step 1: Add a failing API test for default filtering and explicit audit access**

Extend `test_official_intake_api_returns_receipt_filtered_inbox_and_detail` by inserting two internal rows through `kanban_intake.submit_intake` (or the equivalent existing fixture helper), one with source `hermes-migration` and one with source `hermes-reconcile`. Assert:

```python
normal = client.get("/api/plugins/kanban/intake?board=strict")
assert [item["source"] for item in normal.json()["items"]] == ["dashboard-api"]

migration = client.get(
    "/api/plugins/kanban/intake?board=strict&source=hermes-migration"
)
assert migration.json()["count"] == 1
assert migration.json()["items"][0]["source"] == "hermes-migration"

reconcile = client.get(
    "/api/plugins/kanban/intake?board=strict&source=hermes-reconcile"
)
assert reconcile.json()["count"] == 1
assert reconcile.json()["items"][0]["source"] == "hermes-reconcile"
```

- [ ] **Step 2: Run the focused test and verify the new assertion fails**

Run: `scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py::test_official_intake_api_returns_receipt_filtered_inbox_and_detail -q`

Expected: FAIL because the unfiltered response includes both internal sources.

- [ ] **Step 3: Implement the two-source filter at the existing endpoint**

Add one module constant and change only the listing branch:

```python
_INTERNAL_QUALIFICATION_SOURCES = frozenset(
    {"hermes-migration", "hermes-reconcile"}
)

items = kanban_db.list_qualification_intakes(conn, status=status)
if source is None:
    items = [
        item for item in items
        if item["source"] not in _INTERNAL_QUALIFICATION_SOURCES
    ]
else:
    items = [item for item in items if item["source"] == source]
```

- [ ] **Step 4: Run the focused plugin tests**

Run: `scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the Inbox change**

```bash
git add plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_dashboard_plugin.py
git commit -m "fix(kanban): hide internal qualification intake"
```

### Task 2: Add exact-manifest validation and dry-run reporting

**Files:**
- Create: `hermes_cli/kanban_legacy_reconcile.py`
- Create: `tests/hermes_cli/test_kanban_legacy_reconcile.py`

**Interfaces:**
- Consumes: `kb.kanban_db_path(board)` and read-only SQLite connections.
- Produces:
  - `class ReconciliationBlocked(RuntimeError)`
  - `manifest_sha256(path: Path) -> str`
  - `audit_manifest(manifest_path: Path) -> dict[str, Any]`

- [ ] **Step 1: Write failing tests for the manifest contract**

Create all six approved board fixtures under a temporary `HERMES_HOME`, with
cards on two boards and the other four empty, then create an exact manifest.
Cover:

```python
def test_audit_is_read_only_and_reports_exact_counts(...):
    before = {board: _sha(kb.kanban_db_path(board)) for board in boards}
    report = reconcile.audit_manifest(manifest_path)
    assert report["mode"] == "dry-run"
    assert report["manifest_sha256"] == _sha(manifest_path)
    assert report["counts"] == {
        "cards": 5,
        "verify": 1,
        "legacy_reconciled": 1,
        "keep_open": 1,
        "review": 1,
        "archive": 1,
        "qualification_corrections": 2,
    }
    assert {board: _sha(kb.kanban_db_path(board)) for board in boards} == before

@pytest.mark.parametrize(
    "mutation, message",
    [
        ("status", "guard mismatch"),
        ("missing_card", "manifest inventory mismatch"),
        ("duplicate_card", "duplicate card"),
        ("unknown_disposition", "unknown card disposition"),
    ],
)
def test_audit_fails_closed_on_invalid_or_changed_inventory(...):
    with pytest.raises(reconcile.ReconciliationBlocked, match=message):
        reconcile.audit_manifest(manifest_path)
```

The fixture manifest uses this exact entry shape:

```json
{
  "version": 1,
  "scope": "legacy-board-inbox-reconciliation-2026-07-20",
  "boards": [
    "agentic-os-cockpit",
    "the-trading-company",
    "llm-memory-wiki-bridge",
    "handoff-lab",
    "ready-console",
    "useful-tool"
  ],
  "cards": [{
    "board": "agentic-os-cockpit",
    "task_id": "t_example",
    "expected": {
      "status": "ready",
      "current_step_key": "release_measure",
      "current_run_id": null,
      "running": 0,
      "blocked": 0,
      "work_contract_id": "wc_example"
    },
    "card_disposition": "legacy_reconciled",
    "qualification_disposition": "migration_artifact_not_qualification",
    "qualification_lineage": {
      "intake_ids": ["qi_example"],
      "contract_ids": ["wc_example"]
    },
    "evidence": ["test evidence"]
  }]
}
```

- [ ] **Step 2: Run the new tests and verify import failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_legacy_reconcile.py -q -k 'audit or invalid'`

Expected: FAIL because `kanban_legacy_reconcile` does not exist.

- [ ] **Step 3: Implement strict loading, inventory comparison, and read-only audit**

Implement only the exact schema above. Canonical behavior:

```python
CARD_DISPOSITIONS = frozenset(
    {"verify", "legacy_reconciled", "keep_open", "review", "archive"}
)
QUALIFICATION_DISPOSITIONS = frozenset(
    {"legitimate_or_none", "migration_artifact_not_qualification"}
)

def manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def audit_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest, digest = _load_and_validate_manifest(manifest_path)
    observed = _read_boards_query_only(manifest["boards"])
    _validate_exact_inventory(manifest["cards"], observed)
    _validate_guards_and_lineage(manifest["cards"], observed)
    return {
        "version": 1,
        "mode": "dry-run",
        "scope": manifest["scope"],
        "manifest_sha256": digest,
        "boards": manifest["boards"],
        "counts": _counts(manifest["cards"]),
        "ready_to_apply": True,
    }
```

Use `PRAGMA query_only=ON`; do not call `kb.init_db()` or any schema-migrating connection in dry-run. Require the production manifest's top-level `boards` list to equal the six approved slugs, including the empty `useful-tool` board. Compare the manifest set with every non-archived and archived task row on those boards, so extra, missing, or cross-board duplicate IDs fail closed. Validate that each listed intake and contract exists and that all 52 correction entries account for the internal lineage attached to that task.

- [ ] **Step 4: Run dry-run tests and lint the new module**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_legacy_reconcile.py -q -k 'audit or invalid'`

Run: `ruff check hermes_cli/kanban_legacy_reconcile.py tests/hermes_cli/test_kanban_legacy_reconcile.py`

Expected: PASS.

- [ ] **Step 5: Commit the validation seam**

```bash
git add hermes_cli/kanban_legacy_reconcile.py tests/hermes_cli/test_kanban_legacy_reconcile.py
git commit -m "feat(kanban): validate legacy reconciliation manifests"
```

### Task 3: Apply the three approved mutations with rollback and idempotency

**Files:**
- Modify: `hermes_cli/kanban_legacy_reconcile.py`
- Modify: `tests/hermes_cli/test_kanban_legacy_reconcile.py`

**Interfaces:**
- Consumes: Task 2's validated manifest and digest; `kb._dispatch_tick_lock(path)`, `migration._snapshot_board(board, recovery_root=..., audit=...)`, `kb.authorized_governance_write()`, `kb.write_txn(conn)`, `kb._append_event(...)`, and `kb.archive_task(...)`.
- Produces: `apply_manifest(manifest_path: Path, approval_path: Path, *, recovery_root: Path | None = None) -> dict[str, Any]` and an immutable `receipt.json` beneath `~/.hermes/recovery/legacy-reconciliation/` (or the injected test recovery root).

- [ ] **Step 1: Write failing tests for authority, mutation, rollback, and re-run**

Cover these exact outcomes:

```python
def test_apply_requires_approval_bound_to_manifest_hash(...):
    approval["manifest_sha256"] = "0" * 64
    with pytest.raises(reconcile.ReconciliationBlocked, match="approval hash"):
        reconcile.apply_manifest(manifest_path, approval_path)

def test_apply_writes_only_approved_state_and_events(...):
    result = reconcile.apply_manifest(
        manifest_path, approval_path, recovery_root=recovery_root
    )
    assert result["counts"]["legacy_reconciled"] == 1
    assert result["counts"]["archived"] == 1
    assert result["counts"]["qualification_history_corrected"] == 2
    assert result["manifest_sha256"] == _sha(manifest_path)
    assert Path(result["receipt_path"]).stat().st_mode & 0o222 == 0
    # verify/keep_open/review rows are byte-for-byte field-equivalent
    # contracts/intakes/decisions/comments/runs/attachments are unchanged
    # legacy target is Done/current_step_key=done with legacy_reconciled event
    # archive target is Archived through the existing archive behavior

def test_apply_is_idempotent_for_same_manifest(...):
    first = reconcile.apply_manifest(manifest_path, approval_path, ...)
    second = reconcile.apply_manifest(manifest_path, approval_path, ...)
    assert second["already_applied"] is True
    assert _event_count("legacy_reconciled") == 1
    assert _event_count("qualification_history_corrected") == 2

def test_active_mutation_target_blocks_before_snapshots_or_writes(...):
    with pytest.raises(reconcile.ReconciliationBlocked, match="active run"):
        reconcile.apply_manifest(manifest_path, approval_path, ...)
    assert not recovery_root.exists()

def test_later_board_failure_restores_earlier_board(..., monkeypatch):
    monkeypatch.setattr(reconcile, "_apply_board", fail_on_second_board)
    with pytest.raises(reconcile.ReconciliationBlocked, match="restored"):
        reconcile.apply_manifest(manifest_path, approval_path, ...)
    assert _logical_export(alpha_db) == alpha_before
    assert _logical_export(beta_db) == beta_before
    assert _integrity(alpha_db) == _integrity(beta_db) == "ok"
```

- [ ] **Step 2: Run apply tests and verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_legacy_reconcile.py -q -k 'apply or active or restores'`

Expected: FAIL because `apply_manifest` has no mutation implementation.

- [ ] **Step 3: Implement the minimal apply boundary**

Implement the approval contract:

```json
{
  "version": 1,
  "authority": "break-glass",
  "scope": "legacy-board-inbox-reconciliation-2026-07-20",
  "approved_by": "Ole Ørum-Petersen",
  "approved_at": "2026-07-20T00:00:00+02:00",
  "manifest_sha256": "SHA-256 copied from the successful dry-run",
  "evidence": "Default-board maintenance card ID plus the approved Codex task"
}
```

Acquire every board lock in sorted order with `contextlib.ExitStack`. Re-run the full read-only audit while locks are held, reject mutation targets with `running=1` or an active pointed run, snapshot every board with the existing consistent SQLite backup helper, then apply one transaction per board.

Inside each board transaction:

```python
if entry["qualification_disposition"] == "migration_artifact_not_qualification":
    _append_once(
        conn,
        entry["task_id"],
        "qualification_history_corrected",
        {
            "manifest_sha256": digest,
            "actor": "hermes/default",
            "reason": "Legacy migration/reconciliation was not Qualification or Requalification",
            **entry["qualification_lineage"],
        },
    )

if entry["card_disposition"] == "legacy_reconciled":
    _append_once(
        conn,
        entry["task_id"],
        "legacy_reconciled",
        {
            "manifest_sha256": digest,
            "actor": "hermes/default",
            "reason": "Approved evidence proves this externally orchestrated legacy work is complete",
            "evidence": entry["evidence"],
        },
    )
    conn.execute(
        "UPDATE tasks SET status='done', current_step_key='done', "
        "completed_at=COALESCE(completed_at, ?), running=0, blocked=0, "
        "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
        "block_kind=NULL, block_recurrences=0 WHERE id=?",
        (now, entry["task_id"]),
    )
elif entry["card_disposition"] == "archive":
    kb.archive_task(conn, entry["task_id"])
```

Wrap governed fields in `kb.authorized_governance_write()`. Do not call `kb.complete_task`; it would fabricate a modern completion event and invoke modern workflow gates. `verify`, `keep_open`, and `review` never change lifecycle fields.

After every board, verify expected states inside a read-only connection. On any failure, restore each snapshot database with SQLite's backup API while all locks remain held, verify `PRAGMA integrity_check`, and emit a separate failure/restore receipt. On success, write a receipt containing approval, manifest hash, snapshot locations, before/after counts, event counts, integrity results, and `status: applied`; chmod the complete receipt directory read-only. Before requiring pre-state guards, detect a prior successful exact-hash apply receipt and return `already_applied: true` only after verifying all 13 lifecycle targets are in their canonical post-state and all expected exact-hash events exist. A partially applied state without a successful receipt fails closed.

- [ ] **Step 4: Run the complete reconciliation test file**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_legacy_reconcile.py -q`

Run: `ruff check hermes_cli/kanban_legacy_reconcile.py tests/hermes_cli/test_kanban_legacy_reconcile.py`

Expected: PASS.

- [ ] **Step 5: Commit governed apply behavior**

```bash
git add hermes_cli/kanban_legacy_reconcile.py tests/hermes_cli/test_kanban_legacy_reconcile.py
git commit -m "feat(kanban): apply governed legacy reconciliation"
```

### Task 4: Wire the CLI and freeze the exact live manifest

**Files:**
- Modify: `hermes_cli/kanban.py:231-255`
- Modify: `hermes_cli/kanban.py:955-990`
- Modify: `hermes_cli/kanban.py:1328-1365`
- Modify: `tests/hermes_cli/test_kanban_legacy_reconcile.py`
- Create: `docs/reconciliation/manifests/2026-07-20-legacy-board-inbox-reconciliation.json`

**Interfaces:**
- Consumes: `reconcile.audit_manifest(Path(args.manifest))` and `reconcile.apply_manifest(Path(args.manifest), Path(args.approval))`.
- Produces:
  - `hermes kanban legacy-reconcile --manifest MANIFEST_PATH [--apply --approval APPROVAL_PATH] [--json]`
  - one Git-reviewed exact manifest whose SHA-256 is used by dry-run, approval, apply, and receipt.

- [ ] **Step 1: Write failing CLI tests**

Add assertions:

```python
dry = json.loads(kc.run_slash(
    f"legacy-reconcile --manifest {manifest_path} --json"
))
assert dry["mode"] == "dry-run"

applied = json.loads(kc.run_slash(
    f"legacy-reconcile --manifest {manifest_path} "
    f"--apply --approval {approval_path} --json"
))
assert applied["status"] == "applied"
```

Also assert `--apply` without `--approval` is rejected by argparse and that this command, like `qualification-migrate`, bypasses automatic `kb.init_db()` so its dry-run remains read-only.

- [ ] **Step 2: Run CLI tests and verify parser failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_legacy_reconcile.py -q -k cli`

Expected: FAIL because `legacy-reconcile` is not registered.

- [ ] **Step 3: Add the narrow CLI surface**

Register only `--manifest`, `--apply`, `--approval`, and `--json`. Reject `--approval` without `--apply`; require approval with apply. Add `legacy-reconcile` beside `qualification-migrate` in the auto-initialization exception. Print dry-run counts and manifest hash in text mode, and receipt path in apply mode.

- [ ] **Step 4: Freeze the exact 82-card manifest with no generator code**

Read every live board through query-only SQLite and transcribe the guarded fields into the manifest using `apply_patch`. Use the exact disposition IDs in Global Constraints; all remaining cards are `verify`. For each of the 52 cards named in `2026-07-20-qualification-inbox-cleanup-scope.md`, query and record its exact internal intake and contract lineage. Evidence strings for lifecycle decisions come from `2026-07-19-all-board-card-reconciliation-analysis.md`.

Validate mechanically:

```bash
hermes kanban legacy-reconcile \
  --manifest docs/reconciliation/manifests/2026-07-20-legacy-board-inbox-reconciliation.json \
  --json
```

Expected JSON: `ready_to_apply: true`, six boards, 82 cards, dispositions `47/10/12/10/3`, and 52 qualification corrections. The command must leave all six database SHA-256 hashes unchanged.

- [ ] **Step 5: Run focused and adjacent tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_legacy_reconcile.py tests/hermes_cli/test_kanban_qualification_migrate.py tests/plugins/test_kanban_dashboard_plugin.py -q`

Run: `ruff check hermes_cli/kanban.py hermes_cli/kanban_legacy_reconcile.py plugins/kanban/dashboard/plugin_api.py tests/hermes_cli/test_kanban_legacy_reconcile.py tests/plugins/test_kanban_dashboard_plugin.py`

Expected: PASS.

- [ ] **Step 6: Commit the CLI and manifest**

```bash
git add hermes_cli/kanban.py tests/hermes_cli/test_kanban_legacy_reconcile.py docs/reconciliation/manifests/2026-07-20-legacy-board-inbox-reconciliation.json
git commit -m "feat(kanban): expose exact legacy reconciliation"
```

### Task 5: Review, merge, deploy, apply, and close the maintenance task

**Files:**
- Create at runtime: `~/.hermes/recovery/legacy-reconciliation/RUN_ID/approval.json`
- Create at runtime: `~/.hermes/recovery/legacy-reconciliation/RUN_ID/receipt.json`
- Update through CLI: the single Default-board Framework Maintenance Task only.

**Interfaces:**
- Consumes: the merged CLI, exact manifest hash, the user's approval in this Codex task, and the Default-board maintenance card ID.
- Produces: merged/deployed code, immutable approval/apply evidence, verified live state, and one completed Default-board maintenance card.

- [ ] **Step 1: Run independent task reviews and a whole-branch review**

Use `superpowers:subagent-driven-development` task review after each implementation task. Generate the final review package from `git merge-base origin/main HEAD` through current `HEAD`; require both spec compliance and code-quality approval with no open Critical or Important findings.

- [ ] **Step 2: Run final local verification**

Run the focused/adjacent tests through `scripts/run_tests.sh` and the ruff commands from Task 4, then the repository's standard non-integration test command used by CI. Record exact commands and counts on the Default-board card.

- [ ] **Step 3: Push, open a PR, pass CI, and merge to `main`**

Push `fix/legacy-board-inbox-reconciliation`, create a PR with the approved scope and verification, wait for required GitHub checks, merge, and pull the merged `main` into the installed Hermes checkout. Record the merge SHA.

- [ ] **Step 4: Deploy the normal Inbox filter and CLI**

Restart only the Hermes gateway/dashboard processes required for installed Python code to reload. Verify the running installation resolves to the merge SHA and the Inbox endpoint uses the new default filter.

- [ ] **Step 5: Run the exact live dry-run and bind approval**

Capture pre-apply Git/worktree/runtime state and logical exports of all immutable qualification evidence. Hash the checked-in manifest, run `legacy-reconcile` without `--apply`, and compare the output to `82 cards / 47 verify / 10 legacy_reconciled / 12 keep_open / 10 review / 3 archive / 52 corrections`. If any guard or count differs, stop without mutation and report the mismatch.

Create `approval.json` with `apply_patch`, binding `authority=break-glass`, the exact manifest hash, `approved_by=Ole Ørum-Petersen`, the Default-card ID, and this already-approved Codex execution instruction. Chmod it read-only.

- [ ] **Step 6: Stop Hermes, apply once, and restart only after verification**

Stop the gateway so no new dispatcher/dashboard writes race the multi-board correction. Run:

```bash
hermes kanban legacy-reconcile \
  --manifest "$MANIFEST_PATH" \
  --apply \
  --approval "$APPROVAL_PATH" \
  --json
```

If apply fails, confirm the receipt says all changed boards were restored and all six integrity checks are `ok` before restarting Hermes. If it succeeds, verify all six integrity checks before restarting Hermes.

- [ ] **Step 7: Verify the approved live outcomes**

Verify normal Inbox count is 3; explicit source queries return 52 `hermes-migration` plus 27 `hermes-reconcile`; exactly 10 target cards are now Done with one `legacy_reconciled` event; exactly 3 targets are Archived; all 52 affected cards have one correction event; keep-open/review/verify lifecycle fields are unchanged; Work Contract pointers and evidence tables are unchanged; all six databases pass `PRAGMA integrity_check`; product repositories, worktrees, and Trading runtime hashes/state are unchanged.

- [ ] **Step 8: Attach evidence and complete only the Default maintenance card**

Add a Default-board comment/result containing the manifest hash, merge SHA, deployed SHA, dry-run counts, receipt path, post-apply counts, integrity results, and explicit non-impact checks. Complete the maintenance card through the normal non-strict Default-board path. Do not mutate the existing unrelated Default-board card.
