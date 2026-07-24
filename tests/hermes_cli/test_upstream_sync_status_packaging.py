from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_web_server_status_path_does_not_require_operations_package() -> None:
    probe = f"""
import importlib.abc
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, {str(REPO_ROOT)!r})

class BlockOperationsPackage(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ops.cloudadvisor" or fullname.startswith("ops.cloudadvisor."):
            raise ImportError(f"blocked operations import: {{fullname}}")
        return None

sys.meta_path.insert(0, BlockOperationsPackage())
from hermes_cli import web_server

root = Path(tempfile.mkdtemp())
web_server._upstream_sync_status_path = lambda: root / "missing-status.json"
web_server._upstream_sync_receipt_root = lambda: root / "missing-receipts"
payload = web_server._with_upstream_sync_status({{"behind": 0}})
assert payload["sync_state"] is None
"""
    imported = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert imported.returncode == 0, imported.stderr


def test_nix_wheel_can_import_status_schema_without_source_tree(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    for name in ("setup.py", "pyproject.toml", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, source_root / name)
    shutil.copytree(REPO_ROOT / "hermes_cli", source_root / "hermes_cli")

    wheel_dir = tmp_path / "wheel"
    env = dict(os.environ)
    env["HERMES_NIX_BUILD"] = "1"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), "."],
        cwd=source_root,
        capture_output=True,
        text=True,
        env=env,
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
    imported_env = {
        key: value for key, value in os.environ.items() if key != "PYTHONPATH"
    }
    imported = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=imported_env,
        timeout=60,
    )

    assert imported.returncode == 0, imported.stderr
