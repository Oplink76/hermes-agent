# Platform hermeticity report

Status: **DONE**

## Scope

Diagnosed and fixed the reproducible macOS/platform-dependent failures in:

- `tests/gateway/test_background_command.py`
- `tests/hermes_cli/test_gateway_wsl.py`
- `tests/hermes_cli/test_gateway_service.py`
- `tests/test_live_system_guard_self_test.py`
- `tests/hermes_cli/test_service_manager.py`
- `tests/tools/test_file_tools.py`

No live service, gateway, or systemd mutation was performed. No production
behavior was changed.

## Root causes and fixes

1. **Canonical macOS temp paths**
   - `validate_media_delivery_path()` and the write/patch path resolver
     intentionally canonicalize absolute paths.
   - On macOS, lexical `/tmp/...` becomes `/private/tmp/...`.
   - The background-media and file-tool tests asserted the lexical input path
     even though the production contract is the canonical path.
   - Fixed the assertions to compare with `realpath()` / `Path.resolve()`.

2. **WSL tests leaked host command discovery**
   - `supports_systemd_services()` correctly requires `systemctl` to exist
     before evaluating WSL/systemd state.
   - The tests mocked Linux/WSL detection but left `shutil.which("systemctl")`
     dependent on the macOS host.
   - Fixed the three affected cases by providing a hermetic `systemctl` lookup.

3. **Systemd restart unit tests leaked user D-Bus state**
   - Four restart-routing tests mocked their systemctl operations but did not
     mock `_preflight_user_systemd()`.
   - On a non-systemd host, the preflight correctly raised before the behavior
     under test was reached.
   - Fixed each focused test by mocking only that preflight seam, matching the
     existing pattern in nearby service tests.

4. **Live-system guard self-tests required a real Linux executable**
   - Four pass-through tests called the host's `systemctl` merely to prove the
     guard did not reject read-only argv.
   - macOS has no `systemctl`, so the guard passed the command and the OS then
     raised `FileNotFoundError`.
   - Fixed the tests by keeping `systemctl` in argv for guard inspection while
     using `sys.executable` as the harmless executable. This tests the guard
     without probing or mutating host services.

5. **Darwin setgid filesystem behavior**
   - The s6 skeleton tests require mode `03730` on Linux, where the service is
     deployed.
   - Darwin strips the setgid bit from these pytest temp directories and
     reports `01730`; changing ownership/order did not alter that behavior.
   - Fixed the assertions to retain the exact `03730` Linux contract while
     expecting Darwin's precise `01730` result. The tests still validate all
     directory/FIFO creation and permission bits; nothing is skipped.

## Red/green evidence

Initial combined RED run through `scripts/run_tests.sh`:

- 365 passed, 16 failed.
- Failures by file: 1 background media, 2 WSL, 4 gateway service, 4 guard
  self-test, 2 service manager, 3 file tools.

Intermediate run after the first five fixes:

- 379 passed, 2 failed.
- The two remaining failures isolated Darwin's setgid behavior and disproved
  the tentative production-order hypothesis; that production edit was
  reverted.

Focused service-manager GREEN run:

- 66 passed, 0 failed.

Final combined GREEN run:

```text
scripts/run_tests.sh -j 6 \
  tests/gateway/test_background_command.py \
  tests/hermes_cli/test_gateway_wsl.py \
  tests/hermes_cli/test_gateway_service.py \
  tests/test_live_system_guard_self_test.py \
  tests/hermes_cli/test_service_manager.py \
  tests/tools/test_file_tools.py -q

381 passed, 0 failed
```

Ruff:

```text
$HOME/.hermes/hermes-agent/.venv/bin/python -m ruff check <six changed Python files>
All checks passed!
```

`git diff --check` also passed.

## Self-review

- Changes are limited to the six failing test files plus this report.
- No skip markers or blanket platform exclusions were added.
- Linux systemd and s6 contracts remain asserted.
- Path assertions now match the existing canonical-path security behavior.
- Guard tests cannot reach a live system service.
- No concerns remain.
