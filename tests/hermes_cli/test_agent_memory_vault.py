"""Contracts for the external, append-only Agent Memory vault."""

import json
from datetime import datetime

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
