# Changelog

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
