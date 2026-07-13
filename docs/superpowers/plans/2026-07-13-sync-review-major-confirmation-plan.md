# Confirmed-major Sync Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent one non-reproducible Claude `major` verdict from involving Ole while preserving fail-closed exact-head review and durable, actionable evidence for a confirmed major.

**Architecture:** Add a focused immutable-artifact module for structured review attempts. Refactor `ClaudeConflictReviewer` to persist its first result and run exactly one findings-specific confirmation after an initial major. Carry the final artifact reference through a typed review error into the existing escalation decision-details field; do not change CI, merge, deploy, rollback, scheduling, or runtime scope.

**Tech Stack:** Python 3.11+, frozen dataclasses, canonical JSON and SHA-256, POSIX file modes, existing `CommandRunner`, pytest, existing CloudAdvisor operations controller.

## Global Constraints

- A first green verdict performs one Claude call.
- A first major verdict must contain non-empty findings and performs exactly one focused confirmation call.
- Confirmation green continues as minor-resolved; confirmation major escalates.
- Command, parse, exact-head, worktree-state, or evidence-write failure remains fail-closed.
- Raw model transcripts are never persisted.
- Review artifacts are canonical, digest-named, direct regular files and mode `0400` on POSIX.
- Codex remains resolver and Claude remains reviewer.
- No changes to candidate preparation, CI authority, protected merge, deployment, rollback, schedule, notification idempotency, or Trading runtime scope.

---

## File structure

- Create `ops/cloudadvisor/hermes_ops/sync_review_evidence.py`: canonical schema, validation, loading, and atomic publication for one review attempt.
- Create `tests/cloudadvisor_ops/test_sync_review_evidence.py`: content binding, mode, path, schema, and symlink tests for review artifacts.
- Modify `ops/cloudadvisor/hermes_ops/sync_review.py`: bounded review execution, confirmation prompt, artifact chaining, and typed review errors.
- Modify `tests/cloudadvisor_ops/test_sync_review.py`: one-call green, major→green, major→major, invalid-major, confirmation failure, command-boundary, exact-head, and mutation tests.
- Modify `ops/cloudadvisor/hermes_ops/sync_controller.py`: classify confirmed major separately and carry trusted review-artifact references into escalation details.
- Modify `tests/cloudadvisor_ops/test_sync_controller.py`: confirmed-major outcome and evidence-reference tests.
- Modify `tests/cloudadvisor_ops/test_decision_packet.py`: prove the existing decision-details artifact exposes the review evidence reference without copying findings into the notification packet.
- Modify `ops/cloudadvisor/AUTONOMOUS_SYNC_ACTIVATION.md`: document the two-verdict major threshold and fail-closed exceptions.

---

### Task 1: Immutable conflict-review attempt artifacts

**Files:**
- Create: `ops/cloudadvisor/hermes_ops/sync_review_evidence.py`
- Create: `tests/cloudadvisor_ops/test_sync_review_evidence.py`

**Interfaces:**
- Produces: `ConflictReviewAttemptArtifact.load(path: Path) -> ConflictReviewAttemptArtifact`
- Produces: `write_conflict_review_attempt(receipt_root: Path, *, candidate_sha: str, resolution_record_sha256: str, resolver_backend: str, reviewer_backend: str, attempt: int, review_kind: Literal["initial", "major_confirmation"], verdict: Literal["green", "major"], findings: tuple[str, ...], reviewed_at: str, prior_artifact_sha256: str | None = None) -> ConflictReviewAttemptArtifact`
- Artifact property: `relative_path: str`, always `conflict-reviews/review-<sha256>.json`

- [ ] **Step 1: Write failing round-trip and validation tests**

Create tests that call the not-yet-existing writer and assert exact fields, digest binding, canonical bytes, and POSIX mode:

```python
def test_review_attempt_round_trip_is_candidate_and_resolution_bound(tmp_path: Path):
    artifact = write_conflict_review_attempt(
        tmp_path,
        candidate_sha="a" * 40,
        resolution_record_sha256="b" * 64,
        resolver_backend="codex",
        reviewer_backend="claude",
        attempt=1,
        review_kind="initial",
        verdict="major",
        findings=("kanban invariant changed",),
        reviewed_at="2026-07-13T10:00:00Z",
    )

    loaded = ConflictReviewAttemptArtifact.load(artifact.path)
    assert loaded == artifact
    assert artifact.relative_path == f"conflict-reviews/review-{artifact.sha256}.json"
    assert artifact.candidate_sha == "a" * 40
    assert artifact.resolution_record_sha256 == "b" * 64
    if os.name != "nt":
        assert stat.S_IMODE(artifact.path.stat().st_mode) == 0o400
```

Add parametrized failures for invalid SHA, attempt outside `{1, 2}`, crossed kind/attempt, green with findings, major without findings, confirmation without a prior digest, initial with a prior digest, non-canonical bytes, filename/digest mismatch, symlink input, and artifact outside a direct `conflict-reviews` directory.

- [ ] **Step 2: Run the artifact tests and confirm RED**

Run:

```bash
./scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_review_evidence.py -q
```

Expected: collection fails because `sync_review_evidence` does not exist.

- [ ] **Step 3: Implement the canonical artifact module**

Implement `SCHEMA_VERSION = 1`, the `ReviewKind` and `ReviewVerdict` literal
aliases shown in the Interfaces section, and a frozen
`ConflictReviewAttemptArtifact` dataclass with these fields in order: `path`,
`sha256`, `candidate_sha`, `resolution_record_sha256`, `resolver_backend`,
`reviewer_backend`, `attempt`, `review_kind`, `verdict`, `findings`,
`reviewed_at`, and `prior_artifact_sha256`. Its `relative_path` property must
return `conflict-reviews/<artifact filename>`. Its `load(path)` classmethod
must read and validate one already-published artifact using the same schema and
content-binding rules as the writer.

Implement `write_conflict_review_attempt` with the exact signature declared in
the Interfaces section.

The writer must:

1. Validate full lowercase candidate and digest identities.
2. Require `attempt=1` with `initial` and no prior digest, or `attempt=2` with `major_confirmation` and a prior digest.
3. Require green findings to be empty and major findings to be non-empty strings.
4. Serialize with `sort_keys=True`, separators `(",", ":")`, UTF-8, and one trailing newline.
5. Create `<receipt_root>/conflict-reviews` as mode `0700`, rejecting a symlink or non-directory.
6. Publish `review-<digest>.json` with exclusive/no-follow creation, `fsync`, and mode `0400` on POSIX.
7. Reuse an existing digest path only when bytes and mode validate through `load`.

- [ ] **Step 4: Run artifact tests and confirm GREEN**

Run:

```bash
./scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_review_evidence.py -q
```

Expected: all artifact tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add ops/cloudadvisor/hermes_ops/sync_review_evidence.py tests/cloudadvisor_ops/test_sync_review_evidence.py
git commit -m "feat(ops): persist exact conflict review attempts"
```

---

### Task 2: Bounded findings-specific major confirmation

**Files:**
- Modify: `ops/cloudadvisor/hermes_ops/sync_review.py`
- Modify: `tests/cloudadvisor_ops/test_sync_review.py`

**Interfaces:**
- Consumes: `write_conflict_review_attempt(...)` from Task 1.
- Extends: `ConflictReviewReceipt.evidence_artifact: str | None = None`.
- Extends: `ConflictReviewError(message: str, *, details_artifact: str | None = None)`.
- Produces: `ConfirmedMajorReviewError`, a `ConflictReviewError` subtype.
- Preserves: `IndependentConflictReviewer.review(...) -> ConflictReviewReceipt`.

- [ ] **Step 1: Add a sequence runner and failing confirmation tests**

Extend the test runner so Claude calls consume ordered payloads while git calls remain deterministic:

```python
class SequenceReviewerRunner(ReviewerRunner):
    def __init__(self, payloads: list[dict[str, object]]):
        super().__init__({})
        self.payloads = iter(payloads)

    def run(self, argv: list[str], cwd: Path, timeout: int = 300):
        if Path(argv[0]).name.startswith("claude"):
            self.calls.append((tuple(argv), Path(cwd), timeout))
            payload = next(self.payloads)
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"structured_output": payload}), ""
            )
        return super().run(argv, cwd, timeout)
```

Add these behavior tests:

```python
def test_initial_major_confirmation_green_continues(tmp_path: Path):
    runner = SequenceReviewerRunner([
        {"verdict": "major", "findings": ["possible kanban regression"]},
        {"verdict": "green", "findings": []},
    ])
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    receipt = reviewer.review(
        candidate_sha=CANDIDATE_SHA,
        worktree=worktree,
        resolution_record=record,
    )

    assert receipt.verdict == "green"
    assert receipt.findings == ()
    assert receipt.evidence_artifact is not None
    assert len(claude_calls(runner)) == 2

def test_initial_major_confirmation_major_stays_major(tmp_path: Path):
    runner = SequenceReviewerRunner([
        {"verdict": "major", "findings": ["kanban gate removed"]},
        {"verdict": "major", "findings": ["kanban gate is absent at exact HEAD"]},
    ])
    reviewer, worktree, record = reviewer_fixture(tmp_path, runner)

    receipt = reviewer.review(
        candidate_sha=CANDIDATE_SHA,
        worktree=worktree,
        resolution_record=record,
    )

    assert receipt.verdict == "major"
    assert receipt.findings == ("kanban gate is absent at exact HEAD",)
    assert len(claude_calls(runner)) == 2
```

Also add failures for initial major with no findings, confirmation major with no findings, confirmation non-zero return, invalid confirmation JSON, head change after either call, worktree mutation after either call, and evidence publication failure. Assert green initial review still makes exactly one Claude call.

- [ ] **Step 2: Run focused confirmation tests and confirm RED**

Run:

```bash
./scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_review.py -q
```

Expected: new tests fail because no confirmation or evidence artifact exists.

- [ ] **Step 3: Implement one-call parsing and bounded confirmation**

Add the error evidence carrier and final artifact reference:

```python
class ConflictReviewError(ValueError):
    def __init__(self, message: str, *, details_artifact: str | None = None):
        super().__init__(message)
        self.details_artifact = details_artifact

class ConfirmedMajorReviewError(ConflictReviewError):
    """Two exact Claude reviews confirmed actionable major findings."""

@dataclass(frozen=True)
class ConflictReviewReceipt:
    candidate_sha: str
    resolver_backend: str
    reviewer_backend: str
    verdict: Literal["green", "major"]
    findings: tuple[str, ...]
    reviewed_at: str
    resolution_record_sha256: str
    evidence_artifact: str | None = None
```

Factor the existing Claude invocation into `_review_once(prompt, *, worktree, candidate_sha, resolution, status_before) -> ConflictReviewReceipt`. It must keep the existing argument list, timeout, structured schema, exact-head check, and unchanged-status check. It must reject `major` with no findings in addition to the existing green-with-findings rejection.

After the initial receipt, persist attempt 1 under `self.evidence_dir.parent`. For major, create this focused prompt using JSON-encoded findings:

```python
confirmation_prompt = (
    "Confirm or disprove only these findings from an independent review of "
    f"exact HEAD {candidate_sha}: {json.dumps(list(initial.findings))}. "
    f"Read immutable resolution record {resolution_record} with SHA-256 "
    f"{resolution.sha256}. Return major only for findings directly supported "
    "by the exact code and recorded conflict decisions; otherwise return green "
    "with zero findings. Do not modify files, commit, push, or change remotes."
)
```

Persist attempt 2 with `prior_artifact_sha256=initial_artifact.sha256`. Return the confirmation receipt with `evidence_artifact=confirmation_artifact.relative_path`. Initial green returns its receipt with the initial artifact reference. On confirmation execution/parsing/state failure, re-raise `ConflictReviewError` with `details_artifact=initial_artifact.relative_path`.

- [ ] **Step 4: Run review tests and confirm GREEN**

Run:

```bash
./scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_review.py tests/cloudadvisor_ops/test_sync_review_evidence.py -q
```

Expected: all review and artifact tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add ops/cloudadvisor/hermes_ops/sync_review.py tests/cloudadvisor_ops/test_sync_review.py
git commit -m "fix(ops): confirm major sync review findings"
```

---

### Task 3: Typed confirmed-major escalation with linked evidence

**Files:**
- Modify: `ops/cloudadvisor/hermes_ops/sync_controller.py`
- Modify: `tests/cloudadvisor_ops/test_sync_controller.py`
- Modify: `tests/cloudadvisor_ops/test_decision_packet.py`
- Modify: `ops/cloudadvisor/AUTONOMOUS_SYNC_ACTIVATION.md`

**Interfaces:**
- Consumes: `ConfirmedMajorReviewError` and `ConflictReviewReceipt.evidence_artifact` from Task 2.
- Produces reason code: `CONFLICT_REVIEW_CONFIRMED_MAJOR`.
- Produces failed gate: `conflict_review`.
- Reuses: `AutonomousSyncResult.details_artifact` and existing `controller_details_artifact` decision-details field.

- [ ] **Step 1: Write failing controller and decision-packet tests**

Add a reviewer fixture that returns a final major receipt with a trusted relative artifact:

```python
major_receipt = ConflictReviewReceipt(
    candidate_sha=CANDIDATE_SHA,
    resolver_backend="codex",
    reviewer_backend="claude",
    verdict="major",
    findings=("kanban release gate is absent",),
    reviewed_at="2026-07-13T10:00:00Z",
    resolution_record_sha256=resolution_digest,
    evidence_artifact=f"conflict-reviews/review-{'e' * 64}.json",
)
```

Assert the controller outcome is:

```python
assert result.state is AutonomousSyncState.NEEDS_OLE
assert result.reason_code == "CONFLICT_REVIEW_CONFIRMED_MAJOR"
assert result.failed_gate == "conflict_review"
assert result.details_artifact == f"conflict-reviews/review-{'e' * 64}.json"
```

In `test_decision_packet.py`, publish that result and assert the notification packet stays secret-free while its details document contains:

```python
assert packet.summary == (
    "Automation stopped at conflict_review (CONFLICT_REVIEW_CONFIRMED_MAJOR)."
)
details = json.loads(artifact.details_path.read_text())
assert details["controller_details_artifact"] == (
    f"conflict-reviews/review-{'e' * 64}.json"
)
assert "kanban release gate is absent" not in artifact.path.read_text()
```

Add an error-path test proving a confirmation command failure carries the initial attempt artifact with reason code `CONFLICT_REVIEW_INVALID` and gate `conflict_review`.

- [ ] **Step 2: Run controller and packet tests and confirm RED**

Run:

```bash
./scripts/run_tests.sh tests/cloudadvisor_ops/test_sync_controller.py tests/cloudadvisor_ops/test_decision_packet.py -q
```

Expected: confirmed major is still generic `AUTONOMOUS_GATE_FAILED` and the details reference is absent.

- [ ] **Step 3: Implement typed escalation and evidence precedence**

Import `ConfirmedMajorReviewError`. In `require_conflict_review`, replace the generic major error with:

```python
if classification is SyncClassification.MAJOR:
    raise ConfirmedMajorReviewError(
        "independent review confirmed major conflict findings",
        details_artifact=receipt.evidence_artifact,
    )
```

Add the subtype before `ConflictReviewError` in `_ERROR_REASONS`:

```python
(
    ConfirmedMajorReviewError,
    "independent conflict review confirmed major findings",
    "CONFLICT_REVIEW_CONFIRMED_MAJOR",
    "conflict_review",
),
```

In `_error_result`, compute `error_details = getattr(error, "details_artifact", None)` and use it before reconstruction/deployment checkpoint references. Validate that non-null review references are relative, have no `..`, and begin with `conflict-reviews/review-`; otherwise return the normal invalid-review failure without publishing an unsafe reference.

Update `AUTONOMOUS_SYNC_ACTIVATION.md` to state that one major verdict triggers a findings-specific confirmation, two major verdicts escalate with an immutable artifact, and any unsafe confirmation failure remains fail-closed.

- [ ] **Step 4: Run controller, packet, and integration tests and confirm GREEN**

Run:

```bash
./scripts/run_tests.sh \
  tests/cloudadvisor_ops/test_sync_controller.py \
  tests/cloudadvisor_ops/test_decision_packet.py \
  tests/cloudadvisor_ops/test_autonomous_sync_integration.py \
  -q
```

Expected: all tests pass; confirmed majors have the stable reason code and linked evidence.

- [ ] **Step 5: Commit Task 3**

```bash
git add \
  ops/cloudadvisor/hermes_ops/sync_controller.py \
  ops/cloudadvisor/AUTONOMOUS_SYNC_ACTIVATION.md \
  tests/cloudadvisor_ops/test_sync_controller.py \
  tests/cloudadvisor_ops/test_decision_packet.py
git commit -m "fix(ops): escalate only confirmed sync review majors"
```

---

### Task 4: Full verification and release evidence

**Files:**
- Modify only if a test exposes a defect in Task 1-3 files.
- Do not modify unrelated product, Kanban, gateway, Trading, or UI files.

**Interfaces:**
- Consumes all Task 1-3 behavior.
- Produces a protected PR whose exact head is eligible for standard deployment.

- [ ] **Step 1: Run the complete CloudAdvisor operations suite**

Run:

```bash
./scripts/run_tests.sh tests/cloudadvisor_ops -q
```

Expected: all CloudAdvisor operations tests pass.

- [ ] **Step 2: Run the full repository suite**

Run:

```bash
./scripts/run_tests.sh
```

Expected: the entire suite passes with zero failures.

- [ ] **Step 3: Inspect the exact diff for scope and generated files**

Run:

```bash
git diff --check origin/main...HEAD
git status --short
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
```

Expected: only the design, plan, review evidence/reviewer/controller code, activation documentation, and their tests are present. `build/` remains untracked and is not added.

- [ ] **Step 4: Request exact Standards and Spec review**

Review `origin/main...HEAD` against:

- repository `AGENTS.md` and `CONTRIBUTING.md`;
- `docs/superpowers/specs/2026-07-13-sync-review-major-confirmation-design.md`;
- this implementation plan.

Expected: zero blocking findings. Fix only findings that trace directly to the approved design, then rerun affected tests.

- [ ] **Step 5: Push and open a draft PR**

```bash
git push -u origin fix/sync-review-confirmation-20260713
gh pr create \
  --repo Oplink76/hermes-agent \
  --base main \
  --head fix/sync-review-confirmation-20260713 \
  --draft \
  --title "fix(ops): require confirmed major sync review" \
  --body "Adds one findings-specific confirmation for an initial Claude major verdict, persists exact structured review evidence, and links confirmed findings from the fail-closed escalation path."
```

Expected: a draft PR targeting protected `main`.

- [ ] **Step 6: Wait for GitHub protection and merge only exact green head**

```bash
pr_number=$(gh pr view --repo Oplink76/hermes-agent --json number --jq .number)
gh pr ready "$pr_number" --repo Oplink76/hermes-agent
gh pr checks "$pr_number" --repo Oplink76/hermes-agent --watch --interval 15
head=$(gh pr view "$pr_number" --repo Oplink76/hermes-agent --json headRefOid --jq .headRefOid)
gh pr merge "$pr_number" --repo Oplink76/hermes-agent --merge --match-head-commit "$head"
```

Expected: `All required checks pass` is successful and merge occurs without admin bypass.

- [ ] **Step 7: Deploy exact merge SHA and verify health**

Resolve the exact merged identity, write the standard immutable decision record,
and write its matching mode-`0400` approval record using the existing deployment
evidence schema. Then run:

```bash
pr_number=$(gh pr view --repo Oplink76/hermes-agent --json number --jq .number)
merge_sha=$(gh pr view "$pr_number" --repo Oplink76/hermes-agent --json mergeCommit --jq .mergeCommit.oid)
approval_record="/Users/cloudadvisor/Documents/Codex/2026-07-10/re/outputs/$(date +%F)-hermes-sync-review-confirmation-deployment-approval.json"

/Users/cloudadvisor/.hermes/hermes-agent/.venv/bin/python \
  -m ops.cloudadvisor.hermes_ops.cli deploy \
  --config /Users/cloudadvisor/.hermes/operations/hermes-operations.yaml \
  --sha "$merge_sha" \
  --pr-number "$pr_number" \
  --approval-record "$approval_record" \
  --actor "Ole Ørum-Petersen"

/Users/cloudadvisor/.hermes/hermes-agent/.venv/bin/python \
  -m ops.cloudadvisor.hermes_ops.cli health \
  --config /Users/cloudadvisor/.hermes/operations/hermes-operations.yaml \
  --sha "$merge_sha"
```

Expected: deployment status `deployed`, rollback null, and every mandatory health check passes.

- [ ] **Step 8: Prove the policy with the next scheduled-wrapper run**

Run the installed wrapper once against the then-current upstream head. If the first Claude verdict is major and the confirmation is green, verify the controller continues to CI without `NEEDS_OLE`. If both are major, verify the decision details link the immutable confirmation artifact. If the candidate is clean or initially green, verify normal convergence remains unchanged.

Expected: Ole is involved only for a confirmed major or unsafe confirmation failure.
