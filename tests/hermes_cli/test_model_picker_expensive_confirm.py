from types import SimpleNamespace

import pytest

from hermes_cli.model_switch import ModelSwitchResult


def _bound(fn, instance):
    return fn.__get__(instance, type(instance))


def test_prompt_toolkit_model_picker_defers_confirmation_off_key_handler(monkeypatch):
    import cli as cli_mod

    result = ModelSwitchResult(
        success=True,
        new_model="openai/gpt-5.5-pro",
        target_provider="nous",
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: result,
    )

    captured = {}

    class _Thread:
        def __init__(self, *, target, args, daemon):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(cli_mod.threading, "Thread", _Thread)

    self_ = SimpleNamespace(
        _app=object(),
        _model_picker_state={
            "stage": "model",
            "provider_data": {"slug": "nous"},
            "model_list": ["openai/gpt-5.5-pro"],
            "selected": 0,
            "user_provs": None,
            "custom_provs": None,
        },
        provider="nous",
        model="openai/gpt-5.5",
        base_url="",
        api_key="",
        _restore_modal_input_snapshot=lambda: None,
        _invalidate=lambda **_kwargs: None,
    )
    self_._close_model_picker = _bound(cli_mod.HermesCLI._close_model_picker, self_)
    self_._confirm_and_apply_model_switch_result = (
        lambda *_args: captured.setdefault("ran_inline", True)
    )

    # The key handler now resolves persistence via resolve_persist_behavior,
    # which defaults to True (persist-by-default). Simulate that call.
    _bound(cli_mod.HermesCLI._handle_model_picker_selection, self_)(persist_global=True)

    assert self_._model_picker_state is None
    assert captured["started"] is True
    assert captured["daemon"] is True
    # Third arg is the fresh picker custom_providers snapshot (None here).
    assert captured["args"] == (result, True, None)
    assert "ran_inline" not in captured


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli", "cowork"])
def test_prompt_toolkit_picker_selects_each_primary_local_agent(
    monkeypatch, provider
):
    import cli as cli_mod

    captured = {}
    result = ModelSwitchResult(
        success=True,
        new_model="default",
        target_provider=provider,
    )

    def fake_switch(**kwargs):
        captured["switch"] = kwargs
        return result

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", fake_switch)
    self_ = SimpleNamespace(
        _app=None,
        _model_picker_state={
            "stage": "provider",
            "providers": [
                {
                    "authenticated": True,
                    "models": ["default"],
                    "name": provider,
                    "slug": provider,
                }
            ],
            "selected": 0,
            "user_provs": {provider: {"timeout": 123}},
            "custom_provs": [],
        },
        provider="nous",
        model="hermes-4",
        base_url="",
        api_key="",
        _restore_modal_input_snapshot=lambda: None,
        _invalidate=lambda **_kwargs: None,
    )
    self_._close_model_picker = _bound(cli_mod.HermesCLI._close_model_picker, self_)
    self_._confirm_and_apply_model_switch_result = (
        lambda selected, persist, **_kwargs: captured.update(
            applied=selected,
            persist=persist,
        )
    )
    select = _bound(cli_mod.HermesCLI._handle_model_picker_selection, self_)

    select(persist_global=False)
    assert self_._model_picker_state["stage"] == "model"
    select(persist_global=False)

    assert captured["switch"]["explicit_provider"] == provider
    assert captured["switch"]["raw_input"] == "default"
    assert captured["switch"]["user_providers"] == {
        provider: {"timeout": 123}
    }
    assert captured["applied"] is result
    assert captured["persist"] is False
