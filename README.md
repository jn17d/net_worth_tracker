# Net Worth Tracker — Home Assistant add-on

One dashboard for your whole net worth: **Trading 212** investment accounts and
**Monzo** accounts (including pots), polled on a schedule, charted over time and
stored on your own Home Assistant machine. Your keys never leave your server —
the browser only ever talks to the add-on itself. Pure Python standard library
inside, so the image is small and installs nothing extra.

## Features

- Net worth chart (1w / 1m / 3m / 1y / all) with a per-provider breakdown.
- Trading 212: several accounts, live or practice environment, key + secret auth.
- Monzo: proper OAuth sign-in with automatic token refresh (or paste a
  playground token), pots included, approval handled in the Monzo app.
- If a source fails mid-refresh, its last saved value is kept so the chart
  never dips on a hiccup — you get a notice instead.
- In-app **Settings** page: keys saved with owner-only permissions, masked in
  the browser (`••••last4`), applied live without a restart.
- Demo mode with a year of realistic fake data.

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**,
   add `https://github.com/jn17d/net_worth_tracker`, then open
   **Net Worth Tracker** from the store.
2. Click **Install** — the image builds locally on your Home Assistant machine
   (it's small; first install takes a couple of minutes on a Raspberry Pi).
3. Toggle **Show in sidebar**. **Net Worth** appears in the sidebar.
4. Optional: switch on **Demo mode** in the add-on's *Configuration* tab to
   explore with fake data before adding keys.

## Connecting Trading 212

1. In Trading 212: **Settings → API → Create key** (one key per account).
2. In the add-on's **Settings page** (open the Net Worth panel → Settings):
   enter the API key + secret, optionally a label. Add more blocks for more
   accounts.
3. Set *Trading 212 environment* to `demo` if your key was made for a practice
   account.

## Connecting Monzo

Monzo uses a manual one-time code exchange — no public URL, HTTPS or ports needed.

1. At [developers.monzo.com](https://developers.monzo.com) → **Clients** →
   **New client**, choose **Confidential**.
2. Set the client's **Redirect URL** to exactly:
   `https://localhost/net-worth-tracker`
   (Yes, it's a made-up address — it never needs to load.)
3. Paste the client ID + secret into the add-on's Settings page and **Save**.
4. Click **Authorise on Monzo**, approve access (you may also need to approve
   it in the Monzo app on your phone). The page you land on **won't load —
   that's expected**.
5. Copy the **whole URL** from the browser's address bar (it ends in
   `?code=…`) and paste it into the *Paste the URL you landed on* box →
   **Connect**.

Tokens are stored locally and refreshed automatically; you only do this once.

## Configuration

Everything is configured on the app's own **Settings page** (open the Net Worth
panel → Settings) — there are no add-on Options to fill in. Settings live in a
`credentials.json` under the add-on's `/data` directory: they survive restarts
and are included in Home Assistant backups.

| Setting (in the app) | Meaning |
|---|---|
| Trading 212 key + secret + label | One block per account; create keys in Trading 212 → Settings → API |
| Trading 212 environment | `live` or `demo` (practice accounts) |
| Monzo client ID + secret | OAuth client from developers.monzo.com (redirect URL `https://localhost/net-worth-tracker`) |
| Refresh interval | How often to poll, 1–1440 minutes (default 15) |
| Sign-in (optional) | Username + password protecting the dashboard |

## Security

- The dashboard is served through Home Assistant **ingress** — your HA sign-in
  protects it, and no port is exposed to the network by default.
- Secrets are stored in `/data` with owner-only permissions (`600`) and are
  never sent to the browser unmasked; the Settings API is CSRF-protected.
- A strict Content-Security-Policy is set on every page.

## Backups

Snapshots, tokens and settings all live in the add-on's `/data`, which is
included automatically in Home Assistant backups.

## Troubleshooting

| Message | Fix |
|---|---|
| *Monzo is waiting for your approval* | Open the Monzo app on your phone and allow access. |
| *Monzo refused the sign-in…invalid redirect* | The redirect URL on your Monzo client doesn't match the one on the Settings page — copy it exactly. |
| *rejected the API key* (Trading 212) | Check key + secret, and that the key belongs to the environment (`live`/`demo`) you selected. |
| *rate limit hit* | Nothing to do — it retries on the next poll. |
| Chart shows stale values | A source failed; the notice explains which one. |

Logs: add-on page → **Log**. Health check: `/api/hassio_ingress/<token>/healthz`.

## Standalone (without Home Assistant)

```sh
cd net_worth_tracker/app
python3 server.py            # http://localhost:8787 — real mode
DEMO=1 python3 server.py     # demo data
```

Python 3.9+, no third-party packages. Optional `.env` next to `server.py` is
read at start-up (real environment variables win).

## License

[MIT](LICENSE)
