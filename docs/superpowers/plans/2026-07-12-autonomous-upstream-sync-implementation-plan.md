# Autonomous Hermes Fork Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make clean and independently verified minor-conflict upstream syncs automatically merge through protected fork PRs, deploy the exact merge SHA, recover automatically, and escalate to Ole only when safe resolution is exhausted.

**Architecture:** Keep `sync.py` as the isolated candidate builder and add a narrow controller that owns exact-head GitHub reconciliation, machine-verifiable sync authority, deployment, and recovery under the existing operations lock. Preserve the human approval deploy path unchanged; automated deployment receives a separate receipt type whose ancestry, checks, classification, and merge identity are revalidated. A durable status receipt lets Hermes distinguish official-upstream backlog from fork-to-install updates.

**Tech Stack:** Python 3.11+, dataclasses/protocols, Git/`gh` through injected command runners, GitHub Actions YAML, existing CloudAdvisor deploy/snapshot/runtime primitives, React/TypeScript, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Never push directly to protected fork `main` and never use GitHub admin bypass.
- Automatic merge is allowed only for `clean` or independently reviewed `minor_resolved` candidates.
- A candidate refresh invalidates evidence tied to the prior head.
- Contributor exemption requires exact commit ancestry in fetched official `upstream/main`; branch name is never sufficient.
- Human `ApprovalRecord` behavior remains unchanged.
- Automated deployment accepts only a complete immutable `SyncEligibilityReceipt` for the exact merged SHA.
- Failed candidate health must roll back; healthy rollback must converge fork main through a protected revert PR before retry.
- Rollback or revert failure stops for Ole.
- Trading gateways, watchdog, pulse, and repository writers remain outside every configured service action.
- Use `scripts/run_tests.sh`; never invoke `pytest` directly.
- Land the controller bootstrap first, then trigger PR #7 immediately; status UI work must not delay backlog convergence.

---

### Task 1: Make contributor attribution exact-upstream-aware

**Files:**

- Create: `scripts/check_contributor_attribution.py`
- Modify: `.github/workflows/contributor-check.yml`
- Create: `tests/scripts/test_check_contributor_attribution.py`

**Interfaces:**

- Consumes: repository path, base ref, head ref, optional official upstream ref.
- Produces: `AttributionResult` and exit `0` only when every non-exempt author is mapped.

- [ ] **Step 1: Write failing exact-ancestry tests**

```python
def test_exact_upstream_commit_is_exempt(tmp_path):
    repo, base, upstream_commit, head = make_sync_history(tmp_path)
    result = check_contributors(repo, base=base, head=head, upstream="upstream/main")
    assert result.ok is True
    assert result.exempt_upstream_commits == (upstream_commit,)


def test_branch_name_does_not_exempt_fork_commit(tmp_path):
    repo, base, _upstream_commit, _head = make_sync_history(tmp_path)
    fork_commit = commit_as(repo, "fork change", email="unknown@example.com")
    result = check_contributors(repo, base=base, head=fork_commit, upstream="upstream/main")
    assert result.ok is False
    assert result.missing[0].email == "unknown@example.com"
```

Also cover missing upstream ref, merge commits, mapped authors, noreply authors, and a commit not reachable from official upstream.

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/scripts/test_check_contributor_attribution.py -q
```

Expected: import failure for `scripts.check_contributor_attribution`.

- [ ] **Step 3: Implement the checker**

```python
@dataclass(frozen=True)
class MissingContributor:
    commit: str
    email: str
    author: str


@dataclass(frozen=True)
class AttributionResult:
    ok: bool
    exempt_upstream_commits: tuple[str, ...]
    missing: tuple[MissingContributor, ...]


def check_contributors(
    repo: Path, *, base: str, head: str, upstream: str | None
) -> AttributionResult:
    """Exempt only commits for which merge-base --is-ancestor COMMIT UPSTREAM succeeds."""
```

Import the existing `AUTHOR_MAP` value from `scripts/release.py`; do not parse source with grep. Enumerate non-merge commits in `base..head` and test each SHA individually.

- [ ] **Step 4: Replace the workflow shell**

```yaml
- name: Fetch official upstream main
  run: git fetch --no-tags https://github.com/NousResearch/hermes-agent.git main:refs/remotes/upstream/main
- name: Check contributor attribution
  run: python scripts/check_contributor_attribution.py --base origin/main --head HEAD --upstream upstream/main
```

- [ ] **Step 5: Run GREEN and commit**

```bash
scripts/run_tests.sh tests/scripts/test_check_contributor_attribution.py -q
git diff --check
git add scripts/check_contributor_attribution.py .github/workflows/contributor-check.yml tests/scripts/test_check_contributor_attribution.py
git commit -m "fix(ci): trust exact mirrored upstream authors"
```

---

### Task 2: Add exact-head GitHub sync operations

**Files:**

- Create: `ops/cloudadvisor/hermes_ops/sync_github.py`
- Modify: `ops/cloudadvisor/hermes_ops/cli.py`
- Create: `tests/cloudadvisor_ops/test_sync_github.py`
- Modify: `tests/cloudadvisor_ops/test_cli.py`

**Interfaces:**

- Consumes: repo slug, PR number, expected head/base SHA, required check name.
- Produces: `SyncPullRequestEvidence` and `merge_exact(pr_number, expected_head) -> str`.

- [ ] **Step 1: Write failing strict `gh` tests**

```python
def test_merge_exact_rejects_changed_head(tmp_path):
    github = github_with_pr(tmp_path, head="different")
    with pytest.raises(SyncGitHubError, match="head changed"):
        github.merge_exact(7, expected_head="candidate")


def test_green_exact_head_merges_without_admin(tmp_path):
    github, runner = green_github(tmp_path, head="candidate")
    assert github.merge_exact(7, expected_head="candidate") == "merge-sha"
    argv = tuple(item for call in runner.calls for item in call.argv)
    assert "--admin" not in argv
    assert "--match-head-commit" in argv
```

Cover pending/red/missing aggregate checks, closed PR, duplicate PR, stale base, and missing merge SHA.

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_github.py tests/cloudadvisor_ops/test_cli.py -q
```

- [ ] **Step 3: Implement the narrow port**

```python
@dataclass(frozen=True)
class SyncPullRequestEvidence:
    number: int
    state: str
    base_sha: str
    head_sha: str
    required_check: str
    required_check_conclusion: str
    merge_sha: str | None = None


class SyncGitHubPort(Protocol):
    def evidence(self, pr_number: int) -> SyncPullRequestEvidence: ...
    def merge_exact(self, pr_number: int, *, expected_head: str) -> str: ...
```

`GhSyncGitHub` may call only normalized `gh pr list/create/edit/view/checks/merge` commands. Merge uses `--merge --match-head-commit` and never `--admin`.

- [ ] **Step 4: Preserve create/update compatibility**

Move current `GhGitHub` behavior behind `GhSyncGitHub`, retaining `find_open_pull_request`, `create_pull_request`, and `update_pull_request` for `sync.py`.

- [ ] **Step 5: Run GREEN and commit**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_github.py tests/cloudadvisor_ops/test_cli.py -q
git add ops/cloudadvisor/hermes_ops/sync_github.py ops/cloudadvisor/hermes_ops/cli.py tests/cloudadvisor_ops/test_sync_github.py tests/cloudadvisor_ops/test_cli.py
git commit -m "feat(ops): merge exact green sync heads"
```

---

### Task 3: Classify candidates and require independent conflict review

**Files:**

- Modify: `ops/cloudadvisor/hermes_ops/sync.py`
- Modify: `tests/cloudadvisor_ops/test_sync.py`
- Create: `ops/cloudadvisor/hermes_ops/sync_review.py`
- Create: `tests/cloudadvisor_ops/test_sync_review.py`

**Interfaces:**

- Produces: `prepare_candidate(...) -> SyncResult`, `SyncClassification`, and `ConflictReviewReceipt`.

- [ ] **Step 1: Write failing classification tests**

```python
def test_clean_merge_classifies_clean():
    result = prepare_candidate(config, runner=clean_runner, github=github)
    assert result.classification is SyncClassification.CLEAN


def test_resolved_conflict_requires_review():
    result = prepare_candidate(
        config, runner=conflict_runner, github=github, resolver=resolver
    )
    assert result.classification is SyncClassification.MINOR_REVIEW_REQUIRED
```

Prove existing `run()` still owns the file lock and `prepare_candidate()` does not.

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync.py tests/cloudadvisor_ops/test_sync_review.py -q
```

- [ ] **Step 3: Refactor the candidate builder**

```python
class SyncClassification(str, Enum):
    CLEAN = "clean"
    MINOR_REVIEW_REQUIRED = "minor_review_required"
    MINOR_RESOLVED = "minor_resolved"
    MAJOR = "major"


def prepare_candidate(
    config: SyncConfig,
    *,
    github: GitHubPullRequests,
    runner: CommandRunner,
    resolver: ConflictResolver | None = None,
) -> SyncResult:
    """Prepare/push the rolling PR; caller owns config.lock_path."""
```

Keep `run()` as a lock-owning compatibility wrapper around `prepare_candidate()`.

- [ ] **Step 4: Add exact independent review receipts**

```python
@dataclass(frozen=True)
class ConflictReviewReceipt:
    candidate_sha: str
    resolver_backend: str
    reviewer_backend: str
    verdict: Literal["green", "major"]
    findings: tuple[str, ...]
    reviewed_at: str


class IndependentConflictReviewer(Protocol):
    def review(
        self,
        *,
        candidate_sha: str,
        worktree: Path,
        resolution_record: Path,
    ) -> ConflictReviewReceipt: ...
```

`validate_conflict_review` requires different backend IDs, exact SHA, a complete resolution record, and zero findings for green.

- [ ] **Step 5: Run GREEN and commit**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync.py tests/cloudadvisor_ops/test_sync_review.py -q
git add ops/cloudadvisor/hermes_ops/sync.py ops/cloudadvisor/hermes_ops/sync_review.py tests/cloudadvisor_ops/test_sync.py tests/cloudadvisor_ops/test_sync_review.py
git commit -m "refactor(ops): classify sync candidates under one lock"
```

---

### Task 4: Create immutable sync eligibility receipts

**Files:**

- Create: `ops/cloudadvisor/hermes_ops/sync_receipt.py`
- Create: `tests/cloudadvisor_ops/test_sync_receipt.py`
- Modify: `ops/cloudadvisor/hermes_ops/cli.py`
- Modify: `ops/cloudadvisor/hermes-operations.example.yaml`
- Modify: `tests/cloudadvisor_ops/test_cli.py`

**Interfaces:**

- Consumes: candidate, optional conflict review, exact PR evidence, merge SHA.
- Produces: read-only canonical `SyncEligibilityReceipt` JSON.

- [ ] **Step 1: Write failing tamper and eligibility tests**

```python
def test_clean_green_candidate_is_eligible(tmp_path):
    artifact = write_sync_receipt(tmp_path, clean_candidate(), green_pr())
    assert SyncEligibilityReceipt.load(artifact.path).eligible is True


def test_minor_candidate_without_review_is_rejected(tmp_path):
    with pytest.raises(SyncReceiptError, match="independent review"):
        build_sync_receipt(minor_candidate(), green_pr(), conflict_review=None)
```

Cover writable/tampered receipt, SHA/PR/base mismatch, missing local checks, red aggregate, major classification, and unknown fields.

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_receipt.py tests/cloudadvisor_ops/test_cli.py -q
```

- [ ] **Step 3: Implement the receipt**

```python
@dataclass(frozen=True)
class SyncEligibilityReceipt:
    schema_version: int
    repo_slug: str
    base_sha: str
    upstream_sha: str
    candidate_sha: str
    classification: str
    local_checks: tuple[CheckResult, ...]
    review: ConflictReviewReceipt | None
    pr_number: int
    pr_head_sha: str
    required_check: str
    required_check_conclusion: str
    merge_sha: str | None
    eligible: bool
    created_at: str


@dataclass(frozen=True)
class SyncReceiptArtifact:
    path: Path
    sha256: str
```

Write sorted canonical JSON atomically with mode `0400`; recompute eligibility on load rather than trusting the stored boolean.
`finalize_sync_receipt(premerge_path: Path, *, merge_sha: str) -> SyncReceiptArtifact`
must create a new immutable artifact; it must never rewrite the pre-merge receipt.

- [ ] **Step 4: Add exact configuration**

Add:

```yaml
sync:
  receipt_root: /Users/cloudadvisor/.hermes/recovery/sync-receipts
  required_check: All required checks pass
  check_timeout_seconds: 2700
  poll_interval_seconds: 15
  resolver_backend: codex
  reviewer_backend: claude
```

- [ ] **Step 5: Run GREEN and commit**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_receipt.py tests/cloudadvisor_ops/test_cli.py -q
git add ops/cloudadvisor/hermes_ops/sync_receipt.py ops/cloudadvisor/hermes_ops/cli.py ops/cloudadvisor/hermes-operations.example.yaml tests/cloudadvisor_ops/test_sync_receipt.py tests/cloudadvisor_ops/test_cli.py
git commit -m "feat(ops): attest automatic sync eligibility"
```

---

### Task 5: Add separate automated-sync deploy authority

**Files:**

- Modify: `ops/cloudadvisor/hermes_ops/deploy.py`
- Modify: `ops/cloudadvisor/hermes_ops/cli.py`
- Modify: `tests/cloudadvisor_ops/test_deploy.py`
- Modify: `tests/cloudadvisor_ops/test_cli.py`

**Interfaces:**

- Consumes: existing human approval or exact sync receipt.
- Produces: unchanged `DeploymentRecord`; human behavior remains compatible.

- [ ] **Step 1: Write failing authority-separation tests**

```python
def test_human_deploy_still_requires_named_approver(deploy_fixture):
    request = deploy_fixture.human_request(approver="Someone Else")
    with pytest.raises(PreflightError, match="required approver"):
        deploy(request, **deploy_fixture.dependencies)


def test_sync_deploy_accepts_only_exact_merged_receipt(deploy_fixture):
    request = DeployRequest(
        sha=deploy_fixture.merge_sha,
        pr_number=7,
        actor="hermes-upstream-sync",
        authority_kind="automated_sync",
        authority_record=deploy_fixture.sync_receipt_path,
    )
    record = deploy(request, **deploy_fixture.dependencies)
    assert record.status == "deployed"
```

Reject candidate SHA instead of merge SHA, non-green/writable/tampered receipt, mismatched PR, and crossed human/sync artifact types.

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_deploy.py tests/cloudadvisor_ops/test_cli.py -q
```

- [ ] **Step 3: Generalize only authority validation**

```python
@dataclass(frozen=True)
class DeployRequest:
    sha: str
    pr_number: int
    actor: str
    authority_kind: Literal["human", "automated_sync"] = "human"
    authority_record: Path | None = None
    approval_record: Path | None = None
```

Move current logic unchanged into `_validate_human_authority`. `_validate_sync_authority` reloads the receipt and verifies exact PR/candidate/merge/check identity. Both still require GitHub merge evidence, preservation, clean checkout, exact `origin/main`, snapshot, and runtime health.

- [ ] **Step 4: Add a separate CLI**

```text
hermes-ops deploy-sync --config PATH --sha SHA --pr-number N --sync-receipt PATH
```

Keep existing `deploy --approval-record` semantics unchanged.

- [ ] **Step 5: Run GREEN and commit**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_deploy.py tests/cloudadvisor_ops/test_cli.py -q
git add ops/cloudadvisor/hermes_ops/deploy.py ops/cloudadvisor/hermes_ops/cli.py tests/cloudadvisor_ops/test_deploy.py tests/cloudadvisor_ops/test_cli.py
git commit -m "feat(ops): authorize attested sync deployments"
```

---

### Task 6: Orchestrate automatic merge, deploy, rollback, and revert

**Files:**

- Create: `ops/cloudadvisor/hermes_ops/sync_controller.py`
- Create: `ops/cloudadvisor/hermes_ops/sync_recovery.py`
- Create: `tests/cloudadvisor_ops/test_sync_controller.py`
- Create: `tests/cloudadvisor_ops/test_sync_recovery.py`
- Modify: `ops/cloudadvisor/hermes_ops/cli.py`

**Interfaces:**

- Consumes: Tasks 2–5 ports, existing deploy adapters, clock/sleeper, existing sync lock.
- Produces: `AutonomousSyncResult` terminal states.

- [ ] **Step 1: Write failing clean-path test**

```python
def test_clean_candidate_merges_and_deploys_without_human_artifact():
    result = run_autonomous_sync(deps=green_clean_dependencies())
    assert result.state is AutonomousSyncState.DEPLOYED
    assert result.merge_sha == result.deployed_sha
    assert result.needs_ole is False
```

Assert order: lock → prepare → exact evidence poll → receipt → merge → finalized receipt → deploy → health → status.

- [ ] **Step 2: Write failing recovery tests**

```python
def test_healthy_rollback_creates_green_exact_revert_and_converges():
    deps = failed_deploy_dependencies(rollback_healthy=True, revert_green=True)
    result = run_autonomous_sync(deps.config, **deps.injected)
    assert result.state is AutonomousSyncState.ROLLED_BACK_REVERTED
    assert result.fork_main_sha == result.installed_sha == deps.previous_sha


def test_failed_rollback_stops_without_revert_retry():
    deps = failed_deploy_dependencies(rollback_healthy=False)
    result = run_autonomous_sync(deps.config, **deps.injected)
    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert deps.github.revert_prs_created == 0


def test_changed_pr_head_stops_before_merge():
    deps = green_clean_dependencies(pr_head="changed-after-verification")
    result = run_autonomous_sync(deps.config, **deps.injected)
    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert deps.github.merge_calls == []


def test_same_failed_fingerprint_is_quarantined():
    deps = quarantined_candidate_dependencies()
    result = run_autonomous_sync(deps.config, **deps.injected)
    assert result.state is AutonomousSyncState.NEEDS_OLE
    assert deps.github.merge_calls == []
```

Cover pending timeout, one transient API retry, red checks after bounded repair, minor without review, major conflict, revert-check failure, and lock contention.

- [ ] **Step 3: Run RED**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_controller.py tests/cloudadvisor_ops/test_sync_recovery.py -q
```

- [ ] **Step 4: Implement the bounded controller**

```python
class AutonomousSyncState(str, Enum):
    NO_CHANGE = "NO_CHANGE"
    DEPLOYED = "DEPLOYED"
    ROLLED_BACK_REVERTED = "ROLLED_BACK_REVERTED"
    NEEDS_OLE = "NEEDS_OLE"
    LOCKED = "LOCKED"


@dataclass(frozen=True)
class AutonomousSyncResult:
    state: AutonomousSyncState
    candidate_sha: str | None = None
    merge_sha: str | None = None
    deployed_sha: str | None = None
    fork_main_sha: str | None = None
    installed_sha: str | None = None
    needs_ole: bool = False
    reason: str | None = None

    @classmethod
    def locked(cls) -> "AutonomousSyncResult":
        return cls(state=AutonomousSyncState.LOCKED, reason="sync lock held")

    @classmethod
    def no_change(cls, candidate: SyncResult) -> "AutonomousSyncResult":
        return cls(
            state=AutonomousSyncState.NO_CHANGE,
            candidate_sha=candidate.candidate_sha,
        )


@dataclass(frozen=True)
class AutonomousSyncConfig:
    sync: SyncConfig
    deploy: DeployConfig
    receipt_root: Path
    required_check: str
    check_timeout_seconds: int = 2700
    poll_interval_seconds: int = 15


def run_autonomous_sync(
    config: AutonomousSyncConfig,
    *,
    runner: CommandRunner,
    github: SyncGitHubPort,
    resolver: ConflictResolver | None,
    reviewer: IndependentConflictReviewer | None,
    deploy_fn: Callable[[Path, str, int], DeploymentRecord],
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> AutonomousSyncResult:
    with try_exclusive_file_lock(config.sync.lock_path) as acquired:
        if not acquired:
            return AutonomousSyncResult.locked()
        candidate = prepare_candidate(
            config.sync, github=github, runner=runner, resolver=resolver
        )
        if candidate.state is SyncState.NO_CHANGE:
            return AutonomousSyncResult.no_change(candidate)
        reviewed = require_conflict_review(candidate, reviewer=reviewer)
        evidence = wait_for_green_exact_head(
            github, reviewed, clock=clock, sleeper=sleeper
        )
        receipt = attest_candidate(config, reviewed, evidence)
        merge_sha = github.merge_exact(
            evidence.number, expected_head=reviewed.candidate_sha
        )
        final_receipt = finalize_sync_receipt(receipt.path, merge_sha=merge_sha)
        deployment = deploy_fn(final_receipt.path, merge_sha, evidence.number)
        return finish_or_recover(config, reviewed, deployment, github=github)
```

Private helpers have these exact contracts:

```python
def require_conflict_review(
    candidate: SyncResult,
    *,
    reviewer: IndependentConflictReviewer | None,
) -> SyncResult: ...

def wait_for_green_exact_head(
    github: SyncGitHubPort,
    candidate: SyncResult,
    *,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> SyncPullRequestEvidence: ...

def attest_candidate(
    config: AutonomousSyncConfig,
    candidate: SyncResult,
    evidence: SyncPullRequestEvidence,
) -> SyncReceiptArtifact: ...

def finish_or_recover(
    config: AutonomousSyncConfig,
    candidate: SyncResult,
    deployment: DeploymentRecord,
    *,
    github: SyncGitHubPort,
) -> AutonomousSyncResult: ...
```

Poll only the exact PR head until green, red, changed, or 2700-second deadline. One transient API failure may retry; repeated identical failure returns `NEEDS_OLE`.

- [ ] **Step 5: Implement protected revert recovery**

Create `auto-sync/revert-<merge12>` from current `origin/main`, run `git revert --no-edit <merge_sha>`, verify, push explicit refspec, create one PR, wait for required checks, and exact-head merge. Record/quarantine the failed upstream/candidate fingerprint. Never reset/push main.

- [ ] **Step 6: Wire `sync-auto`**

```text
python -m ops.cloudadvisor.hermes_ops.cli sync-auto --config PATH
```

Keep `sync` prepare-only. Return `0` for `NO_CHANGE`, `DEPLOYED`, and `ROLLED_BACK_REVERTED`; `75` for lock contention; `2` for `NEEDS_OLE`.

- [ ] **Step 7: Run GREEN and commit**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_controller.py tests/cloudadvisor_ops/test_sync_recovery.py tests/cloudadvisor_ops/test_sync.py tests/cloudadvisor_ops/test_deploy.py tests/cloudadvisor_ops/test_cli.py -q
git add ops/cloudadvisor/hermes_ops/sync_controller.py ops/cloudadvisor/hermes_ops/sync_recovery.py ops/cloudadvisor/hermes_ops/cli.py tests/cloudadvisor_ops/test_sync_controller.py tests/cloudadvisor_ops/test_sync_recovery.py
git commit -m "feat(ops): converge upstream sync automatically"
```

---

### Task 7: Make cron and update status truthful

**Files:**

- Create: `ops/cloudadvisor/hermes_ops/sync_status.py`
- Create: `tests/cloudadvisor_ops/test_sync_status.py`
- Modify: `ops/cloudadvisor/OPERATIONS.md`
- Modify: `ops/cloudadvisor/hermes-operations.example.yaml`
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/pages/SystemPage.tsx`
- Create: `web/src/pages/SystemPage.test.tsx`
- Modify: `apps/desktop/src/types/hermes.ts`
- Modify: `apps/desktop/src/store/updates.ts`
- Modify: `apps/desktop/src/store/updates.test.ts`
- Create: `tests/hermes_cli/test_web_server_upstream_sync_status.py`

**Interfaces:**

- Consumes: controller result/status JSON.
- Produces: quiet cron success, one deduplicated Ole packet, and additive update fields.

- [ ] **Step 1: Write failing status tests**

```python
def test_pr_updated_is_not_terminal_success():
    status = status_from_result(prepared_only_result())
    assert status.sync_state == "PR_UPDATED"
    assert status.converged is False


def test_same_needs_ole_fingerprint_notified_once(tmp_path):
    store = SyncNotificationStore(tmp_path / "notifications.json")
    packet = needs_ole_result(fingerprint="same")
    assert store.should_notify(packet) is True
    store.record_notified(packet)
    assert store.should_notify(packet) is False


def test_installed_current_does_not_hide_upstream_backlog(client, status_file):
    status_file.write_text(sync_status(upstream_behind=54, fork_behind=0))
    payload = client.get("/api/hermes/update/check?force=true").json()
    assert payload["behind"] == 0
    assert payload["update_available"] is False
    assert payload["upstream_behind"] == 54
```

TypeScript tests must render “Installed current · 54 official upstream commits syncing,” not “latest version.”

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_status.py tests/hermes_cli/test_web_server_upstream_sync_status.py -q
cd web && npm test -- --run src/pages/SystemPage.test.tsx
cd ..
cd apps/desktop && npm test -- --run src/store/updates.test.ts
```

- [ ] **Step 3: Implement durable status**

```python
@dataclass(frozen=True)
class SyncStatus:
    schema_version: int
    checked_at: str
    upstream_behind: int | None
    sync_state: str
    sync_pr_number: int | None
    required_check: str | None
    fork_main_sha: str | None
    installed_sha: str | None
    escalation_fingerprint: str | None


def status_from_result(result: AutonomousSyncResult) -> SyncStatus: ...


class SyncNotificationStore:
    def __init__(self, path: Path):
        self.path = path

    def should_notify(self, result: AutonomousSyncResult) -> bool: ...
    def record_notified(self, result: AutonomousSyncResult) -> None: ...
```

Write canonical status atomically without secrets/raw logs. API fields are additive: `upstream_behind`, `sync_state`, `sync_pr_number`, `sync_required_check`, `fork_behind`, `installed_sha`. Keep existing `behind` meaning installed-versus-fork.

- [ ] **Step 4: Document cron semantics**

Document that 06:00/18:00 invokes `sync-auto`; clean/minor green is quiet; `NEEDS_OLE` alerts once. The live script activation happens in Task 8 after bootstrap deploy.

- [ ] **Step 5: Run GREEN and commit**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_status.py tests/hermes_cli/test_web_server_upstream_sync_status.py tests/hermes_cli/test_update_check.py -q
cd web && npm test -- --run src/pages/SystemPage.test.tsx
cd ..
cd apps/desktop && npm test -- --run src/store/updates.test.ts
cd ../..
git add ops/cloudadvisor/hermes_ops/sync_status.py ops/cloudadvisor/OPERATIONS.md ops/cloudadvisor/hermes-operations.example.yaml hermes_cli/web_server.py web/src/lib/api.ts web/src/pages/SystemPage.tsx web/src/pages/SystemPage.test.tsx apps/desktop/src/types/hermes.ts apps/desktop/src/store/updates.ts apps/desktop/src/store/updates.test.ts tests/cloudadvisor_ops/test_sync_status.py tests/hermes_cli/test_web_server_upstream_sync_status.py
git commit -m "feat(update): expose autonomous upstream sync state"
```

---

### Task 8: Full integration, bootstrap, and immediate PR #7 convergence

**Files:**

- Create: `tests/cloudadvisor_ops/test_autonomous_sync_integration.py`
- Modify: `tests/cloudadvisor_ops/test_release_paths_integration.py`
- Activation-only: `/Users/cloudadvisor/.hermes/scripts/upstream-sync.sh`
- Production artifacts: sync receipts, deployment record, canary record, status receipt.

**Interfaces:**

- Consumes: complete controller and current PR #7.
- Produces: protected bootstrap merge/deploy and automatically converged current upstream.

- [ ] **Step 1: Add real disposable-repository tests**

Prove upstream commit → candidate PR → exact merge SHA → deployed SHA without a human artifact. Inject candidate health failure and prove previous runtime health, exact revert merge, fork/runtime convergence, and quarantine.

- [ ] **Step 2: Run the new integration tests**

```bash
scripts/run_tests.sh tests/cloudadvisor_ops/test_autonomous_sync_integration.py tests/cloudadvisor_ops/test_release_paths_integration.py -q
```

Expected: both real-path suites pass.

- [ ] **Step 3: Commit integration tests**

```bash
git add tests/cloudadvisor_ops/test_autonomous_sync_integration.py tests/cloudadvisor_ops/test_release_paths_integration.py
git commit -m "test(ops): prove autonomous sync convergence"
```

- [ ] **Step 4: Run complete verification**

```bash
scripts/run_tests.sh tests/scripts/test_check_contributor_attribution.py tests/cloudadvisor_ops tests/hermes_cli/test_web_server_upstream_sync_status.py tests/hermes_cli/test_update_check.py
"$HOME/.hermes/hermes-agent/.venv/bin/ruff" check ops/cloudadvisor hermes_cli scripts tests/cloudadvisor_ops tests/scripts
git diff --check origin/main...HEAD
scripts/run_tests.sh
```

Expected: every command exits `0`, no failed/timed-out file, clean worktree.

- [ ] **Step 5: Independent Standards and spec review**

Review exact head against `origin/main` and the approved design. Fix each finding with a focused regression test and separate commit, then rerun Step 2.

- [ ] **Step 6: Bootstrap through current protection**

Push one implementation PR, wait for `All required checks pass`, and merge the exact reviewed head under Ole's approval recorded on 2026-07-12. Deploy the exact bootstrap merge SHA through the existing human approval path and verify runtime health.

- [ ] **Step 7: Activate and trigger immediately**

Atomically change `/Users/cloudadvisor/.hermes/scripts/upstream-sync.sh` from `sync` to `sync-auto`, record before/after checksum and rollback copy, then run it immediately rather than waiting for 18:00.

- [ ] **Step 8: Process PR #7**

Refresh PR #7 to then-current official upstream, pass exact-ancestry attribution and required checks, exact-head merge, deploy exact merge SHA, and record installed/fork/upstream convergence. If major, stop with one evidence packet.

- [ ] **Step 9: Run recovery canary and final audit**

Use permitted one-shot failure injection only against the installed SHA or recovery-canary environment. Prove healthy rollback/revert without touching Trading services, then confirm normal runtime health and active 06:00/18:00 schedule.

Attach bootstrap PR/merge/deploy, PR #7 head/merge/deploy, eligibility receipt, health, canary, cron, and status artifacts to the recovery record.
