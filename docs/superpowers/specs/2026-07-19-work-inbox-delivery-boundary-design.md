# Work Inbox Delivery Boundary Design

**Status:** Approved
**Date:** 2026-07-19

## Purpose

Every external development AI needs one Hermes-owned door for both asking for
new work and returning work that Hermes assigned. The door is the **Work
Inbox**. Qualification and requalification are decisions Hermes may make after
receipt; neither is the inbox itself.

The existing implementation is authority-safe but conceptually too narrow. Its
public guide is named “Hermes Qualified Work Inbox” and documents only inert
work intent. It cannot represent an assigned worker returning a result through
the same governed boundary.

## Decision

Keep one public Work Inbox and dispatch each authenticated submission into an
existing Hermes seam:

```text
external AI
    |
    v
Work Inbox (durable, idempotent receipt; no routing authority)
    |
    +-- new_work ----------> initial qualification
    |                         -> signed Work Contract
    |                         -> normal Hermes workflow
    |
    +-- assigned_delivery -> validate task + run + Work Contract
                              -> existing complete/block operation
                              -> Normal Handover

existing stuck/misrouted card
    |
    +-- Hermes only -------> requalification
                              -> successor Work Contract on same card
                              -> Normal Handover
```

The Work Inbox is transport and durable audit. It never decides the phase,
assignee, next role, qualification path, or whether evidence satisfies a
handover.

## Canonical Language

**Work Inbox** is the external submission boundary. Avoid “Qualification
Inbox” and “Qualified Work Inbox.”

**New Work Submission** is inert, uncontracted intent. Initial qualification
may accept it and issue a Work Contract, reject it, or request a genuine
business decision.

**Assigned Delivery** is a result returned for the exact task, active run, and
Work Contract Hermes assigned. The caller may report `completed` or `blocked`;
Hermes owns validation and the resulting transition.

**Qualification** is initial authorization of new work only.

**Requalification** is Hermes-owned correction of an existing card whose
current Work Contract or route is wrong or leaves it without a valid next
action. It preserves card identity and uses a successor Work Contract.

**Normal Handover** is the only delivery transition. It validates evidence and
advances the existing card according to its Work Contract and workflow.

## Non-Negotiable Invariants

1. External callers submit; they never qualify, requalify, assign, route,
   advance, release, merge, or invoke break glass.
2. New work remains inert until a signed Work Contract exists.
3. Assigned delivery is accepted only for the exact current task, active run,
   and current Work Contract. Any stale or mismatched identity is rejected
   without changing the card.
4. A successful assigned delivery calls the existing run-scoped complete or
   block operation. No second handover implementation is allowed.
5. Independently produced work with no active Hermes assignment is not an
   Assigned Delivery. It enters as `new_work` with patch/evidence references;
   qualification may justify late entry to Test or Review.
6. Requalification remains internal to Hermes and never appears as an external
   submission kind.
7. One authenticated caller and one `client_request_id` identify one immutable
   submission. A retry returns the original receipt and cannot repeat a
   qualification intake or handover.
8. Receipt persistence and project mutation are auditable. A crash or retry
   cannot make the same run complete or block twice.
9. Agent Memory may inform qualification or a worker but remains advisory and
   outside the Work Inbox authority boundary.

## Public Discovery Guide

Keep the existing discovery URL:

```http
GET /.well-known/hermes-inbox?board=<board-slug>
```

Version 2 returns the name **Hermes Work Inbox** and documents two submission
kinds. It points to one fixed, token-authable route so both submit and receipt
lookup fit the existing exact-path token-auth middleware:

```http
POST /api/plugins/kanban/work-inbox?board=<board-slug>
GET  /api/plugins/kanban/work-inbox?board=<board-slug>&submission_id=<wi_id>
```

The guide remains public and contains no token, private task data, signing
material, filesystem path, or board listing. The existing dashboard-only
qualification-intake routes remain available for compatibility and internal
inspection; external AI guidance no longer points at them.

## Submission Envelope

All submissions use version 2 and share immutable caller provenance:

```json
{
  "version": 2,
  "client_request_id": "stable-id-from-calling-system",
  "kind": "new_work | assigned_delivery",
  "source": {
    "agent": "external-agent-name",
    "session_id": "external-session-id"
  },
  "new_work": null,
  "assigned_delivery": null
}
```

Exactly one kind-specific object must be present.

### New work

```json
{
  "kind": "new_work",
  "new_work": {
    "functional_intent": {
      "title": "Readable capability title",
      "desired_outcome": "Measurable user-visible outcome",
      "project": "project identity",
      "repository": "repository URL or path when known",
      "scope": ["included behavior"],
      "out_of_scope": ["excluded behavior"],
      "aliases": []
    },
    "evidence": [
      {"kind": "patch", "reference": "commit-or-PR-reference"}
    ]
  }
}
```

Hermes persists the Work Inbox submission, creates or returns one existing
qualification intake, and returns a receipt linking the two. The qualifier may
late-enter an existing patch only when current evidence satisfies the existing
entry validator.

### Assigned delivery

```json
{
  "kind": "assigned_delivery",
  "assigned_delivery": {
    "assignment": {
      "task_id": "t_...",
      "run_id": 123,
      "work_contract_id": "wc_..."
    },
    "outcome": "completed",
    "summary": "What was delivered and what remains",
    "result": "Short result line",
    "metadata": {
      "ai_provenance": {
        "writer": {"agent": "external-agent-name"}
      },
      "changed_files": ["path/to/file"],
      "tests_run": ["focused suite: 12 passed"]
    },
    "evidence": [
      {"kind": "commit", "reference": "abc123"},
      {"kind": "test", "reference": "focused suite: 12 passed"}
    ],
    "attempted_resolutions": []
  }
}
```

For `outcome=blocked`, `block_kind` is required and must be one of the existing
Hermes block kinds; `summary` is the reason. For `outcome=completed`,
`block_kind` is forbidden. `metadata` follows the existing worker handover
contract, including `ai_provenance` and `workflow_outcome` when the current
phase requires them. The caller cannot provide phase, assignee, status, next
role, dependency, Epic membership, qualification path, release decision, or
override data.

Hermes validates that:

- the task exists on the requested board and is a contracted executable card;
- the card is not an Epic and is not in `release_measure` or another terminal
  phase;
- `current_run_id` equals the submitted run;
- the submitted Work Contract is the card's current contract;
- the run is active and belongs to the card's current assignee/phase; and
- the receipt has not already produced a terminal delivery disposition.

Hermes then passes the bounded `summary`, `result`, `metadata`, `evidence`, and
`attempted_resolutions` through the same delivery-normalization service used by
the worker tools and into the existing `complete_task` or `block_task` call
with `expected_run_id`. Existing commit, provenance, rework, goal, Test,
Review, release, and phase-transition checks remain authoritative.

## Durable Receipt and Idempotency

Add a small Work Inbox receipt store. It is not a lifecycle or work queue; it
is immutable submission provenance plus the current processing disposition.

Each receipt records:

- opaque `submission_id`;
- board;
- authenticated principal and provider;
- `client_request_id`;
- kind and immutable canonical payload;
- status: `received`, `accepted`, or `rejected`;
- disposition: `qualification_pending`, `qualified`, `handover_applied`,
  `blocked`, or `rejected`;
- linked qualification intake, task, run, Work Contract, and transition event
  when applicable; and
- timestamps and a bounded rejection reason.

`board + provider + principal + client_request_id` is unique. A retry with the
same canonical payload returns the original receipt. Reusing the identity with
different content returns `409 idempotency_conflict`.

For Assigned Delivery, the task transition event stores the Work Inbox
`submission_id`. Receipt recovery treats that event as the durable proof that
the handover already happened, so a crash after transition commit cannot cause
a retry to apply it again.

The exact task/run/Work Contract tuple is delivered only in Hermes' assignment
context. The bearer credential cannot list boards, cards, runs, contracts, or
other callers' receipts, so it cannot discover a foreign tuple through the Work
Inbox surface.

## Authentication and Authorization

Use the existing dashboard token-auth seam. Register only the exact Work Inbox
path. A dedicated machine credential provider returns a `TokenPrincipal` with:

- `work_inbox:submit` for POST;
- `work_inbox:receipt` for GET.

The route checks the relevant scope and stores the principal on every receipt.
A caller may read only receipts created by the same provider and principal.
Dashboard session authentication continues to use the existing internal intake
routes and does not grant external Work Inbox bearer scope.

The credential is a secret and may be configured through the existing secret
configuration conventions. The public guide describes the required scope but
never includes the credential.

## Processing and Failure Semantics

- Invalid envelope or forbidden routing fields: `400`, no receipt.
- Valid authenticated envelope: persist the receipt before any downstream
  action.
- Unknown/stale task, run, or Work Contract: reject receipt, return `409`, no
  task mutation.
- Existing idempotent receipt: return it without repeating work.
- Qualification rejection: receipt links the rejected intake; no card exists.
- Existing completion/block validation failure: reject receipt with a bounded
  safe reason; existing task events remain the detailed audit.
- Process crash after receipt but before action: retry or a bounded Hermes
  sweep resumes that receipt.
- Process crash after handover commit: recovery finds the transition event by
  `submission_id`, marks the receipt accepted, and never calls handover again.
- Receipt processing failure never bypasses Work Contract, run, or workflow
  checks and never escalates routine errors to Ole.

## Compatibility

- Existing version-1 `/api/plugins/kanban/intake` submissions continue to mean
  New Work Submission and continue returning `qi_...` receipts.
- Existing qualification and requalification tables, Work Contracts, strict
  board triggers, and watcher remain authoritative.
- The new Work Inbox bridge may call the existing v1 intake service for
  `new_work`; it must not duplicate qualification logic.
- Existing workers and Kanban tools remain unchanged. The Work Inbox is an
  additional delivery adapter into their existing run-scoped operations.

## Concrete Scenarios

### New feature request

Claude submits `new_work`. Hermes stores `wi_...`, creates one inert `qi_...`,
qualifies it, and exposes both identities on the receipt. No card exists before
the Work Contract.

### Existing external patch

Codex has a commit but no Hermes assignment. It submits `new_work` with the
commit as evidence. Hermes may qualify the card directly to Test or Review if
the existing evidence validator permits it. It is not attached as completion
to an unrelated card.

### Assigned worker completion

Hermes assigns task `t_1`, run `42`, contract `wc_a`. The worker returns those
exact identities. Hermes invokes existing run-scoped completion; Normal
Handover advances the card. Retrying returns the same `wi_...` receipt.

### Stale worker

The card has already been requalified to `wc_b` or claimed as run `43`. A
delivery for `wc_a`/run `42` is rejected without changing the card.

### Misqualified stuck card

No external submission is created. Hermes' existing recovery trigger requests
requalification, installs a successor Work Contract on the same card, and
returns it to Normal Handover.

## Alternatives Rejected

**Rename the guide only:** fixes language but still cannot receive assigned
deliveries or guarantee idempotent orchestration.

**Create a parallel external-delivery workflow:** duplicates run, evidence,
block, and handover rules and can drift from the framework.

**Allow delivery against any existing card without an active run:** lets
unassigned external work mutate governed state and makes authorship,
provenance, and retry safety ambiguous. Existing patches instead enter as new
work with evidence.

## Scope

This design includes the Work Inbox receipt, dedicated scoped authentication,
new-work bridge, assigned-delivery adapter, guide v2, bounded crash recovery,
and behavior-level tests.

It does not redesign qualification, requalification, Work Contracts, phase
transitions, release policy, Agent Memory, Cockpit, or the worker tools. It does
not add a Kanban phase, worker role, scheduler, or lifecycle.

## Acceptance

The design is enforced when tests prove:

1. the guide calls the surface Hermes Work Inbox and documents both kinds;
2. an external token can access only the exact route and required scope;
3. retries cannot create duplicate qualification intake or handover;
4. new work stays inert until qualification;
5. assigned completion/block uses the exact current task/run/contract and the
   existing normal handover behavior;
6. stale, foreign, terminal, uncontracted, and mismatched deliveries make no
   task mutation;
7. independently produced patches cannot masquerade as assigned delivery;
8. requalification remains Hermes-only and same-card; and
9. existing qualification, requalification, handover, dashboard, and Agent
   Memory tests remain green.
