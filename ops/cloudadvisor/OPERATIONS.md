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
python -m ops.cloudadvisor.hermes_ops.config_migration migrate \
  --config /path/to/operations.yaml \
  --backup-dir /operator-owned/rollback-directory
python -m ops.cloudadvisor.hermes_ops.cli sync-auto \
  --config /path/to/operations.yaml --preflight
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

The versioned migration is for an existing July 10 production config, not the
fail-closed example. It refuses `/usr/bin/false`, requires and preserves
`uv_extras: [all, dev, slack]`, preserves the complete Package-1 verifier and
all unrelated runtime/deploy values, then writes the config atomically. Before
replacement it records the original and migrated SHA-256 values, an exact
read-only backup, and a versioned rollback manifest. Restore only through the
checksum-gated command printed in the activation checklist; never copy the
example over a live config.

`sync-auto --preflight` is read-only. It validates the complete migrated
schema, exact fork/upstream identities and remotes, state/decision paths,
non-Trading runtime allowlist, Package-1 verifier, and availability of GitHub
CLI, Codex, Claude, and the direct-delivery command. It does not acquire the
sync lock, merge, deploy, write receipts/status/outbox files, or touch services.

`health` returns success only when at least one mandatory check exists and all
mandatory checks pass. `deploy` verifies the immutable approval, GitHub merge and
required check, Package 1 receipt, clean checkout, exact `origin/main` SHA, and a
fresh Git/SQLite snapshot before stopping a service. A failed post-restart health
matrix automatically switches back to the previous SHA, preserves live mutable
state, restarts the same service set, and records rollback health. The snapshot
is retained for explicit manual recovery only.

## Autonomous schedule and status

The production schedule runs `sync-auto` at 06:00 and 18:00 Europe/Copenhagen.
A clean sync, an independently reviewed minor conflict, and an already-converged
run are quiet successes. `PR_UPDATED` is an in-progress state, not success.
`NEEDS_OLE` stages a durable outbox record and keeps `notify_ole: true` until the
versioned `cron_wrapper` invokes the configured direct delivery command and
atomically acknowledges only after that command returns success for the exact packet,
fingerprint, digest, and idempotency key. The wrapper drains an older pending
record before starting a new sync; neither a healthy terminal cycle nor changed
evidence may erase an unacknowledged alert. A delivery failure remains pending
for retry. A crash after handoff but before acknowledgement can repeat the same
packet, so downstream delivery should deduplicate its stable decision id. This
at-least-once boundary prefers a controlled duplicate over losing Ole's only
alert. Malformed or contradictory output fails closed on stderr and is never
reported as `NO_CHANGE`.

The direct command receives the message on stdin, not argv or config, and must
return nonzero on Slack failure. The production command uses `hermes send`,
which reads the existing profile-scoped Slack credential without exposing it to
the wrapper. Keep the sync cron job's scheduler-level `deliver` value at
`slack:C0BFLTFC2LS`. Direct delivery success and every routine result emit no
output, so the no-agent scheduler converts them to its `[SILENT]` sentinel and
sends nothing. A wrapper/config/direct-delivery failure instead emits only a
concise safe failure; scheduler Slack delivery is the last-resort alert while
the outbox remains pending for the next stable retry. Hermes `deliver: local`
is a sentinel meaning “no delivery target”, not a fallback channel, so changing
the production job to `local` would remove this safety net. The independent
`health` action retains its existing stdout/scheduler delivery behavior.

The `sync-auto` controller writes the canonical, secret-free status atomically
to the configured `sync.status_file` before releasing its exclusive sync lock.
It stages the outbox in `sync.notification_store` under the same lock. A
contending `LOCKED` run does not write or clear either file. Neither file
contains raw command output. The dashboard keeps `behind`
(and additive `fork_behind`) as installed-versus-fork, while `upstream_behind`,
`sync_state`, and `sync_pr_number` report official upstream progress
independently. Therefore an installed runtime can be current with fork `main`
while official upstream commits are still syncing.

Each escalation packet is canonical JSON under the configured trusted
`sync.receipt_root/decision-packets/<fingerprint>/` directory. The filename is
the SHA-256 of its contents. It contains only the exact PR/commit identities,
the stable reason code and failed gate, repo/PR/commit identities, affected
files, rollback/revert evidence, a content-addressed structured Details
artifact, and `Approve / Wait / Details`; it never embeds free-form command
output or controller logs. `NEEDS_OLE`, safe-rollback recovery, `merge_intent`,
`merged_pending_deploy`, and invalid crossed checkpoint evidence suppress both
displayed and direct dashboard/desktop updates.

Task 8 activates the live 06:00/18:00 script only after the bootstrap release is
deployed. This document describes the intended schedule; this task does not
change launchd, cron, services, or the production installation.

The repository-owned wrapper is `ops/cloudadvisor/upstream-sync.sh`. Its default
action is `sync-auto`; passing `health` preserves the existing attention-only
gateway health action. Install or activation follows
`ops/cloudadvisor/AUTONOMOUS_SYNC_ACTIVATION.md`.

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
