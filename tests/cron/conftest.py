"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch, _hermetic_environment):
    """Pin cron tests to a model and the hermetic per-test state directory."""
    from cron import jobs as jobs_mod

    hermes_home = Path(os.environ["HERMES_HOME"]).resolve()
    cron_dir = hermes_home / "cron"

    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(
        jobs_mod,
        "_IMPORT_STORE",
        jobs_mod._CronStorePaths(
            cron_dir,
            cron_dir / "jobs.json",
            cron_dir / "output",
        ),
    )
    monkeypatch.setattr(
        jobs_mod,
        "TICKER_HEARTBEAT_FILE",
        cron_dir / "ticker_heartbeat",
    )
    monkeypatch.setattr(
        jobs_mod,
        "TICKER_SUCCESS_FILE",
        cron_dir / "ticker_last_success",
    )
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", cron_dir / "output")
    yield
