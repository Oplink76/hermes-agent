# Fork Sync Recommendation Handover Design

**Status:** Approved

**Date:** 2026-07-24

**Owner:** Ole Orum-Petersen

**System:** CloudAdvisor Hermes fork operations

## Purpose

Every Hermes fork evaluation already produces:

1. an executive summary of what is new, fixed, and improved; and
2. a Fork Impact Evaluation covering the effect on fork customizations.

When the second report recommends **ADOPT** or **CONVERGE**, that recommendation
must survive the conversation so another AI can later design, plan, or execute
the work under separate authority.

## Decision

Use one plain Markdown handover file per completed fork evaluation. This is an
operating record, not a workflow system.

Store records under:

```text
~/.hermes/recovery/fork-sync-recommendations/
```

Name each file:

```text
YYYY-MM-DD-<upstream-short-sha>.md
```

Do not add runtime code, a database, JSON, schemas, digests, indexes, an outbox,
or automatic task creation.

## Record contents

The file begins with:

- evaluation date;
- fork `main` SHA;
- official upstream `main` SHA;
- merge SHA when a sync has already completed; and
- links or paths to the two reports when available.

Each **ADOPT** or **CONVERGE** recommendation gets one section containing:

- `ID`: `FSR-<upstream-short-sha>-NN`;
- `Action`: `ADOPT` or `CONVERGE`;
- `Status`: `open`, `completed`, `declined`, or `superseded`;
- the recommended outcome and why it matters;
- upstream commits or files supporting the recommendation;
- fork behavior and constraints that must be preserved;
- the suggested next stage: `design`, `plan`, or `implementation`; and
- dated disposition evidence when the status changes.

No record is required when an evaluation contains no **ADOPT** or
**CONVERGE** recommendation.

## Operating flow

1. Before a new fork evaluation, read existing records with open items.
2. Produce the executive summary and Fork Impact Evaluation in the agreed
   format.
3. Record every new **ADOPT** or **CONVERGE** recommendation in that
   evaluation's Markdown file.
4. Reassess older open items instead of duplicating them. Add a dated
   disposition when an item is completed, declined, or superseded.
5. End the two-report delivery with the path to the handover record.

A later AI may use an open item as input to design, planning, or implementation,
but the record itself grants no authority and changes no Hermes board state.

## Minimal implementation

Implementation is documentation-only:

- add the short operating rule and inline Markdown template to the existing
  CloudAdvisor operations guide; and
- create the first record for the latest fork evaluation.

No production code or automated tests are needed. Verification consists of
checking that the documented path, required fields, authority boundary, and
first record agree with this design.

## Acceptance criteria

1. Every reported **ADOPT** or **CONVERGE** recommendation has a durable ID and
   status.
2. A future AI can find open recommendations using ordinary file search.
3. Repeated evaluations do not create duplicate recommendations.
4. Records never create tasks, mutate Kanban, or authorize execution.
5. The solution adds no runtime code or new operational subsystem.

## Non-goals

- Automatically creating Work Inbox or Default-board work.
- Automatically designing, planning, implementing, or closing recommendations.
- Reusing authorization-sensitive sync receipt machinery.
- Tracking **KEEP**, **REMOVE**, or **DEFER** findings unless they explain an
  **ADOPT** or **CONVERGE** recommendation.
