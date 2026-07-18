# Normal-Handover Requalification Design

**Status:** Proposed for implementation

## Purpose

Qualified work can currently remain parked even though the normal v2 handover engine is healthy. The clearest example is qualified development work in `scheduled`: the dispatcher deliberately ignores it, and nothing in the normal gateway tick wakes it.

Requalification must not become another workflow. It is a Hermes-owned correction that returns an existing card to the same qualification and handover path used by all other governed work.

## Decision

Use the existing qualification intake, signed Work Contract, handover, dispatcher, and reconcile loop.

Add one intake kind: `task_requalification`. It references an existing task and carries the authoritative task snapshot, recorded evidence, and reason that its current contract or route needs review. The existing qualifier decides the valid scope, route, dependencies, entry phase, and handover.

On acceptance, Hermes:

1. stores a new immutable signed Work Contract;
2. atomically replaces the existing task's Work Contract and contract-owned routing under the existing Hermes service authority;
3. replaces dependencies and Epic membership with the successor contract's values;
4. records the old and new contract IDs in a `requalified` task event; and
5. derives `todo` or `ready` from dependencies, allowing the existing dispatcher and handover engine to continue the card.

The old Work Contract remains immutable and auditable. `requalification` is never represented as `qualification_path=override`.

## Flow

```text
ordinary intake -> qualification -> signed contract -> normal handovers -> done
                                           ^
                                           |
existing card -> Hermes requalification intake
```

Only Hermes may request or apply requalification. Codex, Claude, Cockpit, and other clients can still submit inert ordinary intake, but cannot re-route a card or write the qualification marker.

Ole's authenticated direct instruction to Hermes remains the only break-glass override. It uses the existing `override` qualification path and its existing audit requirements.

## Recovery Trigger

Extend the existing bounded `reconcile()` safety net; do not create a daemon, scheduler, or controller.

One reconcile pass may enqueue at most one requalification intake. It acts only on an unambiguous, non-terminal card with no worker and no valid normal next action. For the first implementation slice this means a qualified `scheduled` card on a strict v2 product board. Product-development sequencing belongs in contract dependencies; `scheduled` remains available elsewhere for genuine time-based waits.

The intake record is the idempotency marker: a task with a pending requalification intake is not enqueued again. A rejected intake suppresses retries only while its evidence digest is unchanged; new task, history, dependency, or repository evidence permits one new ordinary qualification attempt.

These are not stale and must not be requalified:

- `todo` with incomplete dependencies;
- assigned `ready` work, which belongs to the dispatcher;
- `running` work, which already has dead-worker recovery;
- an explicit unresolved blocker;
- terminal `done` or `archived` work; or
- `release_measure`, which is a normal terminal handover and must follow its release-evidence policy.

Age alone never changes a card.

## Existing Parked Work

After deployment, the same reconcile trigger feeds qualified scheduled cards through requalification one at a time. The qualifier must use current task events, comments, runs, dependencies, and repository evidence:

- already delivered work is routed to the latest justified phase without rerunning completed phases;
- unfinished work resumes at the earliest phase supported by evidence; and
- dependencies replace sequencing-by-`scheduled`.

No bulk status rewrite is allowed.

## Alternatives Rejected

**Separate stale-card reconciler or release controller:** duplicates the engine and creates another lifecycle to operate.

**Make the qualification marker an editable tag:** lets clients forge routing authority and breaks the signed-contract boundary.

**Automatically unblock every scheduled card:** resumes work without re-checking whether the original route still matches current evidence.

## Verification

Tests must prove:

- a signed successor contract requalifies the same task rather than creating a second task;
- the old contract remains stored and the event links old to new;
- dependencies, Epic membership, phase, and assignee come only from the successor contract;
- the resulting state is `todo` when dependencies are open and `ready` when they are satisfied;
- ordinary clients cannot change the contract or routing;
- requalification cannot use the break-glass override path;
- reconcile enqueues at most one eligible card and never duplicates pending intake;
- valid waits and terminal cards remain untouched; and
- the existing v2 handover and dispatcher tests continue to pass.

## Success Criteria

Qualified development cards no longer remain inert merely because they were parked as `scheduled`. They re-enter the existing governed flow automatically, while valid dependency waits, blockers, release gates, and break-glass authority keep their existing meaning.

## Non-goals

- no new lifecycle status;
- no new worker profile;
- no new periodic service;
- no age-based state mutation;
- no client-side orchestration; and
- no Trading-board activation.
