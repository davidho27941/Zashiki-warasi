"""GET /auth/start and GET /auth/callback — OAuth web flow endpoints.

The full flow (started by POST /reauth):

  1. Operator hits `POST /reauth` → server creates a Flow, persists it
     in `oauth_flow_store` keyed by a fresh CSRF `state`, returns
     `{"auth_url": ".../auth/start?csrf=<state>", ...}`.
  2. Operator opens `auth_url` in a browser → `GET /auth/start`.
     - Validates csrf (pops the flow; if missing → 400).
     - Builds Google's consent URL via `Flow.authorization_url(...)`.
     - Puts the Flow back so the callback (which may land on a
       different replica) can find it.
     - 307-redirects the browser to Google.
  3. Operator consents at Google → Google redirects to
     `GET /auth/callback?code=<code>&state=<csrf>`.
     - Pops the Flow, exchanges `code` for tokens, writes token.json,
       reloads the running Gmail client's credentials, notifies via
       Telegram, returns 200 plain-text success.

Neither endpoint requires the `X-API-Key` header — /auth/start is
guarded by CSRF-in-store, /auth/callback is guarded by the state
Google echoes back plus store-membership. Browsers can't send
arbitrary headers on a Google-initiated redirect anyway.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse

from zashiki_warasi.core.services import Services
from zashiki_warasi.gmail.auth import _persist
from zashiki_warasi.notifications.telegram import TelegramError
from zashiki_warasi.web.dependencies import get_services

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/start", name="auth_start")
def auth_start(
    csrf: str, services: Services = Depends(get_services)
) -> RedirectResponse:
    _require_redirect_uri(services)
    flow = services.oauth_flow_store.pop(csrf)
    if flow is None:
        raise HTTPException(
            status_code=400,
            detail="unknown_or_expired_csrf",
        )
    # `authorization_url` returns (url, state) — we set state=csrf
    # explicitly so the callback receives the same value we minted.
    auth_url, _echoed_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=csrf,
    )
    # Put the flow back so /auth/callback (potentially on a different
    # replica) can find it via the same csrf.
    services.oauth_flow_store.put(csrf, flow)
    logger.info(f"oauth: /auth/start redirected (state={csrf[:8]})")
    return RedirectResponse(url=auth_url, status_code=307)


@router.get("/callback", name="auth_callback")
def auth_callback(
    code: str,
    state: str,
    services: Services = Depends(get_services),
) -> PlainTextResponse:
    flow = services.oauth_flow_store.pop(state)
    if flow is None:
        raise HTTPException(
            status_code=400,
            detail="unknown_or_expired_state",
        )
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        # Google's error page usually shows the reason in the browser
        # already; log for our own record and 400 the operator.
        logger.warning(f"oauth: fetch_token failed (state={state[:8]}): {exc}")
        raise HTTPException(
            status_code=400, detail=f"fetch_token_failed: {exc}"
        )
    credentials = flow.credentials
    token_path = services.gmail_settings.token_path
    _persist(credentials, token_path)
    services.gmail_client.reload_credentials()
    logger.info(
        f"oauth: token refreshed via web flow (state={state[:8]}, "
        f"path={token_path})"
    )
    _notify_reauth_success(services, token_path)
    return PlainTextResponse(
        "OAuth reauth completed. You can close this tab.",
        status_code=200,
    )


def _require_redirect_uri(services: Services) -> None:
    if not services.oauth_settings.redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="OAUTH_REDIRECT_URI is not configured",
        )


def _notify_reauth_success(services: Services, token_path) -> None:
    if services.notifier is None:
        return
    try:
        services.notifier.send_message(
            "✅ Zashiki-warasi: Gmail 授權已透過 web flow 更新\n\n"
            f"Token path: <code>{token_path}</code>"
        )
    except TelegramError:
        # Alert failure is non-blocking; the reauth itself succeeded.
        logger.exception("Failed to send Telegram alert about reauth success")
