# Functional Intent Memory and Inbox Guide Design

> **Inbox boundary superseded:** The inbox portions of this document are
> superseded by
> [`2026-07-19-work-inbox-delivery-boundary-design.md`](./2026-07-19-work-inbox-delivery-boundary-design.md).
> Agent Memory remains unchanged. The canonical external boundary is the Work
> Inbox; qualification and requalification are downstream Hermes decisions.

**Status:** Inbox guide merged; advisory Agent Memory implemented on an
unmerged branch and not live
**Date:** 2026-07-18

## Purpose

External development AIs need one clear Hermes-owned interface for delivering
work intent. Hermes and its workers also need a small, portable memory of
functionality already handled so they can notice related work before starting.

This slice keeps those concerns simple:

1. the public inbox guide explains how external AIs submit governed intent;
2. Agent Memory records and recalls concise historical evidence; and
3. Hermes' existing qualification, Work Contract, Kanban, and handover flow
   remains the only workflow authority.

Functionality is the memory identity. Cards, runs, commits, PRs, tests, and
releases are evidence about that functionality, not its identity.

## 1. Public Inbox Guide: Merged

The public, read-only guide is merged into the Hermes fork at commit
`232338d2b0a072b7edbab8514eb544f67fd419bc`:

```http
GET /.well-known/hermes-inbox?board=<board-slug>
```

It returns a board-specific JSON guide with:

- the allowed and forbidden authority boundary;
- the existing authenticated intake and receipt routes;
- the request example and receipt states; and
- a copy-ready prompt an external AI can follow.

The guide does not expose credentials, task content, signing data, private
board listings, or filesystem paths. Missing, malformed, unknown, and
non-qualified boards are rejected.

The canonical endpoint contract and verification are documented in the
[Hermes Inbox Guide implementation plan](../plans/2026-07-18-hermes-inbox-guide.md).
The running installation only exposes this endpoint after a build containing
the merge commit is deployed.

This slice does **not** add a dedicated inbox bearer token or new durable
external-request idempotency. The guide accurately describes the existing
authenticated Hermes API context; it does not claim those deferred safeguards
already exist.

## 2. Advisory Agent Memory: Implemented on This Branch

Agent Memory is one external Markdown vault. It is a historical evidence store,
not a controller, queue, workflow gate, or second source of Kanban truth.

### External vault

The bootstrapped external vault intended for this installation is:

`/Users/cloudadvisor/Library/CloudStorage/OneDrive-CloudAdvisorApS/Agent Memory`

The directory exists, but it is not configured or live. After the branch is
merged and the exact merge SHA is deployed, an operator enables it through the
root Hermes `config.yaml`:

```yaml
agent_memory:
  enabled: true
  vault_path: /Users/cloudadvisor/Library/CloudStorage/OneDrive-CloudAdvisorApS/Agent Memory
```

Missing configuration disables memory cleanly. Hermes never creates a fallback
under `~/.hermes`. `HERMES_AGENT_MEMORY_VAULT` is an internal propagation
bridge: the root Hermes process resolves `agent_memory.vault_path` and passes
that path to profile-scoped workers so they share the same vault. It is not the
operator-facing configuration contract.

The initialized structure is:

```text
Agent Memory/
├── agents.md
├── snapshot.md
├── index.md
├── log.md
├── memory/
├── wiki/
│   ├── functions/
│   └── learnings/
├── raw/
└── .derived/
    ├── agent-memory.lock
    └── functions.json
```

`agents.md` is the binding Session Gist schema. Markdown under `memory/` is the
append-only history. `snapshot.md`, `index.md`, `log.md`, and
`.derived/functions.json` are deterministic projections that lint can rebuild.
The empty `wiki/` and `raw/` directories reserve the human-readable layout;
promotion into them is not implemented in this slice.

### Session Gists

Hermes appends one bounded Session Gist after a durable v2 handoff, completion,
or block transition. The entry uses the schema written to `agents.md`:

```markdown
## HH:MM | <agent-id> | <role>
<!-- gist_id: <opaque-id> -->
- Function: <function_id> | <title>
- Context: <board/card/project/repository evidence>
- Summary: <concise governed transition summary>
- Reused: <existing functionality/evidence, or none>
- Result: <what changed or was learned>
- Maturity: <planned/in_development/code_complete/tested/reviewed/released>
- Evidence: <allowed repository/commit/PR/test/review/release identifiers>
- Behavior: <learning, or none>
- Decisions: <decisions, or none>
- Open loops: <remaining work, or none>
```

The gist identity includes the immutable transition event, so retrying capture
does not append the same transition twice. Capture waits for the outer database
commit; a rolled-back transition leaves no gist.

Hermes generates the stored summary and accepts only a strict allowlist of
evidence identifiers. Values are bounded and redacted. Worker transcripts,
private reasoning, credentials, secrets, arbitrary run metadata, event payloads,
and unrelated conversation are not stored.

### Functionality-first identity

For v2 work, `function_id` is derived deterministically from the Work Contract's
stable functional boundary: work kind/type, desired outcome, scope, and
out-of-scope. The readable title is not part of that identity.

When no Work Contract exists, Hermes may use an immutable task idempotency key.
Legacy work with only a mutable title or body has no safe functional identity,
so memory capture is skipped with a warning. It is safer to remember nothing
than to create a false identity.

### Two advisory recall points

Recall is bounded lexical matching over recent valid gists. It runs:

1. during qualification, where matches are included as historical evidence;
2. before worker execution, where matches are labelled as historical notes.

Recalled text is never an instruction, authority source, qualification result,
duplicate decision, or execution guard. Hermes and workers must verify it
against the current Work Contract, board, and repository before deciding to
reuse or extend functionality.

### Lint and failure boundary

Lint validates the required gist shape and deterministically rebuilds the
derived function index, snapshot, index, and log. Vault mutation is serialized
with a bounded cross-process lock, and projections are atomically replaced.
Historical gist files are not rewritten by lint.

Memory is best effort. An unconfigured, unavailable, invalid, or temporarily
locked vault can produce a warning or no match, but cannot create, move, block,
complete, roll back, or otherwise alter a Kanban card or run.

### Live-state boundary

The external directory can be initialized before deployment, but Agent Memory
is **not live** until all three conditions are true:

1. this branch is merged through the normal PR and CI path;
2. the exact resulting merge SHA is deployed to the Hermes installation; and
3. Hermes configuration explicitly enables Agent Memory and points at the
   external path above.

Initializing the vault alone does not activate capture or recall. No live
Hermes configuration is changed by this implementation task.

## 3. Deferred Safeguards and Learning

The following are intentionally not implemented in this slice:

- hard duplicate rejection or blocking worker execution;
- semantic or embedding-based similarity search;
- a dedicated inbox bearer-auth scope;
- durable idempotency for external inbox requests;
- crash fallback gists;
- historical bootstrap from existing boards, runs, commits, or PRs;
- generated Functional Intent wiki pages or function-wiki promotion;
- behavior-learning validation or promotion;
- a memory scheduler, daemon, workflow, or controller; and
- memory-driven card creation, routing, qualification, requalification, or
  break-glass override.

These may be designed separately if evidence shows they are needed. They must
not be inferred from the presence of the vault, its empty directories, the
public inbox guide, or recalled historical text.

## Security and Authority

- External AIs follow the inbox guide and submit intent through Hermes; they do
  not orchestrate directly on a project board.
- Hermes' existing engine, Work Contracts, roles, handovers, and operating
  rules remain authoritative.
- Agent Memory contains generated, bounded evidence only and grants no
  authority to its writers or readers.
- The adjacent Second Brain is a separate user vault and is never read,
  modified, indexed, or used as an Agent Memory fallback.
- Trading, Cockpit, and every Kanban board are outside this slice.

## Verification and Acceptance

This slice is complete when:

- the public inbox-guide behavior remains covered by its existing tests;
- the external vault API tests prove explicit configuration, append-only
  deduplication, redaction, bounded recall, deterministic lint, concurrency,
  and idempotent initialization;
- Kanban integration tests prove post-commit capture, rollback isolation,
  functionality-first identity, advisory recall, and best-effort failure;
- the external vault exists with the module-generated `agents.md` and no
  fabricated historical gists;
- running initialization twice leaves the initialized content unchanged;
- the Second Brain, live Hermes configuration, boards, Cockpit, and Trading are
  untouched; and
- deployment is reported separately from merge and vault initialization.
