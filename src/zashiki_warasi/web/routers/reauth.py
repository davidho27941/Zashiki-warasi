"""POST /reauth — initiate the OAuth web flow.

Generates a CSRF `state`, builds a `Flow` from the on-disk client
secrets file, persists it in `oauth_flow_store`, and returns an
`auth_url` the operator opens in a browser to complete Google's
consent screen. See `web/routers/auth.py` for the callback that
finishes the flow.

Protected by `require_api_key` — the returned URL grants the ability
to write a new token when the operator opens it, so the endpoint that
mints it needs a shared-secret gate whenever the API key is configured.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from google_auth_oauthlib.flow import Flow

from zashiki_warasi.core.services import Services
from zashiki_warasi.web.dependencies import get_services, require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reauth"])

# In-flight TTL — matches OAuthFlowStore's default. Advertised to the
# operator so they know how long they have to click the URL.
_REAUTH_EXPIRES_IN_SECONDS = 600


@router.post("/reauth", dependencies=[Depends(require_api_key)])
def reauth(request: Request, services: Services = Depends(get_services)) -> dict:
    redirect_uri = services.oauth_settings.redirect_uri
    if not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="OAUTH_REDIRECT_URI is not configured",
        )

    state = secrets.token_urlsafe(24)
    credentials_path = services.gmail_settings.credentials_path
    scopes = list(services.gmail_settings.scopes)
    flow = Flow.from_client_secrets_file(
        str(credentials_path),
        scopes=scopes,
        state=state,
        redirect_uri=redirect_uri,
    )
    services.oauth_flow_store.put(state, flow)

    # Build a self-URL to `/auth/start?csrf=<state>` so the operator
    # gets ONE clean URL to click; the redirect to Google happens
    # server-side and can inject the exact required parameters.
    auth_start_path = request.url_for("auth_start")
    auth_url = f"{auth_start_path}?csrf={state}"
    logger.info(f"oauth: reauth flow initiated (state={state[:8]})")

    return {
        "auth_url": str(auth_url),
        "expires_in": _REAUTH_EXPIRES_IN_SECONDS,
        "state": state,
    }
