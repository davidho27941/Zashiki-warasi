# Choosing and registering `OAUTH_REDIRECT_URI`

> Ready-to-paste reference for setting up the Google OAuth redirect
> for Zashiki-warasi's headless reauth flow. Referenced from
> `README.md`, `MIGRATION-v1.md`, and `deploy/compose/README.md`.

## Why this matters

`OAUTH_REDIRECT_URI` is the URL Google sends the operator's browser to
after they consent on the OAuth consent screen. Google validates it
byte-for-byte against the list of "Authorized redirect URIs" registered
on your OAuth Client in the Google Cloud Console — a mismatch surfaces
as `Error 400: redirect_uri_mismatch` in the browser, with no way to
proceed.

**A public domain is NOT required.** Google special-cases
`http://localhost` and `http://127.0.0.1` (any port) so a loopback URI
works without HTTPS, without a domain, without a cert. Your v0.6.x CLI
`reauth` already relies on this loopback exception — `InstalledAppFlow.run_local_server()`
picks a random localhost port on the fly, and Google honours it.

## Allowed / disallowed URI forms

| URI form | Allowed? | Notes |
|---|---|---|
| `http://localhost` / `http://localhost:<port>` | ✅ | Loopback special case — no HTTPS required |
| `http://127.0.0.1` / `http://127.0.0.1:<port>` | ✅ | Same loopback special case |
| `http://192.168.x.x/…` (private IP) | ❌ | Google requires HTTPS for non-loopback |
| `http://zashiki.local/…` (`.local` mDNS) | ❌ | Non-loopback hostname → HTTPS required |
| `https://<public-domain>/…` (Let's Encrypt / any public CA) | ✅ | Standard web-flow deploy |
| `https://<internal-domain>/…` (self-signed / internal PKI) | ❌ | Google validates the cert chain against public roots |

## Strategy A — SSH tunnel + loopback (recommended for homelab)

**Zero cost, zero public exposure, requires SSH access to the container host.**

1. Register `http://127.0.0.1:8080/auth/callback` in the Google Cloud Console
   (see "Registering the URI" below).

2. Set on the container:
   ```
   OAUTH_REDIRECT_URI=http://127.0.0.1:8080/auth/callback
   ```

3. When you want to reauth, open an SSH port-forward from your workstation
   to the container host:
   ```
   ssh -L 8080:localhost:8080 you@proxmox-vm
   ```

4. In another terminal (still on your workstation), trigger the flow:
   ```
   curl -X POST http://127.0.0.1:8080/reauth -H "X-API-Key: $HTTP_API_KEY"
   ```
   Response:
   ```json
   {
     "auth_url": "http://127.0.0.1:8080/auth/start?csrf=…",
     "expires_in": 600,
     "state": "…"
   }
   ```

5. Open the `auth_url` in your workstation's browser. It hits the SSH
   tunnel → the container's FastAPI service → 307-redirects you to Google.

6. Complete the Google consent screen. Google redirects your browser
   to `http://127.0.0.1:8080/auth/callback?code=…&state=…`, which is
   your local end of the SSH tunnel → the container writes the new
   `token.json` and reloads its credentials. You see:
   ```
   OAuth reauth completed. You can close this tab.
   ```

7. Close the SSH tunnel. Done until the next reauth.

**Trade-offs:** requires an SSH session per reauth event; reauth is
rare (Google refresh tokens last ~6 months for `In production` clients),
so the friction is worth the security posture.

## Strategy B — Cloudflare Tunnel (or ngrok / any tunneling service)

**Public HTTPS URL without opening router ports; adds a long-lived
external process.**

1. Install `cloudflared` on the container host and log in with your
   Cloudflare account.

2. Create a named tunnel and point it at the FastAPI service:
   ```
   cloudflared tunnel create zashiki
   cloudflared tunnel route dns zashiki zashiki.yourdomain.com
   cloudflared tunnel run --url http://127.0.0.1:8080 zashiki
   ```
   Run this as a systemd service so it survives reboots.

3. Register `https://zashiki.yourdomain.com/auth/callback` in the
   Google Cloud Console.

4. Set on the container:
   ```
   OAUTH_REDIRECT_URI=https://zashiki.yourdomain.com/auth/callback
   ```

5. Now reauth works from any device without SSH:
   ```
   curl -X POST https://zashiki.yourdomain.com/reauth -H "X-API-Key: $HTTP_API_KEY"
   # Open the returned auth_url in any browser.
   ```

**Trade-offs:** one more moving part (cloudflared process); requires
a Cloudflare account (free tier is fine); the tunnel's public
endpoint is discoverable — the `HTTP_API_KEY` on `/poll` and
`/reauth` becomes load-bearing.

The trycloudflare.com preview URL (`--url http://127.0.0.1:8080`
without a named tunnel) works too but changes every restart, so it's
not usable for a persistent OAuth registration.

## Strategy C — Public domain + Let's Encrypt

**Most flexible, most operational surface.**

1. Point a DNS record at your home IP (DDNS if the IP is dynamic;
   Cloudflare / duckdns are free options).

2. Forward router ports 80 and 443 to the container host.

3. Deploy a reverse proxy in front of the FastAPI service — Caddy is
   the least-config option:
   ```
   # Caddyfile
   zashiki.yourdomain.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```
   Caddy handles Let's Encrypt HTTP-01 challenge automatically.

4. Register `https://zashiki.yourdomain.com/auth/callback` in the
   Google Cloud Console.

5. Reauth works from anywhere — same curl as strategy B.

**Trade-offs:** most robust for multi-service homelab setups where
you already have a reverse proxy; own the DNS + cert renewal; router
port-forwards expose 80/443 to the internet (protected only by whatever
the proxy enforces).

## Registering the URI in the Google Cloud Console

1. Go to **APIs & Services** → **Credentials**.
2. Click the existing **OAuth 2.0 Client ID** used for Zashiki-warasi
   (the same one your v0.6.x deploy already uses — the OAuth Client
   type stays `Desktop app` OR you can create a `Web application` type
   for stricter host validation).
3. Under **Authorized redirect URIs**, click **+ ADD URI**.
4. Paste the chosen URI **exactly** — no trailing slash, correct port,
   correct protocol (`http` for loopback, `https` for everything else).
5. Click **Save**. Google needs up to 5 minutes to propagate; if the
   very first reauth fails with `redirect_uri_mismatch`, wait and retry.

## Verifying the registration

```
curl -X POST http://<your-service>:8080/reauth -H "X-API-Key: $HTTP_API_KEY"
# Copy the auth_url from the response.
# Open it in a browser.
```

- **Success:** you see Google's account-picker + consent screen,
  complete it, and land on `/auth/callback` with "OAuth reauth
  completed."
- **`Error 400: redirect_uri_mismatch`:** the browser will show
  BOTH the URI Google received and the URI you registered — a
  character-diff of the two tells you what to fix (common: trailing
  slash, wrong port, `http` vs `https`).
- **`fetch_token_failed: (invalid_grant) Missing code verifier`:**
  the OAuth flow store lost the PKCE `code_verifier` between
  `/auth/start` and `/auth/callback`. Should not happen in v1.0 —
  fixed by persisting `code_verifier` in the flow store's JSON
  payload. If you hit this on a build older than the fix, rebuild.
- **`fetch_token_failed: Scope has changed from "..." to "..."`:**
  Google's token endpoint returned every scope previously granted
  to your Google account for this OAuth client — often a superset
  of what the app asks for (e.g. Drive scopes from a past consent).
  oauthlib rejects the mismatch by default. Set the environment
  variable `OAUTHLIB_RELAX_TOKEN_SCOPE=1` to downgrade the check to
  a warning. The container image and `build_services()` already set
  this via `os.environ.setdefault`; only worth touching if you're
  running an old build or a bare CLI outside the container.

## Rotating and adding URIs

The same OAuth Client can carry **multiple** authorized redirect URIs
simultaneously. Common setup: register both `http://localhost:<port>`
(for local CLI reauth via `InstalledAppFlow.run_local_server()`) AND
the container-service URI (loopback or tunneled) so you can reauth
either way without re-registering.

Google matches whichever URI the running service sends in the
`redirect_uri` parameter of the initial `/oauth2/auth` request. There's
no "primary" URI — first match wins.

## Notes for existing v0.6.x users

You already rely on Google's loopback exception. `InstalledAppFlow.run_local_server()`
opens `http://localhost:<random_port>` and Google honours it because
of the same special case that makes strategy A work. Zero OAuth
configuration changes are required if you're keeping strategy A —
the CLI reauth flow keeps working alongside the new headless flow.

The `Desktop app` OAuth Client type used in v0.6.x is fine for both
the CLI flow AND the FastAPI-brokered web flow. No need to create a
new `Web application` client.

## Related

- [`README.md`](../README.md) — "Reauth (headless)" section for the
  operator-facing walkthrough.
- [`MIGRATION-v1.md`](../MIGRATION-v1.md) — first step in the v0.6.x →
  v1.0.0 upgrade path is picking a strategy from this doc.
- [`deploy/compose/README.md`](../deploy/compose/README.md) — step 5
  in the compose quickstart shows the SSH-tunnel flow inline.
