# Infrastructure handover — Hermes Work Inbox / intake needs updating

> **Archival snapshot (2026-07-23):** Preserved as intake-design evidence. Endpoint behavior, qualifier rules, intake states, and requested changes below are historical and must be revalidated before use; this document is not an active work authorization.

**Date:** 2026-07-23
**To:** Infrastructure (Hermes platform / Work Inbox + intake owner). **Not a cockpit issue** — this is the Hermes intake plumbing (`/.well-known/hermes-inbox`, `hermes_cli/kanban_intake.py`, `hermes_cli/kanban_qualifier.py`, `plugins/kanban/dashboard/plugin_api.py`, `plugins/dashboard_auth/work_inbox`).
**Author:** Claude (Ole-directed). Findings are from **live use** submitting a real bug fix through the inbox (2 rejections, 1 success). External analysis — verify before changing code.

## Why now — concrete findings from live use
Submitting a well-evidenced bug fix took **3 attempts / 2 rejections** because the intake rules + guide are under-specified and partly self-contradictory:
- **The failure mode is opaque.** `"skipped-phase evidence is not grounded in submitted or existing evidence"` — I had to read `kanban_qualifier.py` to learn it means: the submission read as finished analysis, so the qualifier tried to enter past the first phase and couldn't ground the skip in verifiable evidence (my externally-authored analysis isn't Product-Owner evidence).
- **The guide is self-contradictory.** `new_work_instructions` says *"submit the complete handoff document"*, but a complete, solution-shaped document is exactly what triggers the skip-phase rejection.
- **The winning pattern is undocumented.** Success came from submitting a **need** (problem + measurable outcome), evidence framed as advisory, with an explicit "no phase skip" request → qualified cleanly into a card at the first phase.
- **No status visibility.** There is no documented way to check an intake's status; I had to poll the SQLite board DB to see qualified/rejected + reason.
- **`assigned_delivery` is under-explained** — its lifecycle (only usable after Hermes assigns work back with task_id/run_id/work_contract_id) confused both Ole and me.

Evidence trail (board `agentic-os-cockpit`): `qi_ff17646a464a0191` rejected (ungrounded) → `qi_8d91fa943f6168d2` rejected (skip-phase, even with embedded evidence) → `qi_845d4df67ba81245` **qualified** → card `t_459b04df` at `backlog` (need-framed, no-skip).

## Requested changes

### 1. Add a dedicated **bug** intake path
Today only `new_work` and `assigned_delivery` exist. A bug has a distinct shape (observed symptom · repro · evidence · expected-vs-actual) and routing (usually a single fix card). Add a `bug` kind with a defined contract that:
- prompts for the right fields (symptom, repro, observed vs expected, evidence references),
- lets the qualifier route an observed defect to a fix workflow without the submitter having to disguise it as `new_work`,
- explicitly defines what **grounds a bug** (observed behaviour / reproducible evidence) so bug submissions don't trip the "skipped-phase / external authorship" check.
This alone would have prevented both of my rejections.

### 2. Intake must span a range of granularities
Ole's intent: intake should accept the full spectrum and decompose appropriately —
- a **loose feature idea** (vague; framework runs full discovery + decomposition),
- a **concrete plan** that needs breaking into **user stories**,
- an **epic** composed of multiple stories,
- a **bug** (see #1).

Today `new_work` effectively assumes *one intent → one card* (the qualifier may optionally emit an epic, but the **submitter cannot signal shape or propose a decomposition**). Update the intake contract so the submitter can declare the **shape/granularity** (`idea | plan | epic | bug`) and, for a concrete plan, supply an **advisory story breakdown** the framework refines — the framework still owns final qualification, epic/story creation, and dependencies.

### 3. Resolve the "external authorship is not Product-Owner evidence" tension
The qualifier rejects submitter-authored analysis as grounds to skip phases (`_validate_skipped_phases` in `kanban_qualifier.py`: skip evidence must be an exact substring of the verifiable evidence corpus). But #2 explicitly wants submitters to hand over concrete plans / proposed stories. The design must **decide and document** how much submitter structure is accepted as **advisory** (refined by the PO phase, never a phase-skip justification) vs. authoritative. Today that boundary is implicit and is the root of the rejections.

### 4. Make the guide self-sufficient (so submitters never read source)
Extend `/.well-known/hermes-inbox` with:
- a `principles` block — *"submit a need, not a solution; the framework runs its own phases; mark any analysis as advisory."*
- a `common_rejections` map — reason → cause → remedy (start with the skip-phase one).
- a `lifecycle` description — `new_work/bug → qualification → (rejected | qualified→card at a phase) → framework runs phases → if a phase is assigned back to you, Hermes issues task_id/run_id/work_contract_id → then assigned_delivery`.
- a documented **status endpoint** — e.g. `GET …/work-inbox/intake/{intake_id}` returning `status` + latest decision reason, so submitters can observe an intake to landing (Ole wants submitters to watch work through, which currently requires DB polling).
- the shape/granularity + bug-path fields from #1–#2.

## Acceptance (measurable, for whoever picks this up)
- A `bug` submission with observed evidence qualifies without a "skipped-phase" rejection.
- A submitter can declare `idea | plan | epic | bug` and, for a plan, attach advisory stories that the framework decomposes.
- The guide alone is sufficient to get a first-try success (no source reading), including a documented status check.
- The advisory-vs-authoritative boundary is documented in both the guide and the qualifier.
