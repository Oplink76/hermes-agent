"""Contract tests for the fixed-scope Work Inbox bearer credential."""
from __future__ import annotations

import secrets
from unittest.mock import MagicMock

import pytest

import plugins.dashboard_auth.work_inbox as work_inbox
from hermes_cli.dashboard_auth import token_auth


@pytest.fixture
def strong_secret() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def clean_route(monkeypatch):
    monkeypatch.delenv("HERMES_WORK_INBOX_SECRET", raising=False)
    token_auth.clear_token_routes()
    yield
    token_auth.clear_token_routes()


def test_work_inbox_provider_has_one_fixed_scope(strong_secret):
    provider = work_inbox.WorkInboxSecretProvider(secret=strong_secret)

    principal = provider.verify_token(token=strong_secret)

    assert principal is not None
    assert principal.principal == "work-inbox"
    assert principal.provider == "work-inbox-secret"
    assert principal.scopes == ("work_inbox:submit",)


def test_provider_rejects_wrong_or_weak_secret(strong_secret):
    provider = work_inbox.WorkInboxSecretProvider(secret=strong_secret)

    assert provider.verify_token(token="wrong") is None
    with pytest.raises(ValueError):
        work_inbox.WorkInboxSecretProvider(secret="weak")


def test_register_reads_only_work_inbox_secret_and_registers_exact_route(
    strong_secret, monkeypatch,
):
    monkeypatch.setenv("HERMES_WORK_INBOX_SECRET", strong_secret)
    context = MagicMock()

    work_inbox.register(context)

    provider = context.register_dashboard_auth_provider.call_args.args[0]
    assert provider.verify_token(token=strong_secret) is not None
    assert token_auth.is_token_route(work_inbox.WORK_INBOX_ROUTE_PATH)
    assert not token_auth.is_token_route(work_inbox.WORK_INBOX_ROUTE_PATH + "/other")
