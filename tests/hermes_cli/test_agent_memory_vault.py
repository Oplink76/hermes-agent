"""Contracts for the external, append-only Agent Memory vault."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from hermes_cli.agent_memory_vault import (
    SessionGist,
    append_gist,
    configured_vault_path,
    initialize_vault,
    lint_vault,
    recall,
)


def _gist(gist_id="gist-1", *, title="Export release evidence"):
    return SessionGist(
        gist_id=gist_id,
        occurred_at=datetime(2026, 7, 18, 14, 5),
        agent_id="codex",
        role="developer",
        function_id="function-release-export",
        title=title,
        context="board=product; card=42; project=hermes; repository=/repo",
        summary="Added a portable release-evidence export.",
        reused="none",
        result="The export is ready for review.",
        maturity="code_complete",
        evidence="commit abc123; tests: focused suite",
        behavior="none",
        decisions="Use Markdown as the source of truth.",
        open_loops="Review and release remain.",
    )


def test_configured_vault_path_prefers_environment_and_requires_enabled_config(tmp_path):
    configured = tmp_path / "configured"
    from_environment = tmp_path / "environment"

    assert configured_vault_path(
        {"agent_memory": {"enabled": False, "vault_path": str(configured)}}
    ) is None
    assert configured_vault_path(
        {"agent_memory": {"enabled": True, "vault_path": str(configured)}}
    ) == configured
    assert configured_vault_path(
        {"agent_memory": {"enabled": False, "vault_path": str(configured)}},
        {"HERMES_AGENT_MEMORY_VAULT": str(from_environment)},
    ) == from_environment


def test_initialize_vault_creates_only_the_documented_external_structure(tmp_path):
    vault = tmp_path / "Agent Memory"

    initialize_vault(vault)

    assert {path.relative_to(vault).as_posix() for path in vault.rglob("*")} == {
        ".derived",
        ".derived/functions.json",
        "agents.md",
        "index.md",
        "log.md",
        "memory",
        "raw",
        "snapshot.md",
        "wiki",
        "wiki/functions",
        "wiki/learnings",
    }


def test_append_writes_the_structured_gist_once_and_redacts_recorded_text(tmp_path):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    gist = _gist()
    gist.evidence = "token ghp_abcdefghijklmnopqrstuvwxyz1234567890"

    assert append_gist(vault, gist) is True
    assert append_gist(vault, gist) is False

    daily_history = (vault / "memory" / "2026-07-18.md").read_text(encoding="utf-8")
    assert daily_history.count("<!-- gist_id: gist-1 -->") == 1
    assert "## 14:05 | codex | developer" in daily_history
    assert "- Function: function-release-export | Export release evidence" in daily_history
    assert "- Summary: Added a portable release-evidence export." in daily_history
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in daily_history
    assert "ghp_" in daily_history


def test_append_rejects_a_secret_bearing_gist_id_before_creating_the_vault(tmp_path):
    vault = tmp_path / "Agent Memory"
    gist = _gist("ghp_abcdefghijklmnopqrstuvwxyz1234567890")

    with pytest.raises(ValueError, match="opaque"):
        append_gist(vault, gist)

    assert not vault.exists()


def test_append_scrubs_credential_query_values_but_preserves_normal_evidence_urls(tmp_path):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    gist = _gist("gist-credential-url")
    gist.evidence = (
        "credential https://ci.example.test/builds/42?access_token=top-secret "
        "normal https://ci.example.test/builds/43?job=deploy"
    )

    assert append_gist(vault, gist) is True

    history = (vault / "memory" / "2026-07-18.md").read_text(encoding="utf-8")
    assert "access_token=top-secret" not in history
    assert "access_token=«redacted-secret»" in history
    assert "https://ci.example.test/builds/43?job=deploy" in history


@pytest.mark.parametrize("query_key", ["refresh_token", "client_secret", "api_key", "signature"])
def test_append_scrubs_common_credential_query_keys(tmp_path, query_key):
    vault = tmp_path / "Agent Memory"
    gist = _gist(f"gist-{query_key}")
    gist.evidence = f"https://ci.example.test/builds/42?{query_key}=top-secret"

    assert append_gist(vault, gist) is True

    history = (vault / "memory" / "2026-07-18.md").read_text(encoding="utf-8")
    assert f"{query_key}=top-secret" not in history
    assert f"{query_key}=«redacted-secret»" in history


def test_recall_returns_related_functionality_with_capped_evidence(tmp_path):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    append_gist(vault, _gist())
    append_gist(vault, _gist("gist-2", title="Unrelated terminal colors"))

    matches = recall(vault, "release evidence export", limit=1)

    assert len(matches) == 1
    assert matches[0].function_id == "function-release-export"
    assert matches[0].title == "Export release evidence"
    assert "commit abc123" in matches[0].evidence
    assert len(matches[0].snippet) <= 500


def test_recall_does_not_read_daily_files_older_than_the_recent_scan_bound(tmp_path, monkeypatch):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    memory = vault / "memory"
    oldest = memory / "2026-01-01.md"
    oldest.write_text("old history must not be read", encoding="utf-8")
    for day in range(2, 32):
        (memory / f"2026-01-{day:02d}.md").write_text("", encoding="utf-8")
    recent = _gist("recent-gist")
    recent.occurred_at = datetime(2026, 2, 1, 9, 0)
    assert append_gist(vault, recent) is True

    original_read_text = Path.read_text

    def reject_oldest_read(path, *args, **kwargs):
        if path == oldest:
            raise AssertionError("recall read history outside its recent-file bound")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_oldest_read)

    assert recall(vault, "release evidence")


def test_lint_rebuilds_derived_files_without_rewriting_daily_history(tmp_path):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    append_gist(vault, _gist())
    history_path = vault / "memory" / "2026-07-18.md"
    before = history_path.read_bytes()

    report = lint_vault(vault)
    first_index = (vault / ".derived" / "functions.json").read_text(encoding="utf-8")
    second_report = lint_vault(vault)
    second_index = (vault / ".derived" / "functions.json").read_text(encoding="utf-8")

    assert report.valid_entries == second_report.valid_entries == 1
    assert report.invalid_entries == second_report.invalid_entries == 0
    assert history_path.read_bytes() == before
    assert first_index == second_index
    assert json.loads(first_index)["functions"] == [
        {
            "evidence": "commit abc123; tests: focused suite",
            "function_id": "function-release-export",
            "last_gist_id": "gist-1",
            "maturity": "code_complete",
            "title": "Export release evidence",
        }
    ]
