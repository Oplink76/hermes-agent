# Functional Intent Memory and Inbox Guide Design

**Status:** Approved design, pending written-spec review
**Date:** 2026-07-18

## Purpose

External AIs need one discoverable Hermes-owned interface that explains what,
where, and how to submit work to qualified intake. Hermes also needs a durable
cross-project memory that prevents agents from rebuilding functionality that
already exists.

The canonical identity is the intended functionality, not a Kanban card. Cards,
agent runs, sessions, commits, tests, and releases are evidence attached to one
Functional Intent.

## Decisions

1. Create a central Agent Memory vault outside Hermes, beside Ole's Second Brain:
   `/Users/cloudadvisor/Library/CloudStorage/OneDrive-CloudAdvisorApS/Agent Memory`.
2. Use the same human-readable vault structure as the Second Brain. Markdown is
   the source of truth; any machine index is derived and rebuildable.
3. Every Hermes agent/session appends a structured gist through its normal
   handover. Agents cannot edit or delete previous memory.
4. Hermes lints the vault, validates evidence, generates a derived functional
   index, refreshes the snapshot, and promotes durable learning.
5. Functional recall runs twice: during inbox qualification and immediately
   before every worker starts.
6. Exact verified matches are enforced. Similarity is evidence for Hermes to
   decide reuse, extension, or new functionality; similarity alone never
   silently blocks work.
7. Requalification and rework remain normal same-card handovers. The memory does
   not create a parallel workflow, controller, or editable authority marker.

## Language

**Functional Intent**

A stable, cross-card description of the user outcome and functional boundary.

**Session Gist**

One append-only, structured summary written by an agent run through its normal
handover.

**Functional Recall**

A grounded search for identical, overlapping, or related functionality using the
vault's structured index and current repository/board evidence.

**Verified Code Complete**

Development completion recorded by the normal governed handover with concrete
repository evidence. Test, Review, Release, or Done may still be unfinished.

**Derived Index**

A disposable machine-readable projection of the Markdown vault. It is never a
second source of truth and can be rebuilt by Hermes lint.

## Agent Memory Vault

The vault is independent of the Hermes installation and repository. Hermes is a
consumer and curator, not the filesystem owner.

```text
Agent Memory/
├── agents.md
├── snapshot.md
├── index.md
├── log.md
├── memory/
│   └── YYYY-MM-DD.md
├── wiki/
│   ├── functions/
│   └── learnings/
├── raw/
└── .derived/
    └── functions.json
```

- `agents.md` defines the binding append/lint schema.
- `snapshot.md` is capped current context for agents.
- `index.md` is the human-readable catalog.
- `log.md` records curator/lint activity.
- `memory/YYYY-MM-DD.md` contains append-only Session Gists.
- `wiki/functions/` contains one durable page per Functional Intent.
- `wiki/learnings/` contains promoted cross-session behavioral learning.
- `raw/` is immutable evidence and is never rewritten by Hermes.
- `.derived/functions.json` is generated, ignored by human editing, and
  completely rebuildable.

Hermes reads the vault path from configuration. This installation uses the
absolute path above; no code assumes every user has OneDrive or that the vault
lives under `~/.hermes`.

## Functional Intent Record

Each function page has a stable opaque `function_id` plus a readable title and
aliases. Renaming a title does not change its identity.

Required fields:

- `function_id`
- title and aliases
- intended user outcome
- product, project, board, and repository labels
- included and excluded functional scope
- current maturity: `proposed`, `planned`, `in_development`, `code_complete`,
  `tested`, `reviewed`, `released`, or `superseded`
- implementation locations and relevant commits/PRs
- related cards, Work Contracts, sessions, and agents
- current evidence and unresolved gaps
- known overlaps or successor Functional Intents

Cards never become the function's identity. One function can have several
historical cards or sessions, but only one current delivery state.

## Session Gist Schema

Every agent run appends one entry as part of normal complete, block, or handover.
There is no separate optional memory workflow for the agent to remember.

```markdown
## HH:MM | <agent-id> | <role>
- Function: <function_id> | <title>
- Context: board=<slug>; card=<id>; project=<id>; repository=<path-or-url>
- Summary: <1-3 sentences>
- Reused: <existing functionality/evidence, or none>
- Result: <what changed or was learned>
- Maturity: <allowed maturity value>
- Evidence: <commits, PRs, tests, review/release references>
- Behavior: <mistakes, rework, useful agent/process learning, or none>
- Decisions: <decisions, or none>
- Open loops: <remaining work, or none>
```

No private chain-of-thought, full transcript, secret, credential, or unrelated
conversation belongs in Agent Memory. Raw artifacts may be referenced when
needed.

If a worker crashes before handover, Hermes appends a minimal crash gist from the
durable run, event, and heartbeat records. It must be clearly marked incomplete
and cannot promote functional maturity.

## Hermes Memory Lint

Hermes lint is bounded and idempotent. It:

1. validates required headings and fields;
2. verifies board, card, function, repository, and evidence references where
   possible;
3. rejects secrets and forbidden transcript/reasoning content from promotion;
4. detects duplicate or conflicting Functional Intent records;
5. rebuilds `.derived/functions.json`;
6. proposes or applies deterministic snapshot/index refreshes; and
7. promotes repeated, evidenced behavioral learning into `wiki/learnings/`.

Lint never deletes or rewrites historical Session Gists or anything under
`raw/`. Invalid entries remain visible, are marked by lint, and are excluded
from hard execution guards until repaired.

## Public Inbox Guide

Add one public, read-only discovery endpoint:

```http
GET /.well-known/hermes-inbox?board=<board-slug>
```

It returns JSON suitable for an AI or a human. It does not list private boards,
expose tokens, reveal Work Contract signing data, or return task content.

The response includes:

- guide and schema version;
- purpose and authority boundary;
- the requested board and whether qualified intake is required;
- authenticated submission URL and method;
- receipt/status lookup instructions;
- authentication type and required scope, never the credential;
- required request schema;
- idempotency rules;
- functional-memory and duplicate-work behavior;
- forbidden actions, including direct routing, phase, assignment, override, or
  board mutation;
- a copy-ready request example; and
- possible receipt/decision states.

Malformed board slugs return `400`, unknown boards return `404`, and boards that
do not use qualified intake return `409`. The endpoint remains safe to expose to
an unauthenticated internet client.

## Authenticated Inbox

Reuse the official Hermes qualification intake rather than creating another
inbox. External AIs submit through the documented Kanban intake route using a
dedicated inbox-only bearer token. The token grants only:

- submit inert ordinary intake;
- read the caller's receipt/status; and
- no dashboard, board mutation, qualification, routing, requalification,
  override, memory-write, or signing authority.

The token is separate from the dashboard session token. Deployments may disable
external bearer submission while keeping the public guide available.

The guide returns the existing board-scoped routes, resolved against the same
Hermes origin:

```http
POST /api/plugins/kanban/intake?board=<board-slug>
GET /api/plugins/kanban/intake/{intake_id}?board=<board-slug>
```

The first route creates or returns the durable receipt. The second returns only
the receipt/status visible to that authenticated caller.

The request envelope is versioned and requires:

```json
{
  "version": 1,
  "client_request_id": "stable-id-from-the-calling-system",
  "functional_intent": {
    "title": "Readable capability title",
    "desired_outcome": "Measurable user-visible outcome",
    "project": "project identity",
    "repository": "repository identity when known",
    "scope": ["included behavior"],
    "out_of_scope": ["excluded behavior"],
    "aliases": []
  },
  "evidence": [],
  "source": {
    "agent": "claude-code",
    "session_id": "caller-session-id"
  }
}
```

`board + authenticated caller + client_request_id` is a durable idempotency
identity. Retrying it returns the existing intake receipt. That identity must
survive qualification and materialization; it cannot be discarded when a Work
Contract or card is created.

## Stage 1: Inbox Functional Recall

After durable inert intake and before materialization, Hermes searches Agent
Memory using:

- exact `function_id`, stable request identity, issue, commit, PR, or repository
  references;
- product, project, repository, outcome, scope, and aliases; and
- semantic similarity across Functional Intent text and promoted learnings.

Hermes records one grounded qualification result:

**Reuse existing**

The requested outcome already exists or is active. Return the existing
`function_id` and relevant card/receipt. Do not create replacement implementation
work.

**Extend existing**

The request adds a bounded missing behavior. Create only the extension scope and
attach it to the existing `function_id`.

**New function**

No sufficiently grounded match exists. Establish a new Functional Intent and
continue through ordinary qualification.

**Needs Hermes resolution**

Evidence conflicts or similarity is material but ambiguous. Hermes resolves the
classification using its existing qualification role. Ole is asked only if
authority or intended outcome cannot be determined.

Exact identity/evidence matches are deterministic. Similarity alone cannot
silently reject or merge work.

## Stage 2: Pre-Worker Functional Recall

Immediately before every worker claim/spawn, Hermes repeats recall using the
final Work Contract, current function record, current board history, and current
repository evidence. It records a `functional_recall_checked` event with the
function ID, decision, evidence references, and query/index revision.

For Development:

- exact `code_complete`, `tested`, `reviewed`, or `released` evidence prevents a
  new Developer run;
- Hermes returns the same card through ordinary requalification/handover to the
  latest justified unfinished phase;
- partial overlap produces explicit reuse/extension instructions in the worker
  context; and
- an explicit Review `rework_requested` event after the completion evidence may
  reopen Development on the same function and same card.

Test, Review, and Release continue normally after code completion. Code complete
does not mean Done.

If the vault or derived index is temporarily unavailable, Hermes persists the
intake and retries. It fails closed only for starting Development that could
duplicate verified functionality; it does not fabricate a match, mutate the
board, or escalate to Ole before bounded recovery attempts are exhausted.

## Existing Work Bootstrap

The first lint/bootstrap pass builds Functional Intent candidates from existing
Work Contracts, task events, comments, run summaries, commits, PRs, tests, and
release evidence. It does not bulk-edit card status or claim that self-reported
work is verified.

The current code-complete Cockpit cards and older blocked cards with recorded
delivery evidence must therefore become searchable before the safeguard is
enabled. Conflicting or incomplete records remain visible as unresolved memory,
not trusted hard guards.

## Security and Authority

- External callers can read the guide and submit inert intent only.
- Agents append only their own Session Gist through normal handover.
- Agents cannot edit/delete history, promote maturity, alter the derived index,
  qualify work, route cards, or invoke break glass.
- Hermes validates/promotes memory and owns both recall decisions.
- Ole's authenticated Hermes-only break glass remains unchanged.
- The user Second Brain vault is never read or modified by this feature.
- Agent Memory contains no credentials, signing secrets, private reasoning, or
  full transcripts.

## Failure Handling

- Duplicate `client_request_id`: return the original receipt.
- Exact functional match: return reuse/extension disposition; do not create a
  duplicate card.
- Similarity conflict: keep intake inert while Hermes resolves it.
- Invalid memory entry: retain it, lint-mark it, and exclude it from hard guards.
- Missing Agent Memory vault: create no implicit Hermes-local replacement; report
  configuration error and retry safely.
- Crash before Session Gist: append a minimal incomplete crash gist.
- Review requests changes: reopen Development only on the same function/card and
  record the rework relationship.

## Verification

Tests must prove:

1. the public guide is safe without authentication and contains no token, task
   content, signing material, private board listing, or filesystem secret;
2. inbox submission requires the dedicated limited credential;
3. retrying identical intake returns the same receipt and materializes at most
   one card;
4. exact existing functionality reuses its `function_id` and does not create
   implementation work;
5. similar functionality is presented to Hermes but not silently merged;
6. verified code-complete evidence prevents Development at pre-spawn;
7. an explicit Review rework event permits Development on the same card;
8. every complete/block/handover appends one valid Session Gist;
9. worker crashes produce incomplete fallback gists without maturity promotion;
10. lint rebuilds the derived index deterministically and never changes raw or
    historical memory;
11. Agent Memory unavailability follows the documented fail-closed boundary;
12. current qualification, Work Contract, requalification, dispatcher, and
    handover tests remain green; and
13. Trading gateways and boards remain untouched.

## Success Criteria

- Ole can give any external AI one public URL and it can determine exactly how
  to submit governed intent and check its receipt.
- Functionality, not cards, is the durable identity used for recall and learning.
- Retried or already-delivered intent cannot create duplicate implementation
  work through the governed inbox.
- Every agent contributes structured, portable learning without gaining memory
  curation or workflow authority.
- Hermes catches duplicate functionality both before qualification and before
  execution while preserving normal Test, Review, Release, and same-card rework.

## Non-Goals

- no second workflow engine, scheduler, or controller;
- no card-as-function identity;
- no automatic semantic merge without grounded Hermes judgment;
- no full transcript or private reasoning archive;
- no direct external qualification, routing, override, or board mutation;
- no modification of Ole's Second Brain vault; and
- no Trading-board activation.
