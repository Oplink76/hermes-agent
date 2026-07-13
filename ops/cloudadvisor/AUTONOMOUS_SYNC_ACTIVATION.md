# Autonomous upstream-sync activation checklist

This checklist changes production only after the implementation merge SHA has
passed branch protection and the exact SHA is deployed and healthy. The steps
are deliberately separate from tests: repository verification never writes live
cron, launchd, service, or installation state.

1. Verify the deployed `origin/main`, installed `HEAD`, and approved bootstrap
   merge SHA are identical. Run the configured `health` action and retain its
   green evidence.
2. Before changing the wrapper, record the live operations-config SHA-256 and
   run the versioned migration into an operator-owned rollback directory:

   ```bash
   python -m ops.cloudadvisor.hermes_ops.config_migration migrate \
     --config /Users/cloudadvisor/.hermes/operations/hermes-operations.yaml \
     --backup-dir /Users/cloudadvisor/.hermes/recovery/activation-config-backup
   ```

   Retain the emitted original/migrated checksums, exact backup, and rollback
   manifest. Confirm the Package-1 preservation command, `uv_extras: [all, dev,
   slack]`, runtime/services, and unrelated values are unchanged. Never copy
   `hermes-operations.example.yaml`; its `/usr/bin/false` is intentionally a
   fail-closed placeholder.
3. Still before changing the wrapper, run the read-only activation gate and
   retain its JSON evidence:

   ```bash
   python -m ops.cloudadvisor.hermes_ops.cli sync-auto \
     --config /Users/cloudadvisor/.hermes/operations/hermes-operations.yaml \
     --preflight
   ```

   Stop unless every config, executable, repo/remote, state-path, Package-1,
   runtime-scope, GitHub CLI, Codex, Claude, and delivery check is green. This
   command must not create the receipt/status/outbox paths or touch locks,
   branches, services, or deployments.
4. Record SHA-256 checksums of the live wrapper and the repository-owned
   `ops/cloudadvisor/upstream-sync.sh`. Copy the live wrapper to an operator-owned
   rollback path before replacement.
5. Install the repository-owned wrapper atomically at
   `/Users/cloudadvisor/.hermes/scripts/upstream-sync.sh`; preserve executable
   mode and owner. Do not edit the embedded production paths during activation.
6. Confirm the 06:00 and 18:00 Europe/Copenhagen jobs invoke the wrapper with its
   default `sync-auto` action and keep its existing scheduler-level
   `deliver=slack:C0BFLTFC2LS`. Direct delivery success is silent; this scheduler
   target is used only when the no-agent wrapper exits nonzero with a concise
   safe failure. Do not change it to `local`: Hermes uses `local` as the
   no-delivery sentinel. Keep the existing health job and its scheduler delivery
   by invoking the same wrapper with `health`.
7. Run the installed wrapper immediately. Routine `NO_CHANGE`, `DEPLOYED`,
   `ROLLED_BACK_REVERTED`, `PENDING_REFRESH`, and `LOCKED` results produce no
   stdout, as does a successfully delivered `notify_ole=true` decision. The
   wrapper sends the matching content-addressed `Approve / Wait / Details`
   handoff directly and acknowledges it only after Slack succeeds.
8. Verify the status file, notification request state, delivery fingerprint,
   decision packet, outbox acknowledgement, installed SHA, fork `main`, and
   runtime health. Simulate one delivery failure: the outbox must remain pending
   and the next run must retry the same decision id. The failed wrapper must
   expose only the safe scheduler fallback, never raw command stdout/stderr or
   secrets.
9. If config migration, preflight, parsing, packet verification, schedule
   verification, or runtime health fails, restore the wrapper rollback copy and
   restore the config only through its checksum-bound manifest:

   ```bash
   python -m ops.cloudadvisor.hermes_ops.config_migration rollback \
     --manifest /operator-owned/path/to/hermes-operations.yaml.sync-v1.HASH.rollback.json
   ```

   Re-run `health` and the read-only preflight. Preserve all before/after
   checksums and command results in the recovery record. Never reset or push
   protected `main`.

Activation does not start or load the 13 Trading gateways. They remain outside
the Hermes deploy and health scope.
