from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_web_server_uses_packaged_status_module() -> None:
    source = (REPO_ROOT / "hermes_cli" / "web_server.py").read_text(encoding="utf-8")

    assert "ops.cloudadvisor" not in source
    assert "hermes_cli.upstream_sync_status" in source


def test_built_wheel_can_import_status_schema_without_source_tree(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "wheel"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    probe = (
        "import sys;"
        f"sys.path.insert(0, {str(wheels[0])!r});"
        "from hermes_cli.upstream_sync_status import SyncStatus;"
        "assert SyncStatus.__module__ == 'hermes_cli.upstream_sync_status'"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    imported = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert imported.returncode == 0, imported.stderr
