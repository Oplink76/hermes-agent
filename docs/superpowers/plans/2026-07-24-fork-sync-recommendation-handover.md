# Fork Sync Recommendation Handover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every Hermes fork-evaluation ADOPT or CONVERGE recommendation as a simple durable Markdown handover for later authorized work.

**Architecture:** Add one operating rule and inline template to the existing CloudAdvisor operations guide. Create one plain Markdown record for the latest evaluation under the existing recovery root; no runtime component reads or writes it automatically.

**Tech Stack:** Markdown, Git, `rg`

## Global Constraints

- Add no runtime code, database, JSON, schema, digest, index, outbox, or automatic task creation.
- One Markdown file represents one completed fork evaluation.
- Recommendation records are advisory and grant no execution or Kanban authority.
- A future evaluation must reassess older open items instead of duplicating them.
- Do not change or stage the existing user-owned edits in `ops/cloudadvisor/hermes_ops/runtime.py`, `tests/cloudadvisor_ops/test_runtime.py`, or the two untracked July 23 handover documents.

---

### Task 1: Document the rule and create the first handover

**Files:**

- Modify: `ops/cloudadvisor/OPERATIONS.md:128`
- Create: `/Users/cloudadvisor/.hermes/recovery/fork-sync-recommendations/2026-07-24-46c7a4076.md`

**Interfaces:**

- Consumes: The two-report fork evaluation performed against fork `7dd5f5b1d89cbc521520e9f5399933a2791e45ba` and upstream `46c7a4076fc543bdc98de12b81c2c85ef9c864b9`.
- Produces: A human- and AI-readable Markdown convention plus seven durable open recommendations. No programmatic interface is introduced.

- [ ] **Step 1: Add the operating rule and inline template**

Insert this section immediately before `## Approval artifact` in
`ops/cloudadvisor/OPERATIONS.md`:

````markdown
## Fork evaluation recommendation handover

Each fork evaluation produces an executive change summary and a Fork Impact
Evaluation. If either report recommends **ADOPT** or **CONVERGE**, write one
plain Markdown handover for that evaluation under:

```text
~/.hermes/recovery/fork-sync-recommendations/
```

Use `YYYY-MM-DD-<upstream-short-sha>.md` as the filename. Before writing it,
search older files for open recommendations and reassess those items instead of
duplicating them. End the two-report delivery with the new record's path.

Use this shape:

```text
# Hermes fork sync recommendations

- Evaluation date:
- Fork main:
- Official upstream main:
- Merge SHA: not yet synced | <full SHA>
- Reports:

## FSR-<upstream-short-sha>-NN — <short outcome>

- Action: ADOPT | CONVERGE
- Status: open | completed | declined | superseded
- Outcome:
- Why:
- Upstream evidence:
- Preserve:
- Suggested next stage: design | plan | implementation
- Disposition:
```

The record is advisory. It does not create work, mutate Work Inbox or Kanban,
or authorize design, planning, implementation, sync, deployment, or lifecycle
changes. Add a dated disposition when an item is completed, declined, or
superseded. No handover is required when the evaluation contains no **ADOPT**
or **CONVERGE** recommendation.
````

- [ ] **Step 2: Create the recovery directory**

Run:

```bash
mkdir -p /Users/cloudadvisor/.hermes/recovery/fork-sync-recommendations
```

Expected: exit status `0`; existing recovery content remains untouched.

- [ ] **Step 3: Create the latest evaluation's recommendation record**

Create
`/Users/cloudadvisor/.hermes/recovery/fork-sync-recommendations/2026-07-24-46c7a4076.md`
with exactly this content:

```markdown
# Hermes fork sync recommendations

- Evaluation date: 2026-07-24
- Fork main: `7dd5f5b1d89cbc521520e9f5399933a2791e45ba`
- Official upstream main: `46c7a4076fc543bdc98de12b81c2c85ef9c864b9`
- Upstream backlog at evaluation: 738 commits
- Merge SHA: not yet synced
- Reports: Executive Summary and Fork Impact Evaluation delivered in the originating Codex task

These records are advisory. They create no task, grant no execution authority,
and do not authorize sync, deployment, or Kanban mutation.

## FSR-46c7a4076-01 — Isolate Kanban children and worktrees

- Action: ADOPT
- Status: open
- Outcome: Bring upstream child-task and worktree isolation improvements into the fork's governed Kanban implementation.
- Why: Upstream fixes prevent delegated children or decomposition siblings from sharing mutable workspaces or crossing parent boundaries.
- Upstream evidence: commits `a7dcf9787`, `6833eabb5`, `b9b5481d6`, and `65d42e35d`; files `hermes_cli/kanban_db.py`, `hermes_cli/kanban.py`, and `tools/kanban_tools.py`.
- Preserve: Work Inbox/v2 authority, Work Contracts, Agent Memory semantics, canonical worktree binding, Review/worktree/error repairs, and the rule that external agents do not mutate board lifecycle state.
- Suggested next stage: design
- Disposition: none

## FSR-46c7a4076-02 — Converge SessionDB WAL and search protection

- Action: CONVERGE
- Status: open
- Outcome: Integrate upstream WAL-safety, indexing, and archived-session search improvements with the fork's SessionDB behavior.
- Why: These changes reduce database corruption and lost-search risks, but overlap the fork's isolation and rollback-sensitive state handling.
- Upstream evidence: commits `953cbc030`, `9acc4b47f`, `98c0d8b29`, and `711f1c2f1`; files `hermes_state.py` and `tools/session_search_tool.py`.
- Preserve: Per-profile SessionDB isolation, live mutable-state preservation, rescue-before-restore recovery, and snapshot-generation matching.
- Suggested next stage: plan
- Disposition: none

## FSR-46c7a4076-03 — Adopt Slack security hardening

- Action: ADOPT
- Status: open
- Outcome: Bring upstream Slack SSRF, DNS-pinning, authorization-order, token-permission, workspace-isolation, and prompt-injection protections into the fork.
- Why: These are concrete trust-boundary fixes and should replace weaker behavior where they do not conflict with local routing.
- Upstream evidence: commits `2e08b778a`, `42626da1c`, `8e4b5d877`, `5bb933eed`, `a60b00e12`, and `1d7db1ba1`; files under `plugins/platforms/slack/` and matching Slack gateway tests.
- Preserve: Existing configured Slack workspaces, direct sync-alert delivery, bounded retry behavior, privacy-safe logs, and channel/session routing.
- Suggested next stage: implementation
- Disposition: none

## FSR-46c7a4076-04 — Adopt interrupted-update recovery

- Action: ADOPT
- Status: open
- Outcome: Integrate upstream updater marker and Windows self-lock recovery fixes into the fork's guarded update paths.
- Why: The fixes prevent probes from falsely clearing recovery markers and improve recovery when the running executable locks itself.
- Upstream evidence: commits `40fd2b8c0` and `8aa2a8bbc`; files `hermes_cli/subcommands/update.py`, desktop updater files, and matching updater recovery tests.
- Preserve: Exact-fork-SHA update semantics, update suppression during sync attention or rollback recovery, mutable-state preservation, and the current decision not to resume automatic repository sync.
- Suggested next stage: implementation
- Disposition: none

## FSR-46c7a4076-05 — Converge model and provider routing

- Action: CONVERGE
- Status: open
- Outcome: Reconcile upstream route-owned provider settings, model visibility, and per-task model/provider selection with the fork's planned controller/worker routing.
- Why: Upstream now covers parts of the fork's routing needs, but wholesale replacement could weaken profile authority or change model selection during a run.
- Upstream evidence: commits `63dd651b3`, `c1b0f6f3c`, `df051c17c`, and `3ea35d671`; files `hermes_cli/model_switch.py`, `hermes_cli/providers.py`, `hermes_cli/runtime_provider.py`, and `agent/model_metadata.py`.
- Preserve: Profile authority, frozen routing per run, explicit controller/executor roles, and opposite-backend review.
- Suggested next stage: design
- Disposition: none

## FSR-46c7a4076-06 — Converge durable delegation delivery

- Action: CONVERGE
- Status: open
- Outcome: Integrate upstream durable origin identity, honest acknowledgement, API-session wake-up, and worker-result retention with Agent Memory delegation.
- Why: These changes close delivery and restart gaps without making delegation records authoritative.
- Upstream evidence: commits `f50c3d904`, `b1201213b`, `246eacea7`, and `dc3e4e842`; files `tools/async_delegation.py`, `tools/delegate_tool.py`, `agent/delegation_context.py`, and `gateway/session.py`.
- Preserve: Agent Memory remains advisory, Work Contract ownership remains with Hermes, no synthetic mid-loop user nudge is introduced, and acknowledgements must reflect actual durable delivery.
- Suggested next stage: design
- Disposition: none

## FSR-46c7a4076-07 — Converge compression and MoA improvements

- Action: CONVERGE
- Status: open
- Outcome: Selectively integrate upstream compression safety, recent-user-tail preservation, MoA provider context, and progress/reference handling.
- Why: Upstream has substantial correctness and usability improvements, but some context changes overlap the fork's stricter prompt-cache and message-alternation contracts.
- Upstream evidence: commits `0acdf1d8c`, `44c67fca9`, `a9c868225`, `78312c192`, `ad6a2ae40`, and `43be8d1dd`; files `agent/context_compressor.py`, `agent/conversation_compression.py`, and the desktop MoA event handlers.
- Preserve: Byte-stable per-conversation prompt caching, strict role alternation, protected recent user intent, and current fork context semantics. Do not adopt upstream per-turn `select_context()` replacement as-is.
- Suggested next stage: design
- Disposition: none
```

- [ ] **Step 4: Verify the record shape and authority boundary**

Run:

```bash
record=/Users/cloudadvisor/.hermes/recovery/fork-sync-recommendations/2026-07-24-46c7a4076.md
test -f "$record"
test "$(rg -c '^## FSR-46c7a4076-' "$record")" -eq 7
test "$(rg -c '^- Action: (ADOPT|CONVERGE)$' "$record")" -eq 7
test "$(rg -c '^- Status: open$' "$record")" -eq 7
rg -n 'creates no task|grant no execution authority|do not authorize' "$record"
rg -n 'does not create work|mutate Work Inbox or Kanban|No handover is required' ops/cloudadvisor/OPERATIONS.md
git diff --check -- ops/cloudadvisor/OPERATIONS.md
```

Expected: every `test` exits `0`; both `rg` commands show the advisory
boundaries; `git diff --check` prints nothing.

- [ ] **Step 5: Confirm scope isolation**

Run:

```bash
git status --short
test "$(git diff -- ops/cloudadvisor/hermes_ops/runtime.py | shasum -a 256 | cut -d' ' -f1)" = \
  "943f2c0b40f56284388b908891be4067e3368325f23fa55f20c01a09092b4e19"
test "$(git diff -- tests/cloudadvisor_ops/test_runtime.py | shasum -a 256 | cut -d' ' -f1)" = \
  "493ab20abbc1143d8754ed1e7c80a2c73d4da444d477cc233e435dd990c19994"
test -f docs/2026-07-23-codex-handoff-kanban-lifecycle-bugs.md
test -f docs/2026-07-23-infrastructure-handover-work-inbox-intake.md
unexpected="$(
  git diff --name-only |
    rg -v '^(ops/cloudadvisor/OPERATIONS.md|ops/cloudadvisor/hermes_ops/runtime.py|tests/cloudadvisor_ops/test_runtime.py)$' ||
    true
)"
test -z "$unexpected"
```

Expected: every `test` exits `0`. The only new tracked implementation change is
`ops/cloudadvisor/OPERATIONS.md`; the pre-existing user-owned modifications
retain their exact diff hashes, and both untracked files remain present.

- [ ] **Step 6: Commit the repository-owned operating rule**

Run:

```bash
git add -- ops/cloudadvisor/OPERATIONS.md
git diff --cached --check
git commit -m "docs(ops): retain fork sync recommendations"
```

Expected: one commit containing only `ops/cloudadvisor/OPERATIONS.md`. The
recovery record remains operator-owned outside Git.
