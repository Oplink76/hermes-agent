"""Tests for the canonical shell test runner's virtualenv selection."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _fake_venv(root: Path, label: str) -> None:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activate").write_text("# test marker\n", encoding="utf-8")
    python = bin_dir / "python"
    python.write_text(f"#!/bin/sh\necho {label}\n", encoding="utf-8")
    python.chmod(0o755)


def test_linked_worktree_prefers_authoritative_home_dotvenv(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.sh"
    shutil.copy2(source, scripts / "run_tests.sh")

    home = tmp_path / "home"
    install = home / ".hermes" / "hermes-agent"
    _fake_venv(install / ".venv", "AUTHORITATIVE_DOTVENV")
    _fake_venv(install / "venv", "LEGACY_VENV")

    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "AUTHORITATIVE_DOTVENV" in result.stdout
    assert "LEGACY_VENV" not in result.stdout


def test_checkout_legacy_venv_precedes_installed_home_dotvenv(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.sh"
    shutil.copy2(source, scripts / "run_tests.sh")
    _fake_venv(repo / "venv", "CHECKOUT_LEGACY_VENV")

    home = tmp_path / "home"
    install = home / ".hermes" / "hermes-agent"
    _fake_venv(install / ".venv", "INSTALLED_DOTVENV")

    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "CHECKOUT_LEGACY_VENV" in result.stdout
    assert "INSTALLED_DOTVENV" not in result.stdout
