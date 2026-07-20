# Minimal Work Inbox Delivery Boundary Design

**Status:** Approved
**Date:** 2026-07-19
**Admission semantics clarified:** 2026-07-20
**Supersedes:** The earlier durable-receipt design on the abandoned
`design/work-inbox-delivery-boundary` implementation branch.

## Intent

Hermes Work Inbox is the authenticated local door through which an AI outside
the Hermes-managed framework either submits genuinely new work or returns work
Hermes assigned. New work is submitted only after Ole approves its admission
to Hermes. That approval means the work may enter the framework; it does not
authorize immediate execution or bypass Product Owner, qualification, Work
Contract, evidence, routing, or handover rules.

The submitted payload remains technically untrusted input and cannot mutate
project state directly. Qualification is the framework step that structures
the approved intent, reconciles current work, resolves decisions within its
authority or returns them to Ole, and issues a signed Work Contract.
Requalification remains the correction path for an existing misqualified or
stuck card.

The smallest correct implementation is one thin POST adapter over behavior
Hermes already has:

```text
Ole admission approval
    |
    v
local external AI
    |
    v
POST Hermes Work Inbox
    |
    +-- new_work ----------> existing submit_intake
    |                         -> existing qualification
    |                         -> Work Contract and normal workflow
    |
    +-- assigned_delivery -> exact task/run/Work Contract check
                              -> existing complete_task or block_task
                              -> existing Normal Handover

existing stuck card ------> existing Hermes-only requalification
```

## Non-Negotiable Boundaries

1. Work Inbox submission attests that Ole approved admission to the framework;
   it does not attest that the work is ready for immediate execution.
2. External callers submit intent and evidence; they never choose phase, assignee,
   dependency, Epic membership, next role, qualification path, release,
   override, or break glass.
3. `new_work` creates only an existing qualification intake. It creates no
   task directly.
4. `assigned_delivery` is valid only for the exact currently running task,
   run, and Work Contract already supplied by Hermes.
5. Assigned completion and blocking call the existing run-scoped
   `complete_task` or `block_task` functions with `expected_run_id`. No second
   handover implementation is allowed.
6. Work produced without an active Hermes assignment is `new_work` with
   evidence. It cannot be attached to an arbitrary existing card.
7. Requalification remains Hermes-only, preserves the existing card, and does
   not appear as an external submission kind.

## Public Surface

Keep the public discovery URL:

```http
GET /.well-known/hermes-inbox?board=<board-slug>
```

Guide version 2 is named **Hermes Work Inbox** and points to one route:

```http
POST /api/plugins/kanban/work-inbox?board=<board-slug>
Authorization: Bearer <machine credential>
```

The route accepts exactly two closed request shapes.

The guide must say that the caller is local but outside the framework, that Ole
has already approved admission before submission, and that qualification still
performs the normal framework process before any Work Contract authorizes
execution. The machine credential authenticates the narrow Inbox caller; it is
not a general Hermes or board credential.

### New work

```json
{
  "version": 2,
  "kind": "new_work",
  "request": {
    "functional_intent": {
      "title": "Capability title",
      "desired_outcome": "Measurable outcome",
      "project": "project identity",
      "repository": "repository when known",
      "scope": ["included behavior"],
      "out_of_scope": ["excluded behavior"],
      "aliases": []
    },
    "evidence": [
      {"kind": "commit", "reference": "optional existing commit"}
    ]
  },
  "session_id": "external session when known",
  "attachments": []
}
```

Hermes passes `request`, `session_id`, and `attachments` unchanged to the
existing `kanban_intake.submit_intake` service and returns its existing
`qi_...` receipt. The existing qualifier remains the only component that may
create and route a card. A rich discovery handoff may carry verified current
state, desired outcomes, constraints, unresolved decisions, proposed sequence,
and document evidence. One submission does not automatically materialize that
sequence as executable cards; the Product Owner and qualifier decide the
correct Epic/story decomposition through the normal framework.

### Assigned delivery

```json
{
  "version": 2,
  "kind": "assigned_delivery",
  "task_id": "t_...",
  "run_id": 123,
  "work_contract_id": "wc_...",
  "outcome": "completed",
  "summary": "What was delivered",
  "result": "Short result",
  "metadata": {
    "ai_provenance": {
      "writer": {"agent": "external agent"}
    },
    "changed_files": ["path/to/file"],
    "tests_run": ["focused tests passed"]
  }
}
```

For `outcome=blocked`, the request instead includes an existing Hermes
`block_kind` and optional `attempted_resolutions`. The adapter validates the
closed shape, accepts only the existing handover metadata keys
`ai_provenance`, `changed_files`, `tests_run`, and `workflow_outcome`, and
performs these read-only checks before handover:

- requested board is a strict Hermes board;
- task exists and is an executable card;
- task is running and not goal-mode or `release_measure`;
- `current_run_id` equals `run_id`;
- current `work_contract_id` equals `work_contract_id`; and
- the referenced run is active and belongs to the task.

It then calls the existing completion or block function with
`expected_run_id`. Existing provenance, Test/Review, rework, release, block,
run-CAS, and Normal Handover rules remain authoritative.

## Authentication

Register only the exact Work Inbox path with the existing dashboard token-auth
middleware. A small dedicated secret provider reads
`HERMES_WORK_INBOX_SECRET` and always grants the fixed scope
`work_inbox:submit` to principal `work-inbox`.

The credential is never returned by the public guide. The token cannot list
boards, cards, runs, contracts, or receipts and cannot call qualification,
requalification, release, or general card-mutation routes.

## Response and Retry Semantics

- Valid `new_work`: return the existing `202 qualification_required` response
  and `qi_...` intake id. Here `qualification_required` means framework
  structuring and contracting are pending, not that Ole's admission approval is
  pending.
- Valid assigned completion/block: return the resulting task id, submitted run
  id, and `handover_applied` or `blocked` outcome after the existing operation
  commits.
- Malformed or authority-bearing request: `400`, no mutation.
- Unknown board/task: `404`, no mutation.
- Stale, foreign, terminal, uncontracted, or mismatched assignment: `409`, no
  mutation.
- Existing handover policy failure: `409` with a bounded safe message; detailed
  evidence remains in existing task events.

There is deliberately no new `wi_` receipt, receipt table, GET endpoint,
processing state, lease, generation fence, recovery sweep, or retry queue.
Existing task/run compare-and-swap is the duplicate-execution guard. If an
assigned-delivery response is lost, the caller must not retry blindly; it
reports the ambiguous result and Hermes reconciles from the existing run and
task events.

## Compatibility

- Existing `/api/plugins/kanban/intake` and its `qi_...` receipts remain
  unchanged.
- Existing workers, tools, qualification, requalification, Work Contracts,
  task events, and Normal Handover remain unchanged.
- The Work Inbox adapter is an additional authenticated entry point only.
- Existing `qualification_required` response compatibility remains unchanged;
  its clarified meaning is framework processing after approved admission.
- Agent Memory remains advisory and outside this authority boundary.

## Explicit Non-Goals

- No new database table or column.
- No new lifecycle, phase, role, scheduler, daemon, queue, or watcher.
- No receipt lookup API or caller-specific receipt ownership model.
- No extraction or redesign of worker completion/block normalization.
- No Cockpit mutation path.
- No change to requalification.
- No automatic distributed retry protocol.

## Alternatives Considered

**Rename the guide only:** smallest diff, but it still gives an assigned
external worker no governed return path. It does not satisfy the intended
design.

**Thin adapter over existing seams — selected:** adds only authentication,
one closed POST route, assignment checks, and guide copy. Hermes continues to
own all durable state and transitions through existing intake and handover.

**Durable Work Inbox receipt engine — rejected:** gives callers richer retry
and receipt lookup behavior, but duplicates persistence and recovery concerns
already owned by Hermes. The abandoned implementation demonstrated that this
turns a boundary correction into a new subsystem.

## Acceptance

The minimal change is complete when behavior tests prove:

1. the guide says **Hermes Work Inbox** and explains both request kinds;
2. the guide states that local Inbox submissions are Ole-approved admissions
   that still require the framework process and a Work Contract before execution;
3. only a valid bearer token can POST to the exact route;
4. `new_work` produces one existing qualification intake and no task;
5. exact assigned completion/block uses the existing run-scoped operation and
   Normal Handover;
6. stale task/run/contract and forbidden routing/private metadata produce no
   task mutation;
7. unassigned existing patches must use `new_work` with evidence;
8. requalification remains internal and same-card; and
9. the diff adds no persistence or background processing mechanism.
