"""Cron module paths must follow the hermetic per-test HERMES_HOME."""

import os
from pathlib import Path

import cron.jobs as jobs_mod


def test_cron_module_paths_follow_test_hermes_home():
    hermes_home = Path(os.environ["HERMES_HOME"]).resolve()
    cron_dir = hermes_home / "cron"

    assert jobs_mod.HERMES_DIR == hermes_home
    assert jobs_mod.CRON_DIR == cron_dir
    assert jobs_mod.JOBS_FILE == cron_dir / "jobs.json"
    assert jobs_mod.TICKER_HEARTBEAT_FILE == cron_dir / "ticker_heartbeat"
    assert jobs_mod.TICKER_SUCCESS_FILE == cron_dir / "ticker_last_success"
    assert jobs_mod.OUTPUT_DIR == cron_dir / "output"
