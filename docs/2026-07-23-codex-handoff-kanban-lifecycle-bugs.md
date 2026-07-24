# Codex handoff — two Hermes v2 kanban lifecycle/worker bugs

> **Archival snapshot (2026-07-23):** Preserved as incident evidence. Card states, installed skills, and implementation hypotheses below are historical and must be revalidated before use; this document is not an active work authorization.

**Date:** 2026-07-23
**Repo to fix in:** `~/.hermes/hermes-agent` (this repo). These are **framework/engine** bugs, not project bugs — they surfaced on the `agentic-os-cockpit` board but the fault is in Hermes itself.
**Author:** Claude (Ole-directed). External analysis — verify independently before changing code.
**Why here:** separated out of the `agentic-os-cockpit` project per Ole; handoffs live in the repo they're about.

Both bugs stall *completed* work on the board — the cards' actual work is fine; the engine's worker/finalize plumbing is what fails.

---

## Bug 1 — Reviewer worker crashes on a missing `sdlc-review` skill, tripping the failure-breaker

**Symptom:** Board `agentic-os-cockpit`, card `t_c91e1eaf` is **blocked** (failure-breaker). The reviewer worker crashed twice at the Review step and the dispatcher gave up at its 2-failure limit.

**Grounded evidence:**
```
crashed reviewer runs (task_runs):
  378  reviewer  review  crashed  pid 63217 exited with code 1
  374  reviewer  review  crashed  pid 54690 exited with code 1
gave_up event:
  {"failures":2,"effective_limit":2,"limit_source":"dispatcher","error":"pid 63217 exited with code 1","trigger_outcome":"crashed"}
worker log tail (~/.hermes/kanban/boards/agentic-os-cockpit/logs/t_c91e1eaf.log):
  Warning: Unknown toolsets: messaging
  Error: Unknown skill(s): sdlc-review
```

**Root cause (hypothesis to confirm):** the reviewer profile is launched requesting a skill `sdlc-review` (and a toolset `messaging`) that is not installed/registered in this environment. The worker CLI treats an unknown required skill as **fatal → non-zero exit (code 1)**, which the dispatcher counts as a crash; two consecutive crashes trip the failure-breaker and block the card. Note the same `Error: Unknown skill(s): sdlc-review` line appears at the end of *completed* worker sessions too, so unknown-skill validation is exiting non-zero even when the actual work ran.

**Impact:** any Review-phase card whose reviewer profile requires `sdlc-review` will crash-loop into a block. Broader risk: an unknown skill/toolset should not be able to fail an otherwise-successful worker run.

**Suggested fix (advisory):** one or more of —
1. Register/install the `sdlc-review` skill and the `messaging` toolset the profile expects; or
2. Remove `sdlc-review`/`messaging` from the reviewer profile's required set if they're stale; and
3. Make an unknown skill/toolset a **non-fatal warning** (do not exit non-zero) so a validation gap can't fail a completed run. Look at the worker skill/toolset resolution + exit-code path.

---

## Bug 2 — `kanban_complete` reports "unknown id or already terminal" for a card that is still running, stalling finalize

**Symptom:** Board `agentic-os-cockpit`, card `t_459b04df` (currently `blocked / development / needs_input`). The developer finished and verified the implementation, but could not finalize the Development handoff: `kanban_complete` rejected it **twice** while the same card reports as running.

**Grounded evidence:**
```
task state now: blocked | development | assignee=default | block_kind=needs_input
blocked event: {"reason":"workflow-finalize: Development implementation and verification are complete and posted in comments 418/419, but kanban_complete twice returned `unknown id or already te…"}
developer comment:
  kanban_show reports the card as running, current_step_key=development, current_run_id=428, yet:
   1. kanban_complete(task_id="t_459b04df", ...) -> "could not complete t_459b04df (unknown id or already terminal)"
   2. kanban_complete(...) with default task id -> "could not complete t_459b04df (unknown id or already terminal)"
```
The framework's own `human_input_preflight` escalated this with `fault_domain: framework`.

**Root cause (hypothesis to confirm):** a lifecycle/routing mismatch in the finalize path — `kanban_complete` cannot find/complete a task that `kanban_show` simultaneously reports as `running` at `development` with an active `current_run_id=428`. Likely a run_id / step_key / claim-state disagreement between the completion lookup and the live task row (possibly the completion path keys off a run or state that no longer matches, or a terminal-check false-positives).

**Impact:** a development-complete card cannot advance; it dead-ends at `needs_input` despite the work being done. This is **currently blocking the shipped fix for the Paperclip Work-board 502** (card `t_459b04df`) from landing.

**Suggested fix (advisory):** trace `kanban_complete`'s task/run resolution and the "unknown id or already terminal" branch against a card in `running/development` with a live `current_run_id`. Reconcile the completion lookup with the authoritative task row so a running card can finalize its handoff. Add a regression test: complete a card that is `running` at a non-first phase with an active run.

---

## For the fixer
- Reproduce against the live board DB read-only first: `~/.hermes/kanban/boards/agentic-os-cockpit/kanban.db` (cards `t_c91e1eaf`, `t_459b04df`).
- Relevant code areas: worker skill/toolset resolution + exit handling (Bug 1); `kanban_complete` / lifecycle finalize + routing (`hermes_cli/kanban_db.py`, kanban lifecycle) (Bug 2).
- Do **not** hand-mutate the two affected cards to "unstick" them — fix the engine so they finalize/route correctly, then let them progress.
