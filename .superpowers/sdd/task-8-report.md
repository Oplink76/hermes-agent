# Task 8 release-evidence report

Status: DONE

## Isolation and safety

- Worktree: `/Users/cloudadvisor/.hermes/worktrees/hermes-kanban-task8-20260711`
- Branch: `recovery/hermes-kanban-task8-20260711`
- Base: `2b86d6a6f02dfc0588290f46d49ed0212840c2b2`
- No live configuration, service, process, deployment, remote, or user checkout was
  read or mutated. Git integration tests use temporary repositories only.
- No subagents were used, per the explicit task constraint. Tester and reviewer
  gates below were performed as separate local passes.

## TDD evidence

The release tests were written before production changes. After correcting one
test-fixture-only `parents=None` mistake, the focused red run produced 12 expected
failures: `ReleaseEvidenceError` and `release_product_task` did not exist, and no
production implementation had been changed.

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_release_evidence.py \
  tests/hermes_cli/test_kanban_db.py -q \
  -k 'release_evidence or release_measure_cannot_bypass'

0 passed, 12 failed
```

The first focused green run passed 12/12. The final focused suite contains 14
release-evidence tests, including successful concrete deployment evidence and
required pull-request references.

## Implementation

- Added `ReleaseEvidenceError` and `ReleaseResult`.
- Added `release_product_task`, which validates structured test/review runs before
  integration, integrates before Done, evaluates deployment policy, and enters the
  terminal transaction only with complete evidence.
- Default product-board deployment policy is persisted as `manual`.
- Standalone stories record `story_merged_to_main` before Done, including the
  idempotent already-merged path.
- Epic children record story-to-epic integration before child Done. Epic releases
  require every child to be Done with a matching, non-empty integration event/SHA
  before epic-to-main integration.
- Structured tester evidence requires `result=passed`; structured review evidence
  requires `verdict=approved`, a reviewer different from the reviewed writer, and
  the reviewed branch/commit matching the release source.
- `manual` and `not_required` policies record an explicit non-deployment
  evaluation. A required policy without an adapter records
  `release_adapter_missing` and stays in `release_measure`.
- Concrete deployment records require environment, exact integrated revision,
  passing smoke result, rollback target, and runtime evidence.
- `_validate_done_evidence` runs after `BEGIN IMMEDIATE` and before the terminal
  state update. A regression test asserts `conn.in_transaction` at this call.
  The same transaction writes the `completed` event with run, integration,
  deployment-policy, optional deployment/PR, and measurement references.
- `kanban_complete` routes `release_measure` cards through the release orchestrator;
  it never selects `_DefaultOpsClient` as a release adapter.
- Historical Done/unmerged reconciliation behavior remains available.

## Structured tester evidence

All prescribed Task 8 commands were run after the final behavior changes:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_recovery_regressions.py -q
5 passed, 0 failed

scripts/run_tests.sh tests/hermes_cli/test_kanban_release_evidence.py -q
14 passed, 0 failed

scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -q
473 passed, 0 failed

scripts/run_tests.sh tests/hermes_cli/test_kanban_cli.py \
  tests/tools/test_kanban_tools.py \
  tests/plugins/test_kanban_dashboard_plugin.py \
  tests/plugins/test_kanban_governance.py -q
297 passed, 0 failed

scripts/run_tests.sh tests/gateway/test_kanban_dispatch_reconcile.py \
  tests/gateway/test_kanban_auto_decompose_live.py \
  tests/gateway/test_default_resolver_health.py -q
16 passed, 0 failed
```

## Structured reviewer evidence

Spec review:

- PASS: tester pass and independent approved reviewer are structured run fields,
  not summaries.
- PASS: reviewed branch/SHA match is checked before integration and rechecked in
  the terminal transaction.
- PASS: standalone, child, and epic integration ordering is event-backed and
  covered by tests that assert the task is not Done at integration time.
- PASS: deployment policy evaluation is explicit; missing/incomplete required
  adapters leave the card in `release_measure` and cannot fake deployment/Done.
- PASS: the terminal event references every required evidence record, plus PR when
  configured and the measurement note.

Standards review:

- PASS: changes are limited to the four Task 8 files plus this report.
- PASS: no remote push, live deployment, service, process, or live-config path was
  introduced or invoked.
- PASS: `git diff --check` is clean and no conflict markers are present.
- PASS: the implementation reuses existing merge-candidate and transaction helpers;
  reconcile remains the historical repair path.

No P0/P1 findings remain. No deferred concern was identified. The intentional
operational limitation is that the tool surface supplies no production deployment
adapter: boards configured with `deployment_policy=required` therefore stop
honestly at `release_adapter_missing` until a concrete adapter is registered.
