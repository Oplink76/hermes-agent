"""Fixed-scope bearer auth for Hermes Work Inbox submissions."""
from __future__ import annotations

import hmac
import os
from typing import Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    Session,
    TokenPrincipal,
)
from plugins.dashboard_auth.drain import assess_secret_strength


WORK_INBOX_ROUTE_PATH = "/api/plugins/kanban/work-inbox"
WORK_INBOX_SCOPE = "work_inbox:submit"


class WorkInboxSecretProvider(DashboardAuthProvider):
    """Non-interactive credential for the one Work Inbox route."""

    name = "work-inbox-secret"
    display_name = "Work Inbox (service credential)"
    supports_token = True
    supports_session = False

    def __init__(self, *, secret: str) -> None:
        reason = assess_secret_strength(secret)
        if reason is not None:
            raise ValueError(f"work inbox secret rejected: {reason}")
        self._secret = secret

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if token and hmac.compare_digest(token.encode(), self._secret.encode()):
            return TokenPrincipal(
                principal="work-inbox",
                provider=self.name,
                scopes=(WORK_INBOX_SCOPE,),
            )
        return None

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError("Work Inbox uses a service credential only.")

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError("Work Inbox uses a service credential only.")

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError("Work Inbox uses a service credential only.")

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


def register(ctx) -> None:
    """Register only when the fixed Work Inbox credential is configured."""

    secret = os.environ.get("HERMES_WORK_INBOX_SECRET", "").strip()
    if not secret:
        return
    try:
        provider = WorkInboxSecretProvider(secret=secret)
    except ValueError:
        return
    ctx.register_dashboard_auth_provider(provider)
    from hermes_cli.dashboard_auth.token_auth import register_token_route

    register_token_route(WORK_INBOX_ROUTE_PATH)
