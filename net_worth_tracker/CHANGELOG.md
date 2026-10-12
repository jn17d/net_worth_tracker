# Changelog

## 2.1.0 — 2026-10-12

- All configuration now lives exclusively on the app's own Settings page.
  The add-on Options (and their schema, translations and options.json
  mapping in run.sh) are removed — one source of truth, no precedence rules,
  no "locked" fields.

## 2.0.0 — 2026-10-12

Overhauled Monzo connection — the "paste-the-code" flow:

- The redirect URI is now the fixed constant `https://localhost/net-worth-tracker`,
  registered once on your Monzo developer client. The page never needs to load:
  after authorising, copy the URL from the address bar (it contains `?code=…`)
  into the Settings page and press Connect — the app exchanges the code
  server-side.
- Removes all dynamic redirect-URI machinery: no more Supervisor API calls,
  ingress-URL detection, `hassio_api` role requirements or HTTPS/public-URL
  constraints. Redirect mismatch errors are now impossible after step 2.
- Settings page: new Authorise / paste-URL / Connect box; redirect-URI field
  removed. Playground-token paste mode kept as a fallback.
- Tokens still refresh automatically; reconnection is only needed if you
  revoke the client.
- `monzo_redirect_uri` option removed from add-on configuration.

## 1.0.6 — 2026-10-12

- Derive the Monzo redirect URI from the incoming ingress request (path prefix
  + `X-Forwarded-Host`/`X-Forwarded-Proto`) when the Supervisor token isn't
  available, instead of falling straight back to `localhost`. Opening the
  panel once is now enough for the Settings page to show the correct URL.

## 1.0.5 — 2026-10-12

- Log clearly when the Supervisor token/ingress URL can't be detected (with
  retries) instead of failing silently, so the Monzo redirect fallback to
  localhost is visible in the add-on log.
- Better Settings-page error messages showing HTTP status and guidance (1.0.4).

## 1.0.3 — 2026-10-12

- Fix add-on failing to launch with `Unable to access the API, forbidden /
  Failed to get addon config from Supervisor API`: `run.sh` no longer depends
  on bashio or the Supervisor API at all — options are read directly from
  `/data/options.json` with python3, so a denied API can never stop startup.
- Add `hassio_role: manager` so any remaining Supervisor calls (ingress URL
  detection for the Monzo redirect) are properly authorised.

## 1.0.2 — 2026-10-12

- Fix add-on failing to start: the pinned base image tag
  `ghcr.io/home-assistant/base-python:3.12-alpine3.24` does not exist, so the
  Docker build failed. Now pinned to the published `3.12-alpine3.23` tag
  (multi-arch amd64/aarch64).
- Add the recommended `io.hass.*` image labels to the Dockerfile.

## 1.0.1 — 2026-11-10

- Fix installation on current Home Assistant: the per-arch `*-base-python`
  base images are no longer published, so the Dockerfile now builds from the
  multi-arch `ghcr.io/home-assistant/base-python` directly and the deprecated
  `build.yaml` is gone.
- Drop `armv7` from supported architectures — Home Assistant base images are
  published for amd64 and arm64 only.

## 1.0.0 — 2026-11-10

First public release.

- Trading 212: multiple accounts, live and practice environments, per-account breakdown.
- Monzo: OAuth sign-in with automatic token refresh, or a pasted playground token; pots included.
- Net worth chart with 1w / 1m / 3m / 1y / all ranges, split by provider.
- In-app Settings page: keys saved with owner-only permissions, masked in the
  browser, applied live without a restart.
- Home Assistant: sidebar panel via ingress, add-on Options mapping, Monzo
  redirect URI auto-derived from the ingress URL, data stored in `/data` so it
  rides along with Home Assistant backups.
- Demo mode with a year of realistic fake data.
