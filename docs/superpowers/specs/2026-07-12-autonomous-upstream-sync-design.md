# Autonomous Hermes Fork Sync Design

**Status:** Approved authority boundary; written-spec review pending

**Date:** 2026-07-12

**Owner:** Ole Orum-Petersen

**System:** CloudAdvisor Hermes fork operations

## Purpose

Keep `Oplink76/hermes-agent` close to `NousResearch/hermes-agent:main` without making Ole a routine release operator.

Routine clean upstream changes and safely resolved minor conflicts must flow automatically through candidate creation, verification, protected merge, exact-SHA deployment, health checks, and rollback. Ole is contacted only when automation cannot prove that continuing is safe or authorized.

This restores the July 9 intent that clean sync is automatic. It supersedes the temporary July 10 recovery rule that required Ole's approval for every upstream sync.

## Operating outcome

```text
official upstream/main
→ rolling sync candidate
→ local preservation and verification
→ one protected sync PR
→ required GitHub checks
→ exact-head merge
→ exact-merge-SHA deployment
→ runtime health
→ installed Hermes current
```

There is no routine human approval step in the green path.

## Authority model

### Automatic authority

The sync controller may automatically:

- fetch official upstream and fork refs;
- create or refresh only `auto-sync/upstream`;
- resolve a bounded minor conflict in an isolated worktree;
- run local verification and independent review when required;
- push only the candidate branch with force-with-lease;
- create or update one rolling PR to protected fork `main`;
- wait for required GitHub checks on the exact candidate head;
- merge the exact PR head without bypassing branch protection;
- deploy the exact merge SHA through the constrained deployer;
- verify installed revision, gateway/dashboard identity, database integrity, and preservation state;
- roll back a failed deployment;
- revert a failed sync merge through a protected exact-head revert PR when rollback is healthy; and
- retry only after evidence or the candidate changes.

### Ole-only authority

The controller creates one decision packet and stops when:

- a merge conflict remains unresolved after two materially different strategies;
- a conflict requires a product or policy choice rather than a mechanical reconciliation;
- a conflict touches a protected fork-specific safety boundary and independent review cannot prove semantic preservation;
- required checks remain red after one infrastructure retry and one materially different repair;
- candidate ancestry, upstream provenance, PR head, or merge identity is ambiguous;
- required credentials or external authority are unavailable;
- rollback health fails;
- the automatic revert cannot restore protected fork `main`; or
- continuing would weaken branch protection, evidence requirements, Trading research-only rules, or another explicit safety boundary.

Ole receives `Approve / Wait / Details`, with a recommendation and exact evidence. Routine upstream volume, a clean merge, and a successfully verified minor conflict are not reasons to contact him.

## Sync classification

### Clean sync

A clean sync has no Git conflict and requires no fork-authored resolution commit. It is eligible for automatic merge after local gates and required GitHub checks pass.

### Minor resolved conflict

A minor conflict is one the bounded Resolver can reconcile without changing intended fork behavior, operating authority, data meaning, or a safety invariant. It requires:

- a structured resolution record naming every conflicted file and decision;
- zero unmerged entries or conflict markers;
- prescribed local tests;
- an independent exact-head review by a different backend from the Resolver; and
- all required GitHub checks.

When those gates pass, it is eligible for automatic merge and deployment.

### Major conflict

A conflict is major when resolution requires human product judgment, changes a protected fork-specific invariant, lacks enough evidence to compare behaviors, exceeds the bounded Resolver, or crosses an Ole-only boundary. File location alone does not make a clean upstream change major; the classification concerns an actual semantic conflict or failed safety proof.

Protected fork-specific invariants include:

- fork sync/deploy authority and branch protection;
- exact-SHA evidence and rollback;
- credentials, authentication, and authorization;
- Kanban governance and role separation;
- database preservation/migration correctness;
- Trading research-only and dormant execution boundaries; and
- profile isolation and Second Brain raw-source immutability.

## Candidate and provenance rules

The controller operates only on configured repositories/remotes and the fixed `auto-sync/upstream` branch. Every run records:

- fork base SHA;
- official upstream SHA;
- candidate SHA;
- changed/conflicted files;
- classification and Resolver evidence;
- local check results;
- independent review evidence when required;
- PR number and exact head SHA;
- required GitHub check results;
- merge SHA;
- deployed and rollback SHAs; and
- health/revert results.

The controller rejects stale state before push, merge, deployment, and terminal recording. A candidate refresh invalidates all evidence tied to the previous head.

## Sync-aware contributor attribution

The current contributor check incorrectly treats mirrored official-upstream commits as new fork contributions. The check must exempt a commit only when its exact commit SHA is an ancestor of the fetched configured official `upstream/main`.

Branch name alone is never an exemption. Any fork-authored resolution or compatibility commit that is not in official upstream remains subject to normal contributor-attribution rules. This makes the rolling sync PR pass for legitimate mirrored contributors without weakening ordinary PR checks.

## Verification gates

Before pushing a candidate:

1. Verify a clean source repo and configured isolated worktree.
2. Fetch exact `origin/main`, `upstream/main`, and the previous candidate ref.
3. Preserve the previous candidate/base identities.
4. Verify merge state, common ancestry, zero unmerged entries, and zero conflict markers.
5. Run `git diff --check`, compile checks, focused fork-operations/updater/Kanban tests, and the canonical test wrapper prescribed by risk.
6. For a resolved conflict, run independent exact-head Standards and intent review.

Before merging:

1. Revalidate PR head equals the verified candidate SHA.
2. Revalidate official upstream provenance.
3. Require the configured aggregate GitHub check to be green.
4. Require no unresolved review findings.
5. Merge without admin bypass and record the returned merge SHA.

Before deployment:

1. Verify fork `origin/main` equals the recorded merge SHA.
2. Verify the PR is merged and the candidate is contained/equivalent.
3. Verify preservation receipt and snapshot readiness.
4. Deploy only the exact merge SHA.

## Automatic deployment and recovery

The existing snapshot, lock, service-control, health, and rollback primitives remain the deployment boundary. Sync automation receives a distinct machine authority type accepted only when the complete sync eligibility receipt verifies.

On healthy deployment, record the installed SHA and close the sync cycle.

On deployment failure:

1. Roll back services and state automatically to the previous healthy SHA.
2. Verify rollback health independently.
3. Quarantine the failed upstream/candidate fingerprint so the scheduler cannot repeat it unchanged.
4. Open an exact revert PR for the failed sync merge.
5. Run required checks and merge the exact revert head automatically.
6. Verify fork `main` and installed Hermes converge on the previous healthy lineage.
7. Route the failed candidate to the bounded Resolver.

Rollback-health failure or revert failure stops immediately for Ole. A healthy rollback/revert does not require Ole unless Resolver later exhausts its strategies.

## Scheduler and convergence

The existing 06:00/18:00 job remains, and an operator/manual trigger remains available. Each run first reconciles an existing rolling PR before creating new state.

The run must either:

- report no backlog;
- refresh, verify, merge, deploy, and close the current backlog;
- continue a known in-progress exact-head check/deployment safely; or
- emit one major-conflict decision packet.

It must never report success merely because a PR was updated. Success means fork `main` and the installed runtime reached the verified upstream candidate, or a healthy automatic rollback/revert restored the previous lineage.

## User-facing status

Hermes/Cockpit must distinguish:

- **Official upstream backlog** — commits not yet represented in fork `main`;
- **Sync pending** — candidate/PR/check/review state;
- **Fork update available** — fork `main` is ahead of the installed runtime; and
- **Installed current** — runtime equals fork `main`.

“No updates available” may describe fork-to-install state, but it must not hide a nonzero official-upstream backlog. The normal view remains quiet when all four states are converged.

## Existing PR #7 transition

The implementation first lands the sync-policy/controller change through the current protected fork process under Ole's approval given on 2026-07-12. It then:

1. triggers the sync immediately rather than waiting for the next schedule;
2. refreshes PR #7 to current official upstream;
3. applies the sync-aware contributor check;
4. runs the complete eligibility path against the exact refreshed head; and
5. automatically merges and deploys if it classifies clean or minor-resolved and every gate passes.

If PR #7 is major under this definition, the controller stops with one evidence-backed packet rather than silently retaining the backlog.

## Tests and failure injection

Tests must prove:

- clean upstream backlog auto-merges and deploys;
- no-backlog does nothing;
- candidate force-with-lease rejects stale heads;
- mirrored upstream contributor commits pass only by exact ancestry;
- fork-authored unmapped contributors still fail;
- a minor conflict requires independent review and then auto-merges;
- a major conflict never merges or deploys;
- red/pending/missing required checks never merge;
- changed PR head invalidates approval/evidence;
- exact merge SHA is the only deployable SHA;
- deployment failure rolls back and reverts automatically;
- rollback or revert failure escalates once;
- a quarantined fingerprint cannot loop;
- scheduler overlap is excluded by the existing file lock;
- dashboard status distinguishes upstream, PR, fork, and install state; and
- Trading gateways/writers remain stopped throughout sync/deploy.

At least one real disposable-repository integration test must exercise clean sync through protected merge/deploy fakes, and one controlled recovery canary must prove failed candidate health followed by healthy rollback/revert.

## Acceptance criteria

1. A clean upstream sync reaches fork `main` and the installed Hermes runtime without Ole.
2. A minor resolved conflict reaches the same outcome after independent review without Ole.
3. A major/unresolved conflict creates one concise decision packet and performs no unsafe mutation.
4. Branch protection and required GitHub checks are never bypassed.
5. Only exact official-upstream ancestry receives contributor-attribution exemption.
6. Only the exact merged SHA can deploy.
7. Deployment failure never leaves a known-bad fork update advertised as installable.
8. Healthy rollback/revert restores fork/runtime convergence automatically.
9. Failed rollback/revert stops for Ole.
10. Repeated identical failures cannot loop.
11. PR #7 is refreshed and processed under this policy immediately after bootstrap.
12. Hermes/Cockpit reports upstream backlog separately from installable fork updates.

## Non-goals

- Directly pushing clean syncs to protected `main`.
- Disabling required checks or using admin merge bypass.
- Automatically accepting a semantic conflict that cannot be proven safe.
- Automatically starting Trading gateways, watchdogs, writers, or live execution.
- Combining this focused correction with the separate autonomous development-agent-flow implementation.
- Rewriting official upstream history or squashing away contributor authorship.
