# Legacy Board and Work Inbox Reconciliation Design

**Status:** Design approved; written specification awaiting review
**Date:** 2026-07-20
**Scope:** All six live Hermes boards

## Intent

Reconcile board state created before Hermes became the authoritative
orchestrator, while removing internal migration and reconciliation records from
the normal Work Inbox.

Historical work must not be forced through today's Qualification, Test, Review,
or Release stages retroactively. When evidence proves that externally
orchestrated legacy work is finished, Hermes records an honest
`legacy_reconciled` outcome and marks the card Done. Original history remains
available.

This is one bounded correction, not a new cleanup subsystem.

## Current Scope

The reviewed all-board inventory contains 82 cards:

| Disposition | Cards | Action |
|---|---:|---|
| Correctly closed | 47 | Verify only |
| Verified legacy completion | 10 | Record `legacy_reconciled`; mark Done |
| Genuinely unfinished | 12 | Keep open |
| Requires individual judgment | 10 | Report; no lifecycle mutation |
| Lab or orphan | 3 | Preserve, then archive |

Qualification contamination spans four boards:

- 79 internal `hermes-migration` or `hermes-reconcile` intake rows;
- 52 affected legacy cards;
- 79 decisions, 62 Work Contracts, and 89 qualification-related events;
- 52 current task-to-contract pointers.

Three legitimate intake records must remain in normal board history: the
Cockpit `dashboard-api` intake and the two Handoff Lab canary submissions.
There are no current `work-inbox:*` intake rows.

## Design

The correction has three parts.

### 1. Reviewed reconciliation manifest

One immutable JSON manifest lists all 82 cards. Each entry contains:

- board and card id;
- expected status, workflow step, active-run state, and Work Contract id;
- evidence references used for the classification;
- `card_disposition`;
- `qualification_disposition`;
- related migration/reconciliation intake and contract ids when applicable.

The manifest is generated from the approved audits, reviewed before use, and
hashed into the apply receipt. A live card whose guarded fields differ from the
manifest is skipped and makes the apply fail closed.

### 2. Narrow reconciliation operation

One Hermes-owned command accepts the exact manifest. It defaults to dry-run and
requires an explicit apply flag for mutation.

The command:

1. verifies every board and manifest entry;
2. refuses to touch cards with an active run;
3. creates consistent SQLite snapshots before mutation;
4. applies one transaction per board;
5. writes a receipt containing the manifest hash and before/after counts;
6. is idempotent when re-run with the same manifest.

All validation completes before the first write. If a later board transaction
fails, every board already changed by this apply is restored from its snapshot
before Hermes services resume.

For verified legacy completion, it appends a `legacy_reconciled` event with the
actor, reason, and Git/release/supersession evidence, then makes the card
terminal without fabricating modern workflow runs.

For lab or orphan cards, it uses the existing archive behavior after evidence
preservation. Correctly closed, unfinished, and judgment-required cards receive
no lifecycle change.

### 3. Normal Work Inbox filtering

The default qualification-intake listing excludes sources
`hermes-migration` and `hermes-reconcile`. An explicit source query continues
to expose either source for audit.

The intake rows, decisions, Work Contracts, and existing events are not
deleted or rewritten.

## Card and Qualification Linkage

Every manifest entry has two independent decisions:

- `card_disposition`: keep, close as `legacy_reconciled`, archive, or review;
- `qualification_disposition`: legitimate or
  `migration_artifact_not_qualification`.

For each of the 52 affected cards, a `qualification_history_corrected` event
references the incorrect intake and contract ids and states that they came from
legacy migration rather than real Qualification or Requalification. The ten
verified completed cards also receive the separate `legacy_reconciled` terminal
event.

Existing Work Contracts remain preserved. Open cards keep their current
contract pointer so strict-board execution authority is not removed. Closed
cards retain their immutable contract history; terminal state means the
contract is no longer active execution authority. This cleanup does not create
new Qualification, Requalification, or Work Contracts.

Card truth determines lifecycle action. Qualification cleanup records and
hides the earlier category error but does not route cards.

## Safety and Failure Behavior

- No direct ad hoc SQLite edits.
- No physical deletion of immutable qualification or contract evidence.
- No change to product repositories, preserved worktrees, or Git refs.
- No Trading jobs, keys, orders, capital, or operating automation touched.
- No lifecycle mutation for any card whose evidence or live state is uncertain.
- A snapshot failure, manifest mismatch, active run, transaction error, or
  failed integrity check stops the affected board before further writes.
- A board transaction is rolled back on any error. Post-apply failure uses the
  preserved pre-apply snapshot before Hermes services resume.

Only the Hermes gateway/dashboard may be restarted as part of deploying the
Inbox filter and reconciliation command.

## Verification

Before live apply:

- focused tests prove the normal Inbox excludes both internal sources while
  explicit audit queries still return them;
- fixture tests prove dry-run is read-only, mismatches fail closed, apply is
  idempotent, and no active card can be reconciled;
- the manifest runs successfully against snapshots of all six live boards;
- every snapshot passes `PRAGMA integrity_check`.

After live apply:

- the normal Inbox shows only the three legitimate historical intake records;
- explicit source queries still return all 79 internal records;
- exactly ten verified legacy cards gained terminal reconciliation outcomes;
- exactly three lab/orphan cards were archived;
- unfinished and judgment-required cards did not change lifecycle state;
- all 52 affected cards have explicit correction evidence;
- all six databases pass integrity checks and API/UI counts agree;
- product repositories, preserved worktrees, and Trading runtime are unchanged.

## Non-goals

- No general cleanup framework or dashboard.
- No retroactive replay of Hermes workflow stages.
- No bulk completion based only on age or current column.
- No inference that merged code is deployed when deployment is part of a card's
  acceptance criteria.
- No automatic decision for the ten cards requiring individual judgment.
- No stale-run, orphan-run, or dangling-link repair beyond behavior already
  performed by the existing archive operation.
- No removal of migration recovery bundles.
