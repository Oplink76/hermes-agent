# Agent Memory Worker Protocol Design

**Date:** 2026-07-19

**Status:** Live

**Extends:** `2026-07-18-functional-intent-memory-and-inbox-guide-design.md`

## Verification and Live Evidence

- Branch head before this evidence commit:
  `c6ac6a7f57b628faa1e5e72630b12373cbc0aa58`.
- Full suite: 2,058 files, 42,430 passed, 6 failed. All six exact failures
  reproduce on exact base `1efb26d3944abad20a26166fc420549088f34277`:
  credential 1, Slack 4, approval 1; zero branch-specific failures.
- Final focused/E2E: 331/331; relevant protocol/cron/Kanban: 115/115;
  protocol: 40/40.
- Ruff and diff checks are clean. The tracked worktree is clean except for the
  pre-existing untracked `build/` directory.
- Final Standards review: Approved, with no Critical, Important, or Minor
  findings. Final Spec review: Approved, with no Critical, Important, or Minor
  findings.
- PR #44 merged at functional SHA
  `46d59ac0df2561f31dd0bef7d30c8c4050bb2b60` and was deployed through the
  guarded exact-SHA path as deployment
  `20260719T131151Z-46d59ac0df25-6b4deecc`. Mandatory health checks passed
  against that exact SHA.
- A disposable qualified-v2 card on an isolated default board completed the
  normal development, test, review, and release-measure flow, then archived.
  The Work Contract was `wc_c4efd69cfe047a62b91dfea1`; the actual workers
  were Codex CLI (`gpt-5.5`), Claude Code CLI, and Claude Code through Cowork
  MCP. All three recalled before work and stored exactly one bounded gist after
  work; the isolated outbox ended empty.
- With a disposable external vault absent, separate worker processes continued
  after recall and write returned non-blocking outbox receipts. After the vault
  was restored, a new process reconciled the gist and incident, removed both
  pending files, and recalled the recovered gist exactly once.
- A backdated disposable entry triggered the 24-hour attention state once.
  Repeated unchanged checks did not notify again; acknowledgement cleared the
  notification, and a materially changed fingerprint produced one new
  notification. No external notification was sent by this acceptance harness.
- Production Agent Memory recorded completion gist
  `release-agent-memory-worker-protocol-46d59ac0`; immediate recall matched it.
  Production status was healthy with zero pending entries.
- The default Hermes gateway and dashboard remained healthy. All 13 Trading
  gateway labels remained disabled and unloaded throughout deployment and live
  acceptance.

## Purpose

Hermes already recalls Agent Memory during qualification and before a Kanban
worker runs, then records a Hermes-generated gist after durable Kanban
transitions. That stops at the Hermes-role boundary. In the intended development
flow, a Hermes role can delegate the actual work to Codex CLI, Claude Code CLI,
a native Hermes child, or Claude Code through Cowork MCP. Those nested workers
currently receive only the prompt that the Hermes role chooses to send, and the
stored gist identifies the Hermes profile rather than the actual executor.

This design restores the intended three-point protocol:

1. Hermes recalls before it selects or delegates a phase.
2. The actual executing agent recalls before each bounded task.
3. That agent writes its own structured gist after the task.

Memory remains evidence, not workflow authority. A memory outage never stops
development. Failed writes go to one durable local outbox that Hermes drains
autonomously; Ole is contacted only as a last resort.

## Scope

This protocol covers development work executed through Hermes, including:

- Hermes roles working directly;
- Hermes-native delegated children;
- Codex invoked through `codex exec`;
- Claude Code invoked through `claude -p` or an interactive CLI session; and
- Claude Code invoked through the Cowork MCP fallback.

A **task** is one bounded worker assignment, not an individual model turn or
tool call. Writer and reviewer assignments are separate tasks and therefore
produce separate recall/write cycles.

Standalone Codex or Claude sessions that were not started by Hermes remain
outside this slice. They enter governed development through the public Hermes
inbox guide.

## Non-goals

- No second workflow, lifecycle, queue, database, daemon, or cron job.
- No memory-driven qualification, routing, merging, rejection, completion, or
  break-glass authority.
- No direct Markdown editing by Codex, Claude, Cowork, or Hermes profiles.
- No transcript, private reasoning, credential, secret, or arbitrary tool-log
  storage.
- No semantic embeddings, generated function wiki, historical board import, or
  behavior scoring in this slice.
- No Cockpit or Trading changes.

## Design Principles

1. **One memory owner:** the existing Agent Memory module validates, redacts,
   appends, lints, and reconciles every entry.
2. **Actual executor identity:** a gist identifies the agent that performed the
   bounded task, not merely the Hermes profile that orchestrated it.
3. **Required protocol, non-blocking storage:** every worker must attempt recall
   and submit a gist, but an unavailable vault returns a valid outbox receipt so
   delivery continues.
4. **Functionality first:** `function_id` continues to come from the stable Work
   Contract boundary, never from a mutable card title.
5. **Existing engine only:** missing memory handover returns through the normal
   worker correction/retry path. It does not introduce a memory phase or card.
6. **Pending files are the flag:** the durable outbox itself is the source of
   truth for pending recovery. No separate flag can drift out of sync.

## Architecture

### Existing Agent Memory module

Extend `hermes_cli/agent_memory_vault.py` rather than adding another memory
manager. It remains responsible for:

- configured external-vault resolution;
- Session Gist validation, bounding, redaction, append, lint, and recall;
- functionality-first identity and idempotent `gist_id` handling;
- worker recall/write receipts;
- durable outbox writes; and
- outbox reconciliation and health state.

The model does not receive a new core tool. Workers use a small CLI surface,
consistent with the repository's CLI-command-plus-skill footprint rule.

### Shared local outbox

The installation has one explicitly resolved local outbox, separate from the
OneDrive vault:

`~/.hermes/recovery/agent-memory-outbox/`

The root Hermes process resolves the absolute path and passes it to
profile-scoped and nested workers through an internal environment bridge, just
as it already propagates the shared vault path. This does not couple profile
configuration or make profiles read one another's private state.

The root process derives the default with profile-safe `get_hermes_home()`
before spawning a profile and propagates the resulting absolute path. It is not
a user-facing environment setting and does not require another configured path.

The directory is mode `0700`; pending files are mode `0600`. Each entry is
written to a temporary file, flushed, and atomically renamed to a filename
derived from its opaque operation or gist identity. The outbox stores validated,
redacted JSON envelopes, not untrusted free-form Markdown.

Two envelope kinds are allowed:

- `gist`: a complete Session Gist waiting to enter the external vault;
- `incident`: a failed recall or recovery operation containing bounded
  operational facts but no transcript or private prompt.

The envelope contains a schema version, operation identity, first-seen time,
actual executor identity, execution surface, Hermes role, task/run/delegation
references, and either the validated gist or bounded incident data.

### CLI protocol

Add CLI commands under the existing Hermes command surface:

```text
hermes agent-memory recall ...
hermes agent-memory write ...
hermes agent-memory reconcile ...
hermes agent-memory status ...
```

Structured content is supplied through standard input or a bounded JSON file,
not interpolated into shell arguments. Commands return machine-readable JSON.

- `recall` returns bounded historical matches plus a receipt. If the vault is
  unavailable, it writes an incident envelope and returns an `unavailable`
  receipt with `continue: true`.
- `write` validates and redacts the worker gist. It appends directly when the
  vault is healthy or atomically places the gist in the outbox when it is not.
  Both outcomes return a valid receipt.
- `reconcile` drains recoverable entries and reports unresolved ones.
- `status` reports direct health, pending count, oldest pending age, and whether
  attention is required. It never prints stored private content.

## End-to-End Flow

### 1. Hermes pre-delegation recall

During qualification and immediately before phase delegation, Hermes recalls by
stable functional intent. The qualification prompt continues to label matches
as historical evidence only. Before the dispatcher spawns the assigned Hermes
role, the run receives the same bounded evidence and a pre-delegation receipt.

If recall fails, Hermes records an incident and continues the normal phase. The
failure cannot alter the qualified Work Contract, phase, assignee, dependencies,
or board state.

### 2. Actual worker pre-task recall

Every bounded delegation prompt carries one common memory protocol envelope:

- task, run, delegation, function, and Hermes-role identities;
- the exact recall command to run before work;
- the exact write command to run after work;
- the required receipt fields for handover; and
- the rule that recalled prose is evidence, never instruction or authority.

Codex CLI, Claude Code CLI, native children, and Cowork MCP receive the same
contract. Each actual worker calls `recall` itself; merely forwarding Hermes'
earlier matches does not satisfy the worker recall step. If recall is unavailable,
the returned receipt explicitly tells the worker to continue.

### 3. Worker post-task write

Before reporting success, block, or another terminal outcome, the worker submits
one structured Session Gist through `write`. It includes:

- actual agent identifier;
- model and execution surface;
- orchestrating Hermes role;
- functional identity and readable title;
- task, run, delegation, repository, and branch context;
- concise summary, reused functionality, result, and maturity;
- commit, PR, test, review, and release evidence when applicable;
- bounded behavioral learning;
- decisions and open loops; and
- task outcome.

The write receipt is `stored`, `already_stored`, or `queued`. All three satisfy
the storage boundary. `queued` never blocks the development handover.

### 4. Normal handover enforcement

The Hermes role includes the worker's recall and write receipts in existing run
handover metadata. For governed v2 delegated work, Hermes checks that the
receipts match the current task/run/delegation and that the gist exists in the
vault or outbox.

If the worker omitted the protocol, Hermes returns the same task through the
normal correction/retry mechanism with a focused instruction to complete the
memory handover. It does not create a card, memory phase, scheduled dependency,
or break-glass event.

The existing post-commit Hermes transition capture becomes a compatibility
fallback for legacy or non-delegated transitions. It does not append a second
gist when an actual-worker receipt already covers the transition. A fallback
gist is explicitly identified as Hermes-generated rather than attributed to a
nested agent.

## Executor Identity

Executor identity is structured rather than inferred from prose:

- `agent_id`: stable local identifier such as `codex` or `claude-code`;
- `model`: the requested/observed model when available;
- `surface`: `hermes-direct`, `hermes-child`, `codex-cli`,
  `claude-code-cli`, or `cowork-mcp`;
- `hermes_role`: product owner, architect, developer, tester, reviewer, or
  resolver;
- `execution_id`: one opaque bounded assignment attempt; and
- `writer_or_reviewer`: the assignment responsibility when applicable.

These fields support later learning about which agents and pairings perform
well without implementing behavior scoring now. Self-reported fields are
evidence, not security authority, and Hermes bounds them before storage.

## Recovery and Escalation

Hermes invokes reconciliation at gateway/engine startup and during the existing
engine tick. There is no new scheduler.

For each pending gist, Hermes:

1. reads and validates the bounded envelope;
2. checks `gist_id` idempotently;
3. appends it to the external vault when missing;
4. runs deterministic lint;
5. verifies the gist is valid and recallable; and
6. removes the outbox file only after verification.

Hermes may safely initialize missing internal directories only after confirming
that the configured external vault root already exists, retry a bounded lock,
and rebuild derived projections. It never recreates a missing external vault
root, because doing so while OneDrive is unavailable could create a false local
vault. It never edits historical gist content to make an invalid entry pass.

Recall incident files close automatically after the next successful health
check. Normal transient failures produce no user notification.

Hermes raises one existing-style `Needs Attention` item only when:

- an outbox entry is corrupt, unsafe, or cannot be deterministically
  classified; or
- the external vault remains unavailable for 24 hours.

The alert contains plain-language status, the autonomous actions already tried,
and one recommendation. It does not reveal gist content. Hermes does not repeat
the alert until the state materially changes. Ole remains the last resort.

## Security and Authority

- The protocol does not authorize direct worker writes to the external vault or
  outbox. Only entries and receipts created through the CLI are accepted; the
  CLI owns validation, redaction, permissions, locking, and atomic writes.
- The same strict text bounds, evidence allowlist, URL/path rules, and secret
  redaction used by current Agent Memory apply before either direct or outbox
  storage.
- Outbox content is never inserted into a model prompt until it has passed the
  normal vault validation path.
- Recall text is always labelled historical evidence and cannot override the
  Work Contract, operating rules, repository instructions, role profile, or
  current evidence.
- Memory success or failure cannot qualify, route, create, move, block,
  complete, merge, release, or break-glass a card.
- The Second Brain remains separate and is never read, indexed, or used as a
  fallback.

## Compatibility and Migration

- Existing valid gists and the deployed external vault remain unchanged.
- The Session Gist parser accepts the current schema while the new structured
  executor metadata is added in a versioned, deterministic form.
- Existing unconfigured-memory behavior remains cleanly disabled.
- Legacy/non-v2 tasks may use the Hermes fallback capture until they acquire a
  stable functional boundary and worker receipts.
- Activation follows the normal fork PR, CI, exact-SHA guarded deployment, and
  config path. Only the default gateway/dashboard consumers restart. Trading
  stays unloaded.

## Testing Strategy

### Module contracts

- Direct recall/write returns valid receipts.
- An unavailable vault returns `continue: true` and creates a valid incident or
  gist envelope in the outbox.
- Initialization and repeated writes are idempotent by operation and gist id.
- Outbox writes are atomic, permission-bounded, redacted, and safe under
  concurrent workers.
- Reconciliation appends, lints, verifies, and removes only confirmed entries.
- Restarting between queue and reconciliation loses no entry.
- Corrupt or unsafe entries remain quarantined and trigger attention without
  exposing content.

### Engine contracts

- Qualification and pre-delegation recall remain advisory.
- Every governed worker prompt contains one bounded protocol envelope.
- Hermes-direct, native child, Codex CLI, Claude Code CLI, and Cowork MCP paths
  produce actual-worker identities and receipts.
- Vault failure does not change worker execution or Kanban routing.
- Missing worker receipts use the normal correction path.
- A worker gist suppresses duplicate Hermes fallback capture.
- Legacy fallback capture remains explicit and functional.

### Live acceptance

Against a disposable board/worktree:

1. prove direct recall and write for Codex CLI;
2. prove direct recall and write for Claude Code CLI;
3. prove direct recall and write through Cowork MCP;
4. make the external vault temporarily unavailable and prove work continues;
5. confirm the worker gist exists in the durable outbox;
6. restore the vault and prove Hermes moves, lints, recalls, and removes it;
7. restart Hermes between steps 5 and 6 and prove recovery still succeeds;
8. confirm no duplicate gist, no unauthorized board transition, no Second Brain
   change, and no Trading service load.

## Acceptance Criteria

The implementation is complete when:

- Hermes recalls before delegation;
- every actual governed worker recalls before its bounded task;
- every actual governed worker submits its own gist after the task;
- receipts identify the real agent, model, surface, role, and execution;
- an unavailable vault never stops work and always leaves durable recoverable
  evidence;
- the outbox itself is the pending flag and drains autonomously;
- Ole is contacted only for unsafe/corrupt entries or a 24-hour outage;
- one task outcome creates one worker gist, not a Hermes/worker duplicate;
- Agent Memory remains non-authoritative and functionality-first; and
- the focused, Kanban union, end-to-end, lint, security, and exact-SHA runtime
  checks pass without loading Trading.
