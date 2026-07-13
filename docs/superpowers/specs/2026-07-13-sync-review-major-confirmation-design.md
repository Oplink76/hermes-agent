# Confirmed-major review design

## Purpose

Hermes must keep routine upstream synchronization autonomous and involve Ole only as the last resort. A single non-deterministic Claude `major` verdict currently stops the controller immediately, discards the structured findings, and publishes a generic escalation. During recovery, exact read-only replays of the same candidates returned `green` with zero findings. The review gate therefore needs bounded confirmation and durable evidence without weakening exact-head, independent-review, CI, branch-protection, deployment, rollback, or Trading-isolation controls.

## Decision

Use one focused confirmation for a first `major` verdict.

- A first `green` verdict follows the current path with one review call.
- A first `major` verdict must contain at least one non-empty finding. Hermes persists that attempt, then asks Claude to verify only those concrete findings against the same candidate SHA, immutable resolution record, and worktree.
- A confirmation `green` verdict disproves the findings and permits the candidate to continue as minor-resolved.
- A confirmation `major` verdict must also contain findings. Hermes persists both attempts and escalates the confirmed findings to Ole.
- A missing finding, invalid output, changed HEAD, worktree mutation, command failure, or evidence-write failure remains fail-closed and escalates with the available immutable evidence.
- There is no third review, majority vote, automatic override, or retry of execution failures.

This makes a reproducible major finding—not one stochastic model response—the threshold for involving Ole.

## Components and boundaries

### Review execution

`ClaudeConflictReviewer` remains the only reviewer implementation. Codex remains the conflict resolver, preserving backend independence.

The reviewer will use a small internal operation for one structured Claude call. The initial prompt remains unchanged. The confirmation prompt contains:

- the exact candidate SHA;
- the immutable resolution-record path and digest;
- the first attempt's findings encoded as JSON data;
- an instruction to return `major` only for findings supported by the exact code and resolution decision;
- the existing prohibition on edits, commits, pushes, and remote changes.

Arguments remain a normalized list with `--` before the prompt. Findings are never interpolated into a shell command.

### Immutable review evidence

Every completed review attempt is written beneath the configured receipt root in a dedicated `conflict-reviews/` directory. Artifacts are canonical JSON, digest-named, direct regular files, and mode `0400` on POSIX.

Each artifact records only structured evidence:

- schema version;
- candidate SHA;
- resolution-record SHA-256;
- resolver and reviewer backend identities;
- attempt number (`1` or `2`);
- review kind (`initial` or `major_confirmation`);
- verdict and findings;
- review timestamp.

Raw model transcripts are not persisted. Existing immutable files are reused only when their content digest matches.

### Controller outcome

The reviewer returns the final structured receipt plus the attempt artifact paths. A disproved first major returns final `green` with both artifacts attached to the receipt. A confirmed major returns final `major` with both artifacts.

When the controller escalates a confirmed major or a confirmation failure, the decision details reference the trusted review evidence so Ole can see the concrete findings. Generic `AUTONOMOUS_GATE_FAILED` remains reserved for controller failures that are not typed review outcomes.

No change is made to candidate preparation, CI polling, protected merge authority, deployment authority, rollback, runtime scope, scheduling, or notification idempotency.

## Data flow

1. Freeze the conflict-resolution record and verify exact candidate identity.
2. Run the initial Claude review.
3. Verify HEAD and worktree state are unchanged.
4. Persist the initial attempt artifact.
5. If initial is `green`, return it.
6. If initial is `major`, require findings and run one focused confirmation.
7. Again verify HEAD and worktree state, then persist the confirmation artifact.
8. If confirmation is `green`, continue to CI as minor-resolved.
9. If confirmation is `major`, publish a confirmed-major escalation with findings and artifact references.
10. If confirmation cannot be completed safely, fail closed and reference the initial major evidence.

## Error handling and safety

- `major` with an empty findings list is invalid structured output.
- A changed candidate SHA or any worktree mutation invalidates the review.
- Evidence paths must resolve beneath the configured receipt root and may not be symlinks.
- Artifact creation uses exclusive, no-follow semantics and atomic publication.
- Confirmation is bounded to one additional Claude call.
- A confirmation failure never converts a first major into green.
- A confirmed major never enters CI, merge, or deployment.
- Trading gateway definitions and launchd state are not read or modified by this feature.

## Verification

Tests will prove:

- initial green uses one call and persists one exact artifact;
- initial major followed by confirmation green uses two calls and continues;
- initial major followed by confirmation major uses two calls and remains major with concrete findings;
- empty major findings are rejected;
- confirmation command failure and invalid output fail closed;
- candidate or worktree changes before or after either call are rejected;
- artifacts are canonical, digest-bound, non-symlink, read-only, and exact to candidate and resolution record;
- confirmation findings are passed as data in one positional prompt, never shell-interpreted;
- controller decision details reference confirmed-major evidence;
- existing clean-review, protected merge, deployment, rollback, notification, and Trading-isolation tests remain unchanged and green.

The focused review and controller tests run first. The CloudAdvisor operations suite and full repository suite run before merge, followed by the protected GitHub required check.

## Success criteria

- One non-reproducible `major` verdict no longer involves Ole.
- Two exact, structured `major` verdicts with findings do involve Ole.
- Ole receives the concrete persisted findings and evidence paths when escalation is necessary.
- Green, invalid, and confirmed-major paths are deterministic and bounded.
- All existing operational safety gates continue to pass.
