# Hermes Kanban v2 repository boundary

This guide is for a product board that uses the v2 repository contract. The
contract makes Git state and verification commands explicit, keeps story
refresh owned by the dispatcher, and makes Test/Review evidence immutable.
Use a temporary board and temporary repositories for validation. Do not point
these procedures at a live Epic or the stable runner.

## Repository contract

A product board's metadata must contain a `repository` object with these fields:

```yaml
repository:
  # A complete configured ref. Remote-tracking refs are read-only inputs.
  base_ref: refs/remotes/origin/main
  target_branch: main

  verification_profiles:
    story_integration:
      commands:
        - argv: [bash, scripts/run_tests.sh]
          workdir: .
          timeout_seconds: 1800
    epic_release:
      commands:
        - argv: [bash, scripts/run_tests.sh]
          workdir: .
          timeout_seconds: 1800

  ci_observation:
    provider: github_actions
    required_workflows: [CI, Deploy Test]

  boundary_evidence:
    test_globs: [tests/**]
    fixture_globs: [tests/fixtures/**]
    generated_paths: [dashboard/index.html, dashboard/data.json]
```

`base_ref`, `target_branch`, profile names, command argument arrays, workdirs,
timeouts, workflow names, and evidence paths are validated before use. Paths
are repository-relative; `workdir` and `generated_paths` cannot escape the
repository. Every generated path must already be tracked. The normalized
contract is hashed and the digest is carried with verification evidence.

Use the object form shown above for new metadata. The service also accepts the
older compact profile list form, but it normalizes both forms to the same
canonical contract. Do not add shell strings, arbitrary environment maps, or
unknown keys. Verification commands are executed as argument arrays without a
shell, from the validated workdir, with a deliberately small environment. The
captured stdout/stderr is bounded and secret-like values are redacted.

## Authority sequence

The exact SHA chain is the acceptance record:

1. Resolve the configured `base_ref` to a full 40-character SHA. The checked
   out branch and ambient `HEAD` are not substitutes for this ref.
2. Before Architecture/Development dispatch, pin the story branch and the
   current Epic tip. A clean story is merged with the pinned Epic tip in a
   detached disposable worktree.
3. Advance the story branch with a compare-and-swap against the pinned story
   SHA. The original story worktree is updated only after the ref checks and a
   second clean-status check pass.
4. Development produces the source commit. Its handoff SHA is the candidate
   that Test must receive.
5. Test launch records `test_branch` and `test_head_sha` in the dispatcher-owned
   run metadata. Review launch records `review_branch`, `review_base_sha`, and
   `review_head_sha`, and Review must see the same tested branch/head.
6. A positive Test or Review handoff carries the dispatcher-pinned evidence SHA;
   it does not create a new source commit.

A source move, branch movement, missing pin, or contract change invalidates the
candidate. Re-run the appropriate Development/refresh path and create fresh
Test/Review evidence rather than copying an old SHA into new metadata.

## Story refresh outcomes and retained diagnostics

Refresh is a pre-dispatch operation. It never resets, cleans, stashes, pushes,
or overwrites a dirty checkout.

| Outcome | Meaning | Operator action |
| --- | --- | --- |
| `unchanged` | The Epic tip is already an ancestor of the pinned story SHA. | Continue with the existing authority. |
| `refreshed` | The isolated merge succeeded and the story branch CAS advanced. | Treat previous candidate evidence as invalid and dispatch fresh work. |
| `dirty` | The story worktree contains tracked or untracked operator changes. | Preserve the listed paths, make the worktree clean through the normal Development process, then retry. |
| `conflict` | The isolated merge has conflicts. | Inspect the retained detached `conflict_worktree` named in the event/findings and resolve through Development. The original story worktree and branch remain unchanged. |
| `source_moved` | The story or Epic ref changed after pinning, including a late CAS race. | Do not overwrite either source; refresh from newly pinned refs. |
| `error` | Git/repository preparation failed. | Read the typed `error` detail and repair the repository/worktree configuration; no story authority was created. |

A conflict worktree is diagnostic evidence, not a new candidate branch. Keep its
path in the handoff/event record while the Development worker resolves the
conflict. Dirty paths and non-ignored untracked output are reported rather than
silently deleted. Gitignored evidence output is not treated as a source change.

## Configured verification outcomes

The repository service returns typed results for every configured command:

- `passed`: all commands in the selected profile completed with exit code zero.
- `failed`: a configured command ran and returned nonzero; later commands are
  not run. This is a candidate failure, not an infrastructure failure.
- `configuration_error`: the profile, executable, workdir, or argument policy
  is invalid. Repair board/repository metadata before retrying.
- `infrastructure_error`: the process timed out or could not be started. Repair
  the runner/environment and rerun; do not promote the candidate from a partial
  result.

Each result records the source SHA, candidate SHA, contract digest, profile,
per-step argv/workdir/status/return code/duration, and bounded output tails.
Record those fields in the task handoff or release evidence. Do not replace a
configured profile with an ambient `scripts/run_tests.sh` fallback on a
contract-governed board.

## Test and Review evidence boundary

At Test/Review launch the dispatcher requires a clean expected workspace and
pins the exact branch/head before the worker runs. At completion it performs a
fresh Git inspection:

- tracked edits outside `boundary_evidence.generated_paths` reject the handoff
  as `source_moved`;
- non-ignored untracked output rejects the handoff as `untracked_output` and is
  left in place for diagnosis;
- a changed branch or `HEAD` rejects the handoff as `source_moved`;
- declared tracked generated paths are recorded as
  `evidence_generated_mutations` and restored from the pinned SHA using explicit
  pathspecs;
- Gitignored output is allowed, but it is not source evidence.

The generated-path list is an allowlist, not a cleanup directory. It cannot
contain absolute paths, `..`, duplicate paths, or untracked files. The restore
operation does not use `reset`, `clean`, or `stash` and cannot act on arbitrary
paths.

Test and Review workers never author source or fixture commits. If source or
fixture edits are needed, report concrete findings and route the card back to
Development. Only Development owns the source commit boundary. Test/Review
may produce diagnostic output, but the output must either be ignored or be
preserved and declared according to the evidence policy.

## No-remote-write guarantee

Repository-boundary code is local-only. It may resolve refs, create/remove
local disposable worktrees, merge pinned commits in isolation, update the
story branch with a local CAS, restore explicitly allowlisted generated files,
and run configured commands. It must not:

- run `git push` or `git push --force`;
- update a remote-tracking ref (`refs/remotes/...`);
- change a hosted remote, CI ref, or deployment target;
- turn a verification or release measurement into an implicit publication.

A release harness or human may perform an explicitly separate external
operation after the local evidence is accepted. That operation is outside the
repository service and must be recorded as a distinct observation.

## Scratch proof and operator checklist

Run the proof against a temporary bare remote, local clone, board database, and
worktrees. Capture the exact command, result count, duration, authority SHAs,
retained diagnostic paths, and clean/dirty status. The repository proof should
show that a configured non-checked-out ``base_ref`` resolves correctly, a newer
Epic tip refreshes the story, the configured command passes, generated output
is restored, and the remote refs are byte-for-byte unchanged.

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_repository.py \
  tests/e2e/test_kanban_product_recovery_flow.py \
  tests/e2e/test_kanban_epic_integration_release.py -q
```

The E2E epic-integration file (``test_kanban_epic_integration_release.py``)
provides the structural no-remote-write proof: a push-refusing fake ``git``
executable on PATH logs every engine invocation and exercises the full public
path — dispatcher (``reconcile``), integrator (story→Epic-merge lifecycle),
snapshot (prepare + invalidation), dashboard API (release-state), CLI
(``release-state`` and ``v2-migrate``), CI observer (``observe_epic_release_ci``),
and migration (audit/apply/grandfathering) — asserting zero ``push``
invocations and a byte-identical bare remote after every operation. Only the
harness-side ``FakeGit.real()`` runner may write to the remote.

The recovery-flow file (``test_kanban_product_recovery_flow.py``) carries an
additional cross-surface no-push test exercising the same public paths through
the fake ``git`` and asserting the remote is untouched.

```bash
rg -n "git push|push --force|update-ref.*refs/remotes" \
  hermes_cli/kanban_repository.py \
  hermes_cli/kanban_db.py \
  tests/hermes_cli/test_kanban_repository.py \
  tests/e2e/test_kanban_product_recovery_flow.py \
  tests/e2e/test_kanban_epic_integration_release.py
```

The scan may match explicit refusal text or fixture assertions, but it must not
find a production remote-write implementation. The test suite is the authority
for the real-Git proof; source inspection alone is not proof of the refresh,
pinning, restore, diagnostic-retention, or no-remote-write behavior.

Before handoff, verify:

- the temporary repository and board were used;
- the contract digest and all full SHAs are recorded;
- configured verification returned `passed` (or the typed failure is recorded);
- dirty/conflict diagnostics were preserved and not mistaken for candidates;
- Test/Review evidence points to the same immutable Development SHA;
- generated mutations were recorded and restored;
- no remote ref changed;
- the target worktree contains only the intended test/documentation changes.


## V2 product migration (``hermes kanban v2-migrate``)

The ``v2-migrate`` subcommand provides a guarded, manifest-hashed migration
from a generic kanban board to a product v2 board. It operates exclusively on
**explicit scratch copies** of the kanban database — it refuses any path that
resolves to a live board. This ensures the migration can be proven safe before
the operator copies the migrated DB into place.

### Safety properties

- **Scratch-only.** The migration reads and writes only explicit absolute file
  paths. Board-slug resolution, the ``default`` board symlink, and live DB
  paths are rejected with a ``MigrationBlocked`` error.
- **Manifest-hashed.** Every dry-run and apply produces a SHA-256 content
  manifest. The manifest is stable for the same input DB — re-running produces
  the identical digest.
- **All-or-nothing.** Apply runs in one SQLite transaction. If any step fails,
  the entire migration is rolled back and the scratch DB is left unchanged.
- **Idempotent.** Running apply on an already-migrated scratch DB changes zero
  rows and produces an identical receipt.
- **Snapshot-first.** Apply creates an immutable pre-migration snapshot before
  modifying anything. The snapshot is integrity-checked at creation time.
- **Zero active runs.** Apply refuses to proceed if the scratch DB contains
  any task with ``running=1``.
- **Preserves history.** All task comments, events, links, and membership rows
  survive the migration intact. Only ``workflow_template_id`` and
  ``current_step_key`` columns are backfilled.
- **Approvals never grandfather.** The migration only sets workflow metadata
  from durable task evidence (assignee, status). No approval, review outcome,
  or release measurement is carried forward.
- **Facts grandfather from exact evidence.** The inferred v2 step comes from
  the task's current ``assignee`` (mapped through ``PRODUCT_WORKFLOW_ROLE_TO_STEP``),
  ``status``, and ``current_step_key`` — never from sibling boards or
  external sources.

### Usage

```bash
# 1. Create a scratch copy of the board database
cp ~/.hermes/kanban.db /tmp/scratch-kanban.db

# 2. Audit (dry-run) — read-only, no changes
hermes kanban v2-migrate /tmp/scratch-kanban.db
# Output: V2 dry-run: 8 task(s), 1 already product, 7 need migration. Ready to apply.

# 3. Apply the migration
hermes kanban v2-migrate /tmp/scratch-kanban.db --apply
# Output: V2 migration applied: 7 task(s) backfilled to product workflow. Receipt: ...

# 4. Verify the migrated DB
hermes kanban v2-migrate /tmp/scratch-kanban.db
# Output: V2 dry-run: 8 task(s), 8 already product, 0 need migration. Ready to apply.

# 5. After verification, replace the live DB (while the dispatcher is stopped)
mv /tmp/scratch-kanban.db ~/.hermes/kanban.db

# Machine-readable output is available with --json
hermes kanban v2-migrate /tmp/scratch-kanban.db --json
```

### What the migration does

For each non-archived task that does not already have ``workflow_template_id =
'product'`` with a valid step, the migration:

1. Infers the product workflow step from the task's assignee, status, and
   current_step_key using the same role-to-step mapping as the rest of the
   kanban system.
2. Sets ``workflow_template_id = 'product'`` and ``current_step_key = <inferred>``.
3. Records a ``v2_migrated`` event on the task with the manifest digest.
4. Does **not** change task status, assignee, work_contract_id, or any other
   column beyond the two workflow metadata fields.

Epics (``work_item_kind = 'epic'``) are recognized and skipped — they do not
receive a workflow step.

### Recovery

Each apply creates an immutable snapshot directory under the recovery root
(default: ``<db-parent>/v2-migration-snapshots/``). The snapshot contains a
byte-for-byte copy of the pre-migration database and an ``inventory.json``
manifest with integrity checks. To restore, copy the snapshot DB back over the
migrated file.

### Non-goals

- Does not run against live boards — use a scratch copy.
- Does not change task status, assignee, or any operational state.
- Does not create, destroy, or modify work contracts.
- Does not grandfather approvals from other boards.
- Does not create a production coordinator or generalize to non-kanban DBs.
- Does not push, merge, deploy, or touch remotes.
