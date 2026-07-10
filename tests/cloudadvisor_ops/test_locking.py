from __future__ import annotations

import os
from pathlib import Path

import pytest

from ops.cloudadvisor.hermes_ops import locking


@pytest.mark.skipif(os.name == "nt", reason="Test patches the POSIX flock backend")
def test_file_lock_propagates_unexpected_operating_system_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def denied(*args, **kwargs):
        raise PermissionError("lock permission denied")

    monkeypatch.setattr(locking.fcntl, "flock", denied)

    with pytest.raises(PermissionError, match="permission denied"):
        with locking.try_exclusive_file_lock(tmp_path / "operation.lock"):
            raise AssertionError("unreachable")
