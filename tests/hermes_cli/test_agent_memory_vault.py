"""Contracts for the external, append-only Agent Memory vault."""

import hashlib
import json
import multiprocessing
from datetime import datetime
from pathlib import Path

import pytest

from hermes_cli import agent_memory_vault as memory_vault
from hermes_cli.agent_memory_vault import (
    ExecutorIdentity,
    SessionGist,
    append_gist,
    configured_vault_path,
    initialize_vault,
    lint_vault,
    recall,
)


def _gist(gist_id="gist-1", *, title="Export release evidence", executor=None):
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
        executor=executor,
    )


def _manual_gist(
    *,
    gist_id="manual-gist",
    function_id="function-manual",
    maturity="planned",
    summary="A bounded manual summary.",
    evidence="commit abc1234",
):
    return f"""## 12:00 | manual | worker
<!-- gist_id: {gist_id} -->
- Function: {function_id} | Manual function
- Context: board=manual; card=1
- Summary: {summary}
- Reused: none
- Result: recorded
- Maturity: {maturity}
- Evidence: {evidence}
- Behavior: none
- Decisions: none
- Open loops: none
"""


def _concurrent_record(vault_value, index, duplicate, barrier):
    from hermes_cli.agent_memory_vault import append_gist, lint_vault

    vault = Path(vault_value)
    gist_id = "shared-idempotent-gist" if duplicate else f"distinct-gist-{index}"
    function_id = "function-shared" if duplicate else f"function-distinct-{index}"
    gist = _gist(gist_id, title=f"Concurrent function {function_id}")
    gist.function_id = function_id
    barrier.wait(timeout=10)
    append_gist(vault, gist)
    lint_vault(vault)


def _hold_vault_lock(vault_value, ready, release):
    from hermes_cli.agent_memory_vault import _vault_lock

    with _vault_lock(Path(vault_value)):
        ready.set()
        release.wait(timeout=10)


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


def test_configured_vault_path_rejects_relative_environment_and_config_paths():
    assert (
        configured_vault_path({}, {"HERMES_AGENT_MEMORY_VAULT": "relative/vault"})
        is None
    )
    assert configured_vault_path(
        {"agent_memory": {"enabled": True, "vault_path": "relative/vault"}},
        {},
    ) is None


def test_initialize_vault_creates_only_the_documented_external_structure(tmp_path):
    vault = tmp_path / "Agent Memory"

    initialize_vault(vault)

    assert {path.relative_to(vault).as_posix() for path in vault.rglob("*")} == {
        ".derived",
        ".derived/agent-memory.lock",
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
    assert not vault.with_name(vault.name + ".lock").exists()


def test_initialize_vault_twice_preserves_generated_content(tmp_path):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    before = {
        path.relative_to(vault).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in vault.rglob("*")
        if path.is_file()
    }

    initialize_vault(vault)

    after = {
        path.relative_to(vault).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in vault.rglob("*")
        if path.is_file()
    }
    assert after == before


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


def test_executor_metadata_is_optional_and_canonical(tmp_path):
    vault = tmp_path / "Agent Memory"
    assert append_gist(vault, _gist(gist_id="old-format")) is True
    assert append_gist(
        vault,
        _gist(
            gist_id="worker-format",
            executor=ExecutorIdentity(
                agent_id="codex",
                model="gpt-5.5",
                surface="codex-cli",
                hermes_role="developer",
                execution_id="exec-123",
                responsibility="writer",
            ),
        ),
    ) is True
    report = lint_vault(vault)
    history = (vault / "memory" / "2026-07-18.md").read_text()
    assert report.valid_entries == 2
    assert '- Executor: {"agent_id":"codex"' in history


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("surface", "terminal"),
        ("responsibility", "observer"),
        ("agent_id", "x" * 2_001),
        ("model", "x" * 2_001),
        ("hermes_role", "x" * 2_001),
        ("execution_id", "x" * 2_001),
        ("model", "chain of thought"),
        ("hermes_role", "full transcript"),
        ("execution_id", "ghp_abcdefghijklmnopqrstuvwxyz1234567890"),
    ),
)
def test_executor_metadata_rejects_unsupported_unsafe_or_secret_values(
    tmp_path, field, value
):
    vault = tmp_path / "Agent Memory"
    values = {
        "agent_id": "codex",
        "model": "test-model",
        "surface": "codex-cli",
        "hermes_role": "developer",
        "execution_id": "exec-123",
        "responsibility": "writer",
    }
    values[field] = value

    with pytest.raises(ValueError):
        append_gist(vault, _gist(executor=ExecutorIdentity(**values)))

    assert not list((vault / "memory").glob("*.md"))


@pytest.mark.parametrize(
    "surface",
    ("hermes-direct", "hermes-child", "codex-cli", "claude-code-cli", "cowork-mcp"),
)
def test_executor_metadata_accepts_each_governed_surface(tmp_path, surface):
    identity = ExecutorIdentity(
        agent_id="codex",
        model="test-model",
        surface=surface,
        hermes_role="developer",
        execution_id=f"exec-{surface}",
        responsibility="reviewer",
    )
    assert append_gist(tmp_path / surface, _gist(executor=identity)) is True


def test_manual_gist_with_duplicate_executor_fields_is_invalid(tmp_path):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    executor = ExecutorIdentity(
        agent_id="codex",
        model="test-model",
        surface="codex-cli",
        hermes_role="developer",
        execution_id="exec-123",
        responsibility="writer",
    ).canonical_json()
    manual = _manual_gist().replace(
        "- Context:", f"- Executor: {executor}\n- Executor: {executor}\n- Context:"
    )
    (vault / "memory" / "2026-07-18.md").write_text(manual, encoding="utf-8")

    report = lint_vault(vault)

    assert report.valid_entries == 0
    assert report.invalid_entries == 1


def test_append_rejects_duplicate_executor_fields_before_writing_history(tmp_path):
    class DuplicateExecutor(ExecutorIdentity):
        def canonical_json(self):
            canonical = super().canonical_json()
            return f"{canonical}\n- Executor: {canonical}"

    vault = tmp_path / "Agent Memory"
    executor = DuplicateExecutor(
        agent_id="codex",
        model="test-model",
        surface="codex-cli",
        hermes_role="developer",
        execution_id="exec-123",
        responsibility="writer",
    )

    with pytest.raises(ValueError, match="schema"):
        append_gist(vault, _gist(executor=executor))

    assert not list((vault / "memory").glob("*.md"))


def test_append_rejects_a_secret_bearing_gist_id_before_creating_the_vault(tmp_path):
    vault = tmp_path / "Agent Memory"
    gist = _gist("ghp_abcdefghijklmnopqrstuvwxyz1234567890")

    with pytest.raises(ValueError, match="opaque"):
        append_gist(vault, gist)

    assert not vault.exists()


@pytest.mark.parametrize(
    "changes",
    (
        {"maturity": "experimental"},
        {"function_id": "malformed function"},
        {"function_id": "f" * 200, "title": "x" * 1_900},
    ),
    ids=("maturity", "function-id", "combined-function-field"),
)
def test_append_rejects_rendered_gists_that_fail_the_reader_schema(
    tmp_path, changes
):
    vault = tmp_path / "Agent Memory"
    gist = _gist("invalid-rendered-gist")
    for name, value in changes.items():
        setattr(gist, name, value)

    with pytest.raises(ValueError, match="schema"):
        append_gist(vault, gist)

    assert not list((vault / "memory").glob("*.md"))


def test_every_successful_append_is_lint_valid_and_recallable(tmp_path):
    vault = tmp_path / "Agent Memory"
    gist = _gist("writer-reader-invariant")

    assert append_gist(vault, gist) is True

    report = lint_vault(vault)
    matches = recall(vault, gist.gist_id)
    assert report.valid_entries == 1
    assert report.invalid_entries == 0
    assert [match.gist_id for match in matches] == [gist.gist_id]


def test_writer_allows_ordinary_functional_intent_language(tmp_path):
    vault = tmp_path / "Agent Memory"
    gist = _gist(
        "ordinary-functional-language",
        title="Improve agent reasoning and Transcript export",
    )
    gist.result = "Functional boundary: Do not store transcripts; Deliberation controls."

    assert append_gist(vault, gist) is True

    report = lint_vault(vault)
    matches = recall(vault, "agent reasoning transcript export deliberation controls")
    assert report.valid_entries == 1
    assert report.invalid_entries == 0
    assert [match.gist_id for match in matches] == [gist.gist_id]


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
    assert "https://ci.example.test/builds/42?[REDACTED]" in history
    assert "https://ci.example.test/builds/43?[REDACTED]" in history


@pytest.mark.parametrize("query_key", ["refresh_token", "client_secret", "api_key", "signature"])
def test_append_scrubs_common_credential_query_keys(tmp_path, query_key):
    vault = tmp_path / "Agent Memory"
    gist = _gist(f"gist-{query_key}")
    gist.evidence = f"https://ci.example.test/builds/42?{query_key}=top-secret"

    assert append_gist(vault, gist) is True

    history = (vault / "memory" / "2026-07-18.md").read_text(encoding="utf-8")
    assert f"{query_key}=top-secret" not in history
    assert "https://ci.example.test/builds/42?[REDACTED]" in history


@pytest.mark.parametrize(
    ("url", "redacted_url"),
    [
        (
            "https://ci.example.test/callback?id_token=top-secret&session=active&code=oauth-code",
            "https://ci.example.test/callback?[REDACTED]",
        ),
        (
            "https://ci.example.test/callback#access_token=top-secret&state=active",
            "https://ci.example.test/callback#[REDACTED]",
        ),
        (
            "https://build-user:top-secret@ci.example.test/builds/42",
            "https://«redacted-secret»@ci.example.test/builds/42",
        ),
    ],
)
def test_append_redacts_every_http_url_secret_surface(tmp_path, url, redacted_url):
    vault = tmp_path / "Agent Memory"
    gist = _gist("gist-url-surface")
    gist.evidence = url

    assert append_gist(vault, gist) is True

    history = (vault / "memory" / "2026-07-18.md").read_text(encoding="utf-8")
    assert "top-secret" not in history
    assert redacted_url in history


@pytest.mark.parametrize(
    ("url", "redacted_url"),
    [
        ("https://ci.example.test/callback?top-secret", "https://ci.example.test/callback?[REDACTED]"),
        ("https://ci.example.test/callback#top-secret", "https://ci.example.test/callback#[REDACTED]"),
    ],
)
def test_append_redacts_bare_http_url_query_and_fragment_components(tmp_path, url, redacted_url):
    vault = tmp_path / "Agent Memory"
    gist = _gist("gist-bare-url-component")
    gist.evidence = url

    assert append_gist(vault, gist) is True

    history = (vault / "memory" / "2026-07-18.md").read_text(encoding="utf-8")
    assert "top-secret" not in history
    assert redacted_url in history


@pytest.mark.parametrize(
    ("url", "redacted_url"),
    (
        (
            "wss://socket-user:top-secret@socket.example/ws?token=hidden#session",
            "wss://«redacted-secret»@socket.example/ws?[REDACTED]#[REDACTED]",
        ),
        (
            "ftp://ftp-user:top-secret@files.example/archive?token=hidden#part",
            "ftp://«redacted-secret»@files.example/archive?[REDACTED]#[REDACTED]",
        ),
        (
            "postgresql://db-user:top-secret@db.example/app?sslpassword=hidden#dsn",
            "postgresql://«redacted-secret»@db.example/app?[REDACTED]#[REDACTED]",
        ),
    ),
)
def test_append_redacts_url_secrets_across_schemes(tmp_path, url, redacted_url):
    vault = tmp_path / "Agent Memory"
    gist = _gist("gist-cross-scheme-url")
    gist.evidence = url

    assert append_gist(vault, gist) is True

    history = (vault / "memory" / "2026-07-18.md").read_text(encoding="utf-8")
    assert "top-secret" not in history
    assert "hidden" not in history
    assert redacted_url in history


@pytest.mark.parametrize(
    ("url", "redacted_url"),
    (
        (
            "custom://opaquecredentialvalue12345@custom.example/archive",
            "custom://«redacted-secret»@custom.example/archive",
        ),
        (
            "wss://opaquecredentialvalue12345@socket.example/ws",
            "wss://«redacted-secret»@socket.example/ws",
        ),
    ),
)
def test_append_redacts_all_bare_url_userinfo_without_leaving_fragments(
    tmp_path, url, redacted_url
):
    vault = tmp_path / "Agent Memory"
    gist = _gist("gist-bare-userinfo")
    gist.evidence = url

    assert append_gist(vault, gist) is True

    history = (vault / "memory" / "2026-07-18.md").read_text(encoding="utf-8")
    assert "opaquecredentialvalue12345" not in history
    assert "opaque" not in history
    assert "12345" not in history
    assert redacted_url in history


@pytest.mark.parametrize(
    "manual_entry",
    (
        _manual_gist(gist_id="malformed/id"),
        _manual_gist(function_id="malformed function"),
        _manual_gist(maturity="experimental"),
        _manual_gist(evidence="wss://user:top-secret@example.test/ws?token=hidden"),
        _manual_gist(summary="x" * 2_001),
    ),
)
def test_manual_malformed_or_secret_bearing_gists_are_invalid(
    tmp_path, manual_entry
):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    (vault / "memory" / "2026-07-18.md").write_text(
        manual_entry, encoding="utf-8"
    )

    report = lint_vault(vault)

    assert report.valid_entries == 0
    assert report.invalid_entries >= 1
    assert recall(vault, "manual function release evidence") == []


@pytest.mark.parametrize(
    "unsafe_summary",
    (
        "chain_of_thought payload",
        "chain-of-thought payload",
        "chain of thought payload",
        "private reasoning payload",
        "hidden reasoning payload",
        "internal deliberation payload",
        "full transcript payload",
        "conversation transcript payload",
        "User: private payload",
        "Assistant: private payload",
        "System: private payload",
    ),
)
def test_manual_gist_with_private_payload_markers_is_invalid(
    tmp_path, unsafe_summary
):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    (vault / "memory" / "2026-07-18.md").write_text(
        _manual_gist(summary=unsafe_summary),
        encoding="utf-8",
    )

    report = lint_vault(vault)

    assert report.valid_entries == 0
    assert report.invalid_entries >= 1
    assert recall(vault, "manual function") == []


def test_oversized_manual_history_file_is_invalid_and_not_recalled(tmp_path):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    (vault / "memory" / "2026-07-18.md").write_text(
        _manual_gist() + ("x" * 1_100_000), encoding="utf-8"
    )

    report = lint_vault(vault)

    assert report.valid_entries == 0
    assert report.invalid_entries >= 1
    assert recall(vault, "manual function") == []


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

    original_read_history_file = memory_vault._read_history_file
    read_paths = []

    def track_bounded_read(path):
        read_paths.append(path)
        if path == oldest:
            raise AssertionError("recall read history outside its recent-file bound")
        return original_read_history_file(path)

    monkeypatch.setattr(memory_vault, "_read_history_file", track_bounded_read)

    assert recall(vault, "release evidence")
    assert oldest not in read_paths
    assert len(read_paths) == 31


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


def test_concurrent_processes_preserve_distinct_and_idempotent_gists(tmp_path):
    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    context = multiprocessing.get_context("spawn")
    process_count = 16
    barrier = context.Barrier(process_count)
    processes = [
        context.Process(
            target=_concurrent_record,
            args=(str(vault), index, index >= 8, barrier),
        )
        for index in range(process_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    assert [process.exitcode for process in processes] == [0] * process_count
    history = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((vault / "memory").glob("*.md"))
    )
    assert history.count("<!-- gist_id: shared-idempotent-gist -->") == 1
    for index in range(8):
        assert history.count(f"<!-- gist_id: distinct-gist-{index} -->") == 1
    projection = json.loads(
        (vault / ".derived" / "functions.json").read_text(encoding="utf-8")
    )
    assert {item["function_id"] for item in projection["functions"]} == {
        "function-shared",
        *(f"function-distinct-{index}" for index in range(8)),
    }


def test_vault_lock_timeout_is_bounded_and_leaves_history_unchanged(
    tmp_path, monkeypatch
):
    from hermes_cli import agent_memory_vault as memory_vault

    vault = tmp_path / "Agent Memory"
    initialize_vault(vault)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_vault_lock,
        args=(str(vault), ready, release),
    )
    holder.start()
    assert ready.wait(timeout=10)
    monkeypatch.setattr(memory_vault, "_VAULT_LOCK_TIMEOUT_SECONDS", 0.1)
    try:
        with pytest.raises(TimeoutError, match="Agent Memory vault lock"):
            append_gist(vault, _gist("timed-out-gist"))
    finally:
        release.set()
        holder.join(timeout=10)

    assert holder.exitcode == 0
    assert list((vault / "memory").glob("*.md")) == []
