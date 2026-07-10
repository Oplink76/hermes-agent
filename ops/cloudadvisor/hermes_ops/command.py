"""Injected command execution boundary for Hermes operations."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


class CommandRunner(Protocol):
    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessCommandRunner:
    def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
