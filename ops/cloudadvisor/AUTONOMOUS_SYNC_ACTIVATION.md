# Autonomous upstream-sync activation checklist

This checklist changes production only after the implementation merge SHA has
passed branch protection and the exact SHA is deployed and healthy. The steps
are deliberately separate from tests: repository verification never writes live
cron, launchd, service, or installation state.

1. Verify the deployed `origin/main`, installed `HEAD`, and approved bootstrap
   merge SHA are identical. Run the configured `health` action and retain its
   green evidence.
2. Record SHA-256 checksums of the live wrapper and the repository-owned
   `ops/cloudadvisor/upstream-sync.sh`. Copy the live wrapper to an operator-owned
   rollback path before replacement.
3. Install the repository-owned wrapper atomically at
   `/Users/cloudadvisor/.hermes/scripts/upstream-sync.sh`; preserve executable
   mode and owner. Do not edit the embedded production paths during activation.
4. Confirm the 06:00 and 18:00 Europe/Copenhagen jobs invoke the wrapper with its
   default `sync-auto` action. Set this sync job's scheduler-level `deliver` to
   `none`: the wrapper now delivers directly and acknowledges only after Slack
   success. Keep the existing health job and its scheduler delivery by invoking
   the same wrapper with `health`.
5. Run the installed wrapper immediately. Routine `NO_CHANGE`, `DEPLOYED`,
   `ROLLED_BACK_REVERTED`, `PENDING_REFRESH`, and `LOCKED` results produce no
   stdout. Only a `notify_ole=true` result with a matching content-addressed
   decision packet may produce the concise `Approve / Wait / Details` handoff.
6. Verify the status file, notification request state, delivery fingerprint,
   decision packet, outbox acknowledgement, installed SHA, fork `main`, and
   runtime health. Simulate one delivery failure: the outbox must remain pending
   and the next run must retry the same decision id. The wrapper must not expose
   raw stdout/stderr or secrets.
7. If parsing, packet verification, schedule verification, or runtime health
   fails, restore the recorded rollback copy atomically and re-run `health`.
   Preserve all before/after checksums and command results in the recovery
   record. Never reset or push protected `main`.

Activation does not start or load the 13 Trading gateways. They remain outside
the Hermes deploy and health scope.
