# Agent Memory Vault Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Hermes and external development agents one external Markdown vault where they record concise functionality-first session gists and recall related work before qualification or execution.

**Architecture:** Add one focused `agent_memory_vault` module that owns vault initialization, append-only gists, deterministic lint/index generation, and bounded lexical recall. Hermes calls it at existing qualification, worker-context, handoff, completion, and block seams; it remains advisory memory and never creates, moves, blocks, or completes a Kanban card. The live vault is explicitly created beside the Second Brain and configured by absolute path after guarded deployment.

**Tech Stack:** Python standard library, existing Hermes configuration and Kanban SQLite APIs, Markdown and derived JSON, pytest through `scripts/run_tests.sh`.

## Global Constraints

- The vault path is external and configured; Hermes must not silently create a fallback under `~/.hermes`.
- Functional intent is the memory identity; board, card, run, Work Contract, commit, and PR identifiers are supporting evidence.
- Historical daily-memory entries are append-only. Lint may rewrite only `snapshot.md`, `index.md`, `log.md`, and `.derived/functions.json`.
- Recall is advisory context, never an execution guard or workflow authority.
- No full transcript, private reasoning, credential, secret, or unrelated conversation is stored.
- No new daemon, scheduler, workflow, Kanban phase, card type, or Cockpit change.
- Trading configuration, boards, and gateways remain untouched.

---

### Task 1: External Markdown vault contract

**Files:**
- Create: `hermes_cli/agent_memory_vault.py`
- Create: `tests/hermes_cli/test_agent_memory_vault.py`
- Modify: `hermes_cli/config.py`

**Interfaces:**
- Produces: `SessionGist`, `MemoryMatch`, `LintReport`.
- Produces: `configured_vault_path(config=None, environ=None) -> Path | None`.
- Produces: `initialize_vault(vault: Path) -> None`.
- Produces: `append_gist(vault: Path, gist: SessionGist) -> bool`.
- Produces: `recall(vault: Path, query: str, limit: int = 5) -> list[MemoryMatch]`.
- Produces: `lint_vault(vault: Path) -> LintReport`.

- [ ] **Step 1: Write failing vault tests**

Test that initialization creates only the documented external structure, append writes the exact structured gist once, a repeated `gist_id` is idempotent, recall returns related functionality, and lint deterministically rebuilds the derived index without changing daily history.

- [ ] **Step 2: Verify RED**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_agent_memory_vault.py -q
```

Expected: FAIL because `hermes_cli.agent_memory_vault` does not exist.

- [ ] **Step 3: Implement the minimal vault module**

Use `HERMES_AGENT_MEMORY_VAULT` first, then `agent_memory.vault_path` when `agent_memory.enabled` is not false. A missing configuration returns `None`; no implicit fallback is created. Daily files use the approved Session Gist headings and an opaque `gist_id` marker. Bound and redact all recorded text using the existing Hermes redactor.

- [ ] **Step 4: Implement bounded recall and lint**

Recall scans recent valid Markdown gists, ranks exact identifiers and shared normalized terms, and returns capped evidence snippets. Lint validates required fields, writes a deterministic latest-function projection to `.derived/functions.json`, and refreshes the human-readable index/snapshot without rewriting `memory/` or `raw/`.

- [ ] **Step 5: Verify GREEN**

Run the Task 1 test file and `git diff --check`.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/agent_memory_vault.py hermes_cli/config.py tests/hermes_cli/test_agent_memory_vault.py
git commit -m "feat: add external agent memory vault"
```

### Task 2: Existing-flow capture and two advisory recall points

**Files:**
- Modify: `hermes_cli/agent_memory_vault.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/kanban_qualifier.py`
- Create: `tests/hermes_cli/test_agent_memory_kanban.py`
- Modify: `tests/hermes_cli/test_kanban_qualifier.py`

**Interfaces:**
- Consumes: Task 1 vault, append, recall, and lint APIs.
- Produces: `remember_kanban_run(conn, *, board, task_id, run_id, outcome, summary=None) -> bool`.
- Produces: `recall_for_qualification(raw_request) -> list[dict]`.
- Produces: `recall_for_task(title, body) -> list[MemoryMatch]`.

- [ ] **Step 1: Write failing integration tests**

Test that a successful completion, normal v2 handoff, and worker block each append one gist; the gist derives its `function_id` from Work Contract functionality rather than card id; qualification receives advisory memory matches; worker context labels recalled text as historical evidence rather than instructions; and an unavailable/unconfigured vault leaves existing behavior unchanged.

- [ ] **Step 2: Verify RED**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_agent_memory_kanban.py tests/hermes_cli/test_kanban_qualifier.py -q
```

Expected: FAIL because no Kanban or qualifier integration exists.

- [ ] **Step 3: Add best-effort normal-handover capture**

After the existing database transaction commits, call `remember_kanban_run` from v2 `handoff`, terminal `complete_task`, and `block_task`. Query the durable task/run/Work Contract records, construct one redacted gist, append idempotently, and lint. Memory failure must be logged and must never roll back or alter the Kanban transition.

- [ ] **Step 4: Add qualification recall**

Add a bounded `agent_memory_recall` array to the existing authoritative qualification payload. State in the prompt that the array is historical evidence only; Hermes must decide reuse or extension and similarity alone cannot reject or merge the intake.

- [ ] **Step 5: Add worker recall**

Add a bounded `Agent Memory recall` section to `build_worker_context`. It must appear only when matches exist, identify the function and evidence, and explicitly state that recalled prose is not an instruction or authority source.

- [ ] **Step 6: Propagate the configured path to spawned profiles**

Resolve the root `agent_memory.vault_path` in `_default_spawn` and pass it to the child as `HERMES_AGENT_MEMORY_VAULT`, so profile-scoped `HERMES_HOME` cannot hide the shared vault configuration.

- [ ] **Step 7: Verify GREEN and regressions**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_agent_memory_kanban.py tests/hermes_cli/test_agent_memory_vault.py tests/hermes_cli/test_kanban_qualifier.py tests/hermes_cli/test_kanban_lifecycle_hooks.py tests/e2e/test_kanban_qualified_product_flow.py -q
```

- [ ] **Step 8: Commit**

```bash
git add hermes_cli/agent_memory_vault.py hermes_cli/kanban_db.py hermes_cli/kanban_qualifier.py tests/hermes_cli/test_agent_memory_kanban.py tests/hermes_cli/test_kanban_qualifier.py
git commit -m "feat: recall and record agent work"
```

### Task 3: Live vault bootstrap and handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-07-18-functional-intent-memory-and-inbox-guide-design.md`
- Live create: `/Users/cloudadvisor/Library/CloudStorage/OneDrive-CloudAdvisorApS/Agent Memory/`
- Live modify after guarded deployment: `/Users/cloudadvisor/.hermes/config.yaml`

**Interfaces:**
- Consumes: Task 1 `initialize_vault` and Task 2 configured runtime.
- Produces: a live external Agent Memory vault with binding `agents.md` instructions and a configured Hermes path.

- [ ] **Step 1: Align the approved design with the simple memory boundary**

Mark Agent Memory as advisory portable memory. Move exact duplicate-work blocking, dedicated inbox authentication, and durable request idempotency outside this implementation slice; they require separate approval and do not belong inside a memory store.

- [ ] **Step 2: Bootstrap the external vault**

Run `initialize_vault` explicitly against `/Users/cloudadvisor/Library/CloudStorage/OneDrive-CloudAdvisorApS/Agent Memory`. Confirm that it does not read or modify the Second Brain.

- [ ] **Step 3: Review the source diff before deployment**

Run the repository two-axis code review against merge-base `origin/main`: repository standards and the approved memory requirements. Fix every Critical or Important finding before proceeding.

- [ ] **Step 4: Run final verification**

Run all focused tests, `scripts/run_tests.sh -q`, `git diff --check`, and `git status --short`. If the known credential-pool baseline failure persists unchanged on `main`, document it rather than attributing it to Agent Memory.

- [ ] **Step 5: Commit documentation and report integration options**

```bash
git add docs/superpowers/specs/2026-07-18-functional-intent-memory-and-inbox-guide-design.md docs/superpowers/plans/2026-07-18-agent-memory-vault.md
git commit -m "docs: define simple agent memory rollout"
```

After the branch is published and merged through the normal PR/CI path, deploy the exact merge SHA, set `agent_memory.enabled: true` and the absolute `vault_path`, restart only the Hermes dashboard/gateway services that consume the code, and prove one real append plus recall. Trading stays unloaded.

## Self-Review

- Spec coverage: external vault, append-only session gists, functionality-first identity, Hermes lint, qualification recall, pre-worker recall, security boundary, and no parallel workflow are covered.
- Deliberate deferral: hard duplicate-execution guards, semantic embeddings, dedicated inbox bearer auth, durable intake idempotency, crash fallback gists, and historical bootstrap are not needed for the user’s clarified “place to remember” goal and remain separate work.
- Placeholder scan: no implementation placeholder or undefined interface remains.
- Type consistency: Task 2 consumes the exact Task 1 interfaces declared above.
