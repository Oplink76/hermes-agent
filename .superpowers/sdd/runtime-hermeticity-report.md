# Runtime hermeticity diagnosis

Status: DONE

## Scope and safety

Diagnosed the five requested test files in the isolated worktree only. All
process and signal tests targeted subprocesses created by the test process.
No live Hermes, Claude, Trading, gateway, launchd, or systemd process was
restarted, signalled, or killed.

## Reproduction

Initial combined run:

```text
scripts/run_tests.sh tests/tools/test_execute_code_approval_cluster.py \
  tests/gateway/test_shutdown_forensics.py \
  tests/gateway/test_startup_restart_race.py \
  tests/hermes_cli/test_signal_handler_kanban_worker.py \
  tests/tools/test_base_environment.py -q

77 passed, 10 failed
```

The first combined run exposed seven approval failures plus one failure in
each gateway file and the signal test. The BaseEnvironment concurrency test
passed once, then reproduced its tear on the next isolated run (28 passed,
1 failed), confirming it was stochastic rather than healthy.

## Root causes and fixes

### Import-time approval state leaked from the real HOME

`tools.approval` is imported during test collection, before the per-test
fixture assigns a temporary `HERMES_HOME`. On this machine the real config has
`execute_code` permanently approved, so the decision matrix short-circuited
before gateway approve/deny/timeout and smart-mode branches ran.

The `gw_session` fixture now clears and later restores the import-time
`execute_code` permanent approval, clears session state, and forces the frozen
YOLO flag off. Production approval behavior is unchanged.

### Shutdown diagnostics depended on GNU `timeout`

`spawn_async_diagnostic()` launched `timeout ... bash -c ...`. Stock macOS has
no GNU `timeout`, so `Popen` raised `FileNotFoundError` and the function returned
`None`.

The detached helper now uses the current Python interpreter and
`subprocess.run(..., timeout=...)`, preserving the bounded fire-and-forget
contract without a platform-specific executable.

### Startup race fixture lagged the async SessionStore boundary

The hand-built `GatewayRunner` fixture only supplied a synchronous MagicMock
session store. Current production wraps it in `AsyncSessionStore` and offloads
calls through `asyncio.to_thread`; under the test's two-second wall timeout,
the synthetic startup was cancelled before reaching the race under test.

The fixture now supplies an async-store double tied to the same sync store.
The timeout is five seconds: still bounded, but no longer conflates ordinary
startup/import variance with a restart-race deadlock.

### macOS zombie detection in the signal test

The synthetic worker correctly called `os._exit(0)`, but the parent did not
reap it while polling. Linux had a `/proc` zombie check; macOS fell through to
`os.kill(pid, 0)`, which reports zombies as existing.

The helper now performs non-blocking `waitpid` for its own direct child before
the liveness probe. This tests termination rather than unreaped process-table
state.

### `$BASHPID` is unavailable in Apple bash 3.2

The atomic environment snapshot used `<snapshot>.tmp.$BASHPID`. Apple bash
3.2 leaves `BASHPID` empty, so concurrent writers all used the same temp file.
They could overwrite or move one another's partial dump, reproducing the PATH
corruption the atomic rename was meant to prevent.

Both bootstrap and update paths now allocate same-directory unique files with
`mktemp <snapshot>.tmp.XXXXXX`, then atomically rename them. Tests cover the
portable contract, quoted paths with spaces, failed writes, metadata modes,
and concurrent readers/writers.

## Red/green evidence

- Baseline: 10 failures across four files; approval decisions visibly returned
  the real allowlist short-circuit result.
- BaseEnvironment isolated rerun: concurrency behavioral test failed with
  `snapshot tore`; Apple bash reported an empty `BASHPID`.
- Updated mktemp contract tests were run before the production edit and failed
  in all three expected assertions.
- After the production/test isolation changes, the five-file run passed 87/87.

## Verification

```text
scripts/run_tests.sh <all five requested files> -q
87 passed, 0 failed

ruff check <six touched Python files>
All checks passed!

git diff --check
clean
```

The known third-party `pkg_resources` deprecation warnings in the startup race
file remain unchanged and are not related to these fixes.
