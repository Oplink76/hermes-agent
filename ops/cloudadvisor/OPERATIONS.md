# CloudAdvisor Hermes operations

This package keeps fork synchronization and installation deployment as separate
evidence gates. `sync` can only update `auto-sync/upstream` and its pull request.
`sync-auto` may continue through the protected merge and deploy only the exact
attested merge SHA after every required gate is green.

If a merge conflicts, the example resolver runs ephemeral Codex in its
workspace-write sandbox with user configuration disabled. Arbitrary commands and
sandbox escape flags are rejected. The state machine still requires an intact
`MERGE_HEAD`, no unmerged entries, a completed merge commit, and every local gate
before it can push the candidate branch. Fork `main` remains protected by GitHub
branch protection.

Copy `hermes-operations.example.yaml` to an operator-owned path and review every
absolute path. The example deliberately sets `preservation_command` to
`/usr/bin/false`: deployment remains blocked until Package 1 has a durable
completion receipt and this command is replaced by its verifier.

The example service scope contains only the currently active default gateway and
dashboard. The 13 Trading Company gateways stay outside deploy/start scope until
their desired launchd state is explicitly approved.

## Commands

Run from the repository root with its `.venv` active:

```bash
python -m ops.cloudadvisor.hermes_ops.cli sync --config /path/to/operations.yaml
python -m ops.cloudadvisor.hermes_ops.cli sync-auto --config /path/to/operations.yaml
python -m ops.cloudadvisor.hermes_ops.cli health \
  --config /path/to/operations.yaml --sha APPROVED_SHA
python -m ops.cloudadvisor.hermes_ops.cli deploy \
  --config /path/to/operations.yaml \
  --sha APPROVED_MERGE_SHA \
  --pr-number 123 \
  --approval-record /path/to/approval.json \
  --actor operator-name
```

`health` returns success only when at least one mandatory check exists and all
mandatory checks pass. `deploy` verifies the immutable approval, GitHub merge and
required check, Package 1 receipt, clean checkout, exact `origin/main` SHA, and a
fresh Git/SQLite snapshot before stopping a service. A failed post-restart health
matrix automatically switches back to the previous SHA, restores state, restarts
the same service set, and records rollback health.

## Autonomous schedule and status

The production schedule runs `sync-auto` at 06:00 and 18:00 Europe/Copenhagen.
A clean sync, an independently reviewed minor conflict, and an already-converged
run are quiet successes. `PR_UPDATED` is an in-progress state, not success.
`NEEDS_OLE` emits one `notify_ole: true` decision for the active escalation
fingerprint; later runs with the same fingerprint stay quiet. This field requests
delivery by the Task 8 wrapper and does not claim a notification was sent. A
different fingerprint emits a new decision, and a terminal healthy cycle clears
the active fingerprint. Major or unresolved conflicts, ambiguous authority, and
failed rollback/revert are the paths that require Ole.

The `sync-auto` publisher writes the canonical, secret-free status atomically to
the configured `sync.status_file` after every controller outcome. Active
escalation fingerprints are stored separately
in `sync.notification_store`; neither file contains raw command output. The
dashboard keeps `behind` (and additive `fork_behind`) as installed-versus-fork,
while `upstream_behind`, `sync_state`, and `sync_pr_number` report official
upstream progress independently. Therefore an installed runtime can be current
with fork `main` while official upstream commits are still syncing.

Task 8 activates the live 06:00/18:00 script only after the bootstrap release is
deployed. This document describes the intended schedule; this task does not
change launchd, cron, services, or the production installation.

## Approval artifact

The approval file must be read-only on POSIX systems and contain:

```json
{
  "approver": "Ole Ørum-Petersen",
  "pr_number": 123,
  "merge_sha": "full-merge-sha",
  "approved_at": "2026-07-10T12:00:00+02:00",
  "decision_packet": "/absolute/path/to/decision-packet.json",
  "decision_packet_sha256": "64-lowercase-hex-characters"
}
```

The file and referenced JSON decision packet are hashed again during preflight.
The packet must name the same PR and candidate SHA and record green CI,
independent review, and local tests. Any change, missing file, different PR/SHA,
unmerged PR, or non-green required check fails before snapshots or services are
touched. A deployment-wide file lock prevents concurrent operators from racing
service control, checkout, snapshots, or rollback.

Failure injection is accepted only in a `recovery_canary` environment or when the
requested SHA is already the installed SHA. It is a one-shot failure: candidate
health fails and rollback health must independently pass.
