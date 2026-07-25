"""TUI JSON-RPC model catalog boundary for local agent providers."""

from __future__ import annotations

from tui_gateway import server


def test_model_options_returns_all_local_agents(monkeypatch):
    from hermes_cli.inventory import ConfigContext

    monkeypatch.setattr(
        server,
        "_model_picker_context",
        lambda _agent: ConfigContext(
            current_provider="",
            current_model="",
            current_base_url="",
            user_providers={},
            custom_providers=[],
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "hermes_cli.inventory._apply_pricing",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "hermes_cli.inventory._apply_capabilities",
        lambda *_args, **_kwargs: None,
    )

    response = server._methods["model.options"](
        "request-1",
        {"explicit_only": True},
    )

    local_rows = [
        row
        for row in response["result"]["providers"]
        if row["slug"] in {"claude-cli", "codex-cli", "cowork"}
    ]
    assert [row["slug"] for row in local_rows] == [
        "claude-cli",
        "codex-cli",
        "cowork",
    ]
    # "default" leads every local agent (it omits the CLI's --model flag);
    # the two CLI agents also offer their curated model ids, Cowork does not
    # because it selects its own model.
    assert all(row["models"][0] == "default" for row in local_rows)
    assert all(
        len(row["models"]) > 1
        for row in local_rows
        if row["slug"] != "cowork"
    )
    assert next(r for r in local_rows if r["slug"] == "cowork")["models"] == ["default"]
    assert all(row["authenticated"] is False for row in local_rows)
    assert all(row["auth_type"] == "external_process" for row in local_rows)
