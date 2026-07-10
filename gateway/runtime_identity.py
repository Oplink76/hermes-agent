"""Observable, process-owned identity for a running Hermes gateway."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import _get_platform_default_hermes_home


@dataclass(frozen=True)
class RuntimeIdentity:
    source_sha: str
    executable: str
    python_version: str
    pid: int
    ppid: int
    profile: str
    started_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuntimeIdentity":
        return cls(
            source_sha=str(payload["source_sha"]),
            executable=str(payload["executable"]),
            python_version=str(payload["python_version"]),
            pid=int(payload["pid"]),
            ppid=int(payload["ppid"]),
            profile=str(payload["profile"]),
            started_at=str(payload["started_at"]),
        )


def _process_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return _get_platform_default_hermes_home().resolve(strict=False)


def runtime_identity_path(hermes_home: Path | None = None) -> Path:
    home = (
        Path(hermes_home).expanduser().resolve(strict=False)
        if hermes_home is not None
        else _process_hermes_home()
    )
    return home / "runtime" / "gateway.json"


def _source_sha(source_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    sha = (completed.stdout or "").strip()
    return sha if completed.returncode == 0 and sha else "unknown"


def _active_profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def capture_runtime_identity(
    *,
    source_root: Path | None = None,
    profile: str | None = None,
    started_at: datetime | None = None,
) -> RuntimeIdentity:
    root = (
        Path(source_root).expanduser().resolve(strict=False)
        if source_root is not None
        else Path(__file__).resolve().parent.parent
    )
    captured_at = started_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    return RuntimeIdentity(
        source_sha=_source_sha(root),
        executable=str(Path(sys.executable).resolve()),
        python_version=platform.python_version(),
        pid=os.getpid(),
        ppid=os.getppid(),
        profile=(profile or _active_profile()),
        started_at=captured_at.astimezone(timezone.utc).isoformat(),
    )


def write_runtime_identity(
    identity: RuntimeIdentity,
    *,
    hermes_home: Path | None = None,
) -> Path:
    """Atomically write ``identity`` with owner-only permissions."""
    path = runtime_identity_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(identity.to_dict(), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def read_runtime_identity(
    *,
    hermes_home: Path | None = None,
    path: Path | None = None,
) -> RuntimeIdentity | None:
    identity_path = (
        Path(path) if path is not None else runtime_identity_path(hermes_home)
    )
    try:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return RuntimeIdentity.from_dict(payload)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None


def remove_runtime_identity(
    *,
    hermes_home: Path | None = None,
    pid: int | None = None,
) -> bool:
    """Remove the manifest only when it still belongs to ``pid``."""
    path = runtime_identity_path(hermes_home)
    identity = read_runtime_identity(path=path)
    owner_pid = os.getpid() if pid is None else int(pid)
    if identity is None or identity.pid != owner_pid:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
