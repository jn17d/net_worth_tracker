#!/usr/bin/env python3
"""Net worth dashboard for Trading 212 + Monzo.

Runs a small web server that polls both APIs on a schedule, saves snapshots to
SQLite and serves the dashboard. Your keys stay on this machine: the browser
only ever talks to this server. No third-party packages needed (Python 3.9+).

    python3 server.py            # real data, reads .env
    DEMO=1 python3 server.py     # fake data, no keys needed

Keys can also be entered in the app itself, on the Settings page (/settings).
Precedence: real environment variables > data/credentials.json > .env.

Home Assistant add-on: run.sh maps the add-on's Options to environment
variables, DATA_DIR points at the persistent volume, and the dashboard is
reached through ingress — requests arrive as /api/hassio_ingress/<token>/…,
which this server recognises and strips, so it also works standalone.
"""
from __future__ import annotations

import base64
import json
import math
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Keys present in the real environment when the process started. They always win
# over .env and the Settings page, and the Settings page shows them as locked.
REAL_ENV_KEYS = frozenset(os.environ)


def load_env(path: Path) -> None:
    """Minimal .env reader. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(ROOT / ".env")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
POLL_MINUTES = max(1, int(os.environ.get("POLL_MINUTES", "15")))
DEMO = os.environ.get("DEMO", "").lower() in ("1", "true", "yes")
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA / ("demo.db" if DEMO else "networth.db")

# --------------------------------------------------------------------------- #
# Credentials & settings
# --------------------------------------------------------------------------- #
# The Settings page (/settings) writes data/credentials.json (mode 0600).
# Precedence: real environment variables (locked in the UI) > that file > .env.
# Config is read live from os.environ, so saving applies without a restart.
CRED_FILE = DATA / "credentials.json"
CSRF_TOKEN = secrets.token_urlsafe(32)

SETTING_KEYS = (
    "MONZO_CLIENT_ID", "MONZO_CLIENT_SECRET", "MONZO_ACCESS_TOKEN", "MONZO_ACCOUNT_IDS",
    "BASIC_AUTH_USER", "BASIC_AUTH_PASS", "T212_ENV", "POLL_MINUTES",
)
T212_SLOTS = 4  # the Settings page exposes T212_1_* .. T212_4_*


class SettingsError(Exception):
    pass


def mask(value: str) -> str:
    """Safe to send to the browser: only ever the last 4 characters."""
    return ("••••" + value[-4:]) if len(value) > 4 else "••••"


def load_saved_credentials() -> dict:
    if not CRED_FILE.exists():
        return {}
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items()
            if k in SETTING_KEYS or re.fullmatch(r"T212_\d+_(KEY|SECRET|LABEL)", k)
            if isinstance(v, str) and v.strip()}


def apply_saved_credentials() -> None:
    """Runs once at start-up: saved settings override .env, never real env vars."""
    for key, value in load_saved_credentials().items():
        if key not in REAL_ENV_KEYS:
            os.environ[key] = value
    try:
        if CRED_FILE.exists():
            os.chmod(CRED_FILE, 0o600)  # tighten any hand-created copies
    except OSError:
        pass


def save_credentials_file(values: dict) -> None:
    """Atomic write with owner-only permissions."""
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CRED_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(values, f, indent=2, sort_keys=True)
        os.replace(tmp, CRED_FILE)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.chmod(CRED_FILE, 0o600)
    except OSError:
        pass


apply_saved_credentials()

# Live config lookups. There are deliberately no module-level constants here:
# values entered on the Settings page must take effect without a restart.
def poll_minutes() -> int:
    try:
        return max(1, min(1440, int(os.environ.get("POLL_MINUTES", "15"))))
    except ValueError:
        return 15


def t212_env() -> str:
    return "demo" if os.environ.get("T212_ENV", "live").strip().lower() == "demo" else "live"


def monzo_oauth() -> bool:
    return bool(os.environ.get("MONZO_CLIENT_ID", "").strip()
                and os.environ.get("MONZO_CLIENT_SECRET", "").strip())


# Fixed, one-time redirect URI registered on the Monzo developer portal. The
# page never needs to load — after authorising, the browser lands on this URL
# with ?code=… in the address bar, the user copies that URL back into the
# Settings page, and the server exchanges the code. This removes any need for
# the app to know its own public address (ingress token, HTTPS, ports, …).
MONZO_REDIRECT = "https://localhost/net-worth-tracker"


def monzo_redirect() -> str:
    """Redirect URI used for both the consent URL and the token exchange.

    An explicit MONZO_REDIRECT_URI (rare, e.g. an off-label setup registered on
    the Monzo client) always wins; otherwise the fixed constant is used.
    """
    uri = os.environ.get("MONZO_REDIRECT_URI", "").strip()
    return uri or MONZO_REDIRECT


def monzo_account_ids() -> list:
    return [a.strip() for a in os.environ.get("MONZO_ACCOUNT_IDS", "").split(",") if a.strip()]


def auth_user() -> str:
    return os.environ.get("BASIC_AUTH_USER", "").strip()


def auth_pass() -> str:
    return os.environ.get("BASIC_AUTH_PASS", "")


def humanise_monzo_error(raw: str) -> str:
    """Turn Monzo's token-endpoint error JSON into a readable sentence."""
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError
    except ValueError:
        return (raw[:200] + ".") if raw.strip() else "no details were returned."
    msg = d.get("error_description") or d.get("error") or "unknown error"
    hint = {
        "invalid_client": "The client ID and secret don't match — re-enter them in Settings.",
        "invalid_grant": "The sign-in code was already used or expired — press “Sign in to Monzo” again.",
        "invalid_request": "Check the Redirect URI in Settings exactly matches the one registered on your Monzo OAuth client.",
        "unauthorized_client": "This Monzo OAuth client isn't allowed to use this flow — check it in developer.monzo.com.",
    }.get(d.get("error"), "")
    return f"{msg}." + (f" {hint}" if hint else "")


def credentials_state() -> dict:
    """What the Settings page may show. Secrets are always masked."""
    saved = load_saved_credentials()

    def info(key):
        if key in REAL_ENV_KEYS:
            return {"locked": True, "set": True, "masked": mask(os.environ[key].strip())}
        value = os.environ.get(key, "").strip()
        return {"locked": False, "set": bool(value), "masked": mask(value) if value else None,
                "saved": key in saved}

    accounts = [{
        "n": str(n),
        "key": info(f"T212_{n}_KEY"),
        "secret": info(f"T212_{n}_SECRET"),
        "label": os.environ.get(f"T212_{n}_LABEL", "").strip(),
    } for n in range(1, T212_SLOTS + 1)]
    return {
        "csrf": CSRF_TOKEN,
        "demo": DEMO,
        "accounts": accounts,
        "fields": {k: info(k) for k in SETTING_KEYS},
        "plain": {
            "MONZO_CLIENT_ID": os.environ.get("MONZO_CLIENT_ID", "").strip(),
            "MONZO_ACCOUNT_IDS": os.environ.get("MONZO_ACCOUNT_IDS", "").strip(),
            "BASIC_AUTH_USER": auth_user(),
            "T212_ENV": t212_env(),
            "POLL_MINUTES": str(poll_minutes()),
        },
        "monzoRedirect": monzo_redirect(),
        "monzo": {"oauth": monzo_oauth(), "connected": monzo.connected},
        "authEnabled": bool(auth_user()),
    }


def apply_credentials(changes) -> None:
    """Validate a Settings-page payload, persist it, and apply it live."""
    if not isinstance(changes, dict):
        raise SettingsError("Expected an object of settings.")
    allowed = set(SETTING_KEYS)
    for n in range(1, T212_SLOTS + 1):
        allowed |= {f"T212_{n}_KEY", f"T212_{n}_SECRET", f"T212_{n}_LABEL"}
    unknown = sorted(k for k in changes if k not in allowed)
    if unknown:
        raise SettingsError(f"Unknown setting(s): {', '.join(unknown[:6])}.")
    locked = sorted(k for k in changes if k in REAL_ENV_KEYS)
    if locked:
        raise SettingsError(f"{', '.join(locked)} is set by a real environment variable. "
                            "Change it where the server starts, not here.")

    clean: dict = {}
    for key, value in changes.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            clean[key] = None
            continue
        if not isinstance(value, str) or len(value) > 1024:
            raise SettingsError(f"{key}: must be text of at most 1024 characters.")
        value = value.strip()
        if key == "T212_ENV":
            value = value.lower()
        clean[key] = value

    result = load_saved_credentials()
    for key, value in clean.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value

    def effective(key) -> bool:
        if key in REAL_ENV_KEYS:
            return bool(os.environ.get(key, "").strip())
        return bool(result.get(key, "").strip())

    errors = []
    for n in range(1, T212_SLOTS + 1):
        if {f"T212_{n}_KEY", f"T212_{n}_SECRET"} & clean.keys() \
                and effective(f"T212_{n}_KEY") != effective(f"T212_{n}_SECRET"):
            errors.append(f"Trading 212 account {n}: the key and secret go together — save both or clear both.")
    if {"MONZO_CLIENT_ID", "MONZO_CLIENT_SECRET"} & clean.keys() \
            and effective("MONZO_CLIENT_ID") != effective("MONZO_CLIENT_SECRET"):
        errors.append("Monzo: the client ID and client secret go together.")
    if {"BASIC_AUTH_USER", "BASIC_AUTH_PASS"} & clean.keys() \
            and effective("BASIC_AUTH_USER") != effective("BASIC_AUTH_PASS"):
        errors.append("Sign-in: enter both a username and a password, or clear both.")
    if "T212_ENV" in clean and clean["T212_ENV"] not in (None, "live", "demo"):
        errors.append("Trading 212 environment must be live or demo.")
    if "POLL_MINUTES" in clean and clean["POLL_MINUTES"] is not None:
        try:
            if not 1 <= int(clean["POLL_MINUTES"]) <= 1440:
                raise ValueError
        except ValueError:
            errors.append("Refresh interval must be a whole number of minutes between 1 and 1440.")
    if "MONZO_ACCOUNT_IDS" in clean and clean["MONZO_ACCOUNT_IDS"] is not None:
        ids = [i for i in clean["MONZO_ACCOUNT_IDS"].split(",") if i.strip()]
        if len(ids) > 20 or any(len(i.strip()) > 64 for i in ids):
            errors.append("Monzo account IDs: up to 20 comma-separated account ids.")
    if errors:
        raise SettingsError(" ".join(errors))

    save_credentials_file(result)
    for key in sorted(allowed):
        if key in REAL_ENV_KEYS:
            continue
        if key in result:
            os.environ[key] = result[key]
        elif key in clean:
            os.environ.pop(key, None)
    for n in range(1, T212_SLOTS + 1):
        for suffix in ("KEY", "SECRET", "LABEL"):
            key = f"T212_{n}_{suffix}"
            if key in REAL_ENV_KEYS:
                continue
            if key in result:
                os.environ[key] = result[key]
            elif key in clean:
                os.environ.pop(key, None)


RANGES = {"1w": 7, "1m": 30, "3m": 90, "1y": 365}
MAX_POINTS = 400

CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "script-src 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'"
    # frame-ancestors 'self': third-party sites can't embed the dashboard, but
    # Home Assistant's ingress panel can (it iframes us on the same origin).
)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message


def http_json(url, headers=None, data=None, method=None, timeout=20):
    hdrs = {"User-Agent": "networth-dashboard/1.0", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode("utf-8", "replace")[:300])
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(0, str(e))


# --------------------------------------------------------------------------- #
# Trading 212
# --------------------------------------------------------------------------- #
def load_t212_accounts():
    """Reads T212_<n>_KEY / _SECRET / _LABEL from the environment."""
    out = []
    for name in sorted(os.environ):
        m = re.fullmatch(r"T212_(\d+)_KEY", name)
        if not m:
            continue
        n = m.group(1)
        key = os.environ[name].strip()
        secret = os.environ.get(f"T212_{n}_SECRET", "").strip()
        if key and secret:
            out.append({
                "n": n,
                "key": key,
                "secret": secret,
                "label": os.environ.get(f"T212_{n}_LABEL", "").strip() or "Trading 212",
            })
    return out


def fetch_t212(acc):
    token = base64.b64encode(f"{acc['key']}:{acc['secret']}".encode()).decode()
    d = http_json(
        f"https://{t212_env()}.trading212.com/api/v0/equity/account/summary",
        {"Authorization": f"Basic {token}"},
    )
    total = float(d["totalValue"])
    invested = float((d.get("investments") or {}).get("currentValue") or 0)
    return {
        "id": f"t212:{acc['n']}",
        "provider": "Trading 212",
        "label": acc["label"],
        "currency": d.get("currency", "GBP"),
        "total": round(total, 2),
        "parts": [
            {"name": "Investments", "value": round(invested, 2)},
            {"name": "Cash", "value": round(total - invested, 2)},
        ],
    }


def explain_t212(acc, e: ApiError):
    who = "Trading 212" if acc["label"] == "Trading 212" else f"Trading 212 ({acc['label']})"
    if e.status in (401, 403):
        return {"text": f"{who} rejected the API key. Check the key and secret, and that the key was created for your {t212_env()} account."}
    if e.status == 429:
        return {"text": f"{who} rate limit hit. It will try again on the next refresh."}
    if e.status == 0:
        return {"text": f"Couldn't reach {who}."}
    return {"text": f"{who} returned an error ({e.status})."}


# --------------------------------------------------------------------------- #
# Monzo
# --------------------------------------------------------------------------- #
class Monzo:
    """Two modes:
    - OAuth (MONZO_CLIENT_ID + MONZO_CLIENT_SECRET): sign in once, tokens refresh themselves.
    - Token (MONZO_ACCESS_TOKEN): paste a playground token. It expires after a few hours.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.path = DATA / "monzo_tokens.json"
        self.tokens: dict = {}

    def _load_tokens(self):
        if self.tokens:
            return
        if monzo_oauth() and self.path.exists():
            try:
                self.tokens = json.loads(self.path.read_text())
            except ValueError:
                self.tokens = {}

    def access_token(self):
        """Saved OAuth token, else a pasted MONZO_ACCESS_TOKEN, else None."""
        self._load_tokens()
        return (self.tokens.get("access_token")
                or os.environ.get("MONZO_ACCESS_TOKEN", "").strip() or None)

    @property
    def configured(self) -> bool:
        return monzo_oauth() or bool(self.access_token())

    @property
    def connected(self) -> bool:
        return bool(self.access_token())

    def _save(self, d):
        self.tokens = {
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token") or self.tokens.get("refresh_token"),
            "saved_at": int(time.time()),
        }
        DATA.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.tokens, f)

    def _token_request(self, form):
        body = urllib.parse.urlencode(form).encode()
        return http_json(
            "https://api.monzo.com/oauth2/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
            "POST",
        )

    def exchange(self, code: str):
        self._save(self._token_request({
            "grant_type": "authorization_code",
            "client_id": os.environ["MONZO_CLIENT_ID"].strip(),
            "client_secret": os.environ["MONZO_CLIENT_SECRET"].strip(),
            "redirect_uri": monzo_redirect(),
            "code": code,
        }))

    def refresh(self):
        self._save(self._token_request({
            "grant_type": "refresh_token",
            "client_id": os.environ["MONZO_CLIENT_ID"].strip(),
            "client_secret": os.environ["MONZO_CLIENT_SECRET"].strip(),
            "refresh_token": self.tokens["refresh_token"],
        }))

    def api(self, path, params=None):
        url = "https://api.monzo.com" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(2):
            token = self.access_token()
            if not token:
                raise ApiError(401, "not connected")
            try:
                return http_json(url, {"Authorization": f"Bearer {token}"})
            except ApiError as e:
                can_refresh = monzo_oauth() and self.tokens.get("refresh_token")
                if e.status == 401 and attempt == 0 and can_refresh:
                    with self.lock:
                        if self.tokens.get("access_token") == token:  # nobody refreshed yet
                            self.refresh()
                    continue
                raise


monzo = Monzo()
PENDING_STATES: set[str] = set()


def fetch_monzo():
    accounts = [a for a in monzo.api("/accounts").get("accounts", []) if not a.get("closed")]
    wanted = monzo_account_ids()
    if wanted:
        accounts = [a for a in accounts if a["id"] in wanted]
    sources = []
    for a in accounts:
        bal = monzo.api("/balance", {"account_id": a["id"]})
        pots = monzo.api("/pots", {"current_account_id": a["id"]}).get("pots", [])
        pots = [p for p in pots if not p.get("deleted")]
        current = bal["balance"] / 100
        parts = [{"name": "Current account", "value": round(current, 2)}]
        parts += [{"name": p["name"], "value": round(p["balance"] / 100, 2)} for p in pots]
        kind = a.get("type", "")
        sources.append({
            "id": f"monzo:{a['id']}",
            "provider": "Monzo",
            "label": "Joint" if "joint" in kind else "Personal",
            "currency": bal.get("currency", "GBP"),
            "total": round(sum(p["value"] for p in parts), 2),
            "parts": parts,
        })
    return sources


def explain_monzo(e: ApiError):
    relogin = {"label": "Connect Monzo", "href": "/settings"} if monzo_oauth() else None
    if e.status == 401:
        if monzo_oauth():
            out = {"text": "Monzo session expired. Connect again from Settings to keep syncing."}
        else:
            out = {"text": "Monzo access token expired. Playground tokens only last a few hours. "
                           "Paste a new one in Settings, or add an OAuth client there so it refreshes itself."}
    elif e.status == 403:
        out = {"text": "Monzo is waiting for your approval. Open the Monzo app, allow access, then press Refresh."}
    elif e.status == 429:
        out = {"text": "Monzo rate limit hit. It will try again on the next refresh."}
    elif e.status == 0:
        out = {"text": "Couldn't reach Monzo."}
    else:
        out = {"text": f"Monzo returned an error ({e.status})."}
    if relogin and e.status == 401:
        out["action"] = relogin
    return out


# --------------------------------------------------------------------------- #
# Demo data (DEMO=1)
# --------------------------------------------------------------------------- #
DEMO_STATE: dict = {}
DEMO_RNG = random.Random(7)


def demo_step(s, rnd, dt=0.25):
    s["day"] += dt
    s["inv"] *= 1 + rnd.gauss(0.0003, 0.0065) * (dt / 0.25)
    if int(s["day"]) // 30 != int(s["day"] - dt) // 30:
        s["inv"] += 300  # monthly top-up
    s["cur"] = max(40.0, 650 + 480 * math.sin(s["day"] / 4.7) + rnd.gauss(0, 35))
    for k in s["pots"]:
        s["pots"][k] += rnd.choice([0, 0, 0, 4, 9]) * dt


def demo_sources(s):
    pots = [{"name": k, "value": round(v, 2)} for k, v in s["pots"].items()]
    monzo_parts = [{"name": "Current account", "value": round(s["cur"], 2)}] + pots
    return [
        {"id": "monzo:demo", "provider": "Monzo", "label": "Personal", "currency": "GBP",
         "total": round(sum(p["value"] for p in monzo_parts), 2), "parts": monzo_parts},
        {"id": "t212:1", "provider": "Trading 212", "label": "Stocks ISA", "currency": "GBP",
         "total": round(s["inv"] + s["isa_cash"], 2),
         "parts": [{"name": "Investments", "value": round(s["inv"], 2)},
                   {"name": "Cash", "value": round(s["isa_cash"], 2)}]},
    ]


def seed_demo():
    global DEMO_STATE
    if DB_PATH.exists():
        DB_PATH.unlink()
    s = {"day": 0.0, "inv": 8200.0, "isa_cash": 140.0, "cur": 700.0,
         "pots": {"Rainy day": 2100.0, "Holiday": 480.0, "New bike": 220.0}}
    steps = 365 * 4
    now = int(time.time())
    rows = []
    for i in range(steps):
        demo_step(s, DEMO_RNG)
        sources = demo_sources(s)
        ts = now - (steps - 1 - i) * 6 * 3600
        rows.append((ts, round(sum(x["total"] for x in sources), 2), pack_breakdown(sources)))
    with closing(db()) as c:
        with c:
            c.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)", rows)
    DEMO_STATE = s


def demo_poll():
    demo_step(DEMO_STATE, DEMO_RNG, dt=0.01)
    return demo_sources(DEMO_STATE)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def db():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS snapshots "
              "(ts INTEGER PRIMARY KEY, total REAL NOT NULL, breakdown TEXT NOT NULL)")
    return c


def pack_breakdown(sources):
    keys = ("id", "provider", "label", "currency", "total", "parts")
    return json.dumps({s["id"]: {k: s[k] for k in keys} for s in sources})


def store_snapshot(ts, sources):
    total = round(sum(s["total"] for s in sources), 2)
    with closing(db()) as c:
        with c:
            c.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?)", (ts, total, pack_breakdown(sources)))


def latest():
    with closing(db()) as c:
        row = c.execute("SELECT ts, total, breakdown FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    return (row[0], row[1], json.loads(row[2])) if row else None


def history(rng: str):
    since = time.time() - RANGES[rng] * 86400 if rng in RANGES else 0
    with closing(db()) as c:
        rows = c.execute("SELECT ts, total, breakdown FROM snapshots WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    if len(rows) > MAX_POINTS:
        step = len(rows) / MAX_POINTS
        rows = [rows[int(i * step)] for i in range(MAX_POINTS - 1)] + [rows[-1]]
    providers, points = set(), []
    for ts, total, breakdown in rows:
        by_provider: dict = {}
        for s in json.loads(breakdown).values():
            by_provider[s["provider"]] = round(by_provider.get(s["provider"], 0) + s["total"], 2)
        providers.update(by_provider)
        points.append({"t": ts, "total": round(total, 2), "p": by_provider})
    return {"points": points, "providers": sorted(providers)}


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #
STATE = {"errors": {}, "stale": set(), "polling": False, "last_poll_start": 0.0}
POLL_LOCK = threading.Lock()


def poll_once():
    with POLL_LOCK:
        STATE["polling"] = True
        STATE["last_poll_start"] = time.time()
        fresh, errors = [], {}
        try:
            if DEMO:
                fresh = demo_poll()
            else:
                for acc in load_t212_accounts():
                    try:
                        fresh.append(fetch_t212(acc))
                    except ApiError as e:
                        errors[f"t212:{acc['n']}"] = explain_t212(acc, e)
                    except Exception as e:  # malformed response etc.
                        errors[f"t212:{acc['n']}"] = {"text": f"Trading 212 ({acc['label']}): unexpected response ({type(e).__name__})."}
                if monzo.configured and monzo.connected:
                    try:
                        fresh += fetch_monzo()
                    except ApiError as e:
                        errors["monzo"] = explain_monzo(e)
                    except Exception as e:
                        errors["monzo"] = {"text": f"Monzo: unexpected response ({type(e).__name__})."}

            # If a source failed, keep its last known value so the graph doesn't dip.
            prev = (latest() or (0, 0, {}))[2]
            merged = {s["id"]: s for s in fresh}
            stale = set()
            for key in errors:
                for sid, val in prev.items():
                    matches = sid == key or (key == "monzo" and sid.startswith("monzo:"))
                    if matches and sid not in merged:
                        merged[sid] = val
                        stale.add(sid)
            STATE["errors"], STATE["stale"] = errors, stale
            if fresh:
                store_snapshot(int(time.time()), list(merged.values()))
        finally:
            STATE["polling"] = False


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:  # never let the loop die
            print("poll failed:", e)
        time.sleep(poll_minutes() * 60)


def summary():
    cur = latest()
    sources, total, updated, notices = [], 0.0, None, []
    if cur:
        updated, total, breakdown = cur
        for sid, s in breakdown.items():
            sources.append({**s, "id": sid, "stale": sid in STATE["stale"]})
        sources.sort(key=lambda s: (s["provider"], s["label"]))
        currencies = sorted({s.get("currency", "GBP") for s in sources})
        if len(currencies) > 1:
            notices.append({"level": "warn", "text": "Your accounts use different currencies ("
                            + ", ".join(currencies) + "). Totals are added without conversion."})
    if not (DEMO or load_t212_accounts() or monzo.configured):
        notices.append({"level": "info",
                        "text": "No accounts connected yet. Add your API keys in Settings — no restart needed.",
                        "action": {"label": "Open Settings", "href": "/settings"}})
    for err in STATE["errors"].values():
        notices.append({"level": "error", **err})
    if monzo_oauth() and not monzo.connected and not DEMO:
        notices.append({"level": "info", "text": "Monzo isn't connected yet.",
                        "action": {"label": "Connect Monzo", "href": "/settings"}})
    return {
        "sources": sources,
        "total": round(total, 2),
        "currency": sources[0].get("currency", "GBP") if sources else "GBP",
        "updated": updated,
        "syncing": STATE["polling"],
        "pollMinutes": poll_minutes(),
        "demo": DEMO,
        "notices": notices,
    }


# --------------------------------------------------------------------------- #
# Web server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "networth/1.0"

    def log_message(self, *args):
        pass

    # Home Assistant ingress proxies this add-on under /api/hassio_ingress/<token>/…
    INGRESS = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]+")

    def _split(self):
        """Set self.base to the ingress prefix ("" when accessed directly) and
        return the URL with that prefix stripped, so routing is path-agnostic."""
        url = urllib.parse.urlsplit(self.path)
        m = self.INGRESS.match(url.path)
        if m and len(url.path) > m.end():
            self.base = m.group(0)
            # The browser builds URLs as BASE + "/api/…" which can produce a
            # double slash after the ingress prefix ("/token//api/…"). Collapse
            # it so routing sees a clean path.
            rest = url.path[m.end():].lstrip("/")
            url = url._replace(path="/" + rest if rest else "/")
        else:
            self.base = ""
        return url

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, name: str):
        html = (ROOT / "static" / name).read_bytes()
        if self.base:
            # Pages prepend BASE to every fetch and link, so they work under ingress.
            tag = b"<script>window.BASE=" + json.dumps(self.base + "/").encode() + b";</script>"
            html = html.replace(b"</head>", tag + b"</head>", 1)
        return self._send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": CSP})

    def _authed(self) -> bool:
        if self.base:
            return True  # through ingress: the Home Assistant user is already signed in
        expected_user = auth_user()
        if not expected_user:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            return False
        return (secrets.compare_digest(user.encode(), expected_user.encode())
                and secrets.compare_digest(pw.encode(), auth_pass().encode()))

    def _gate(self) -> bool:
        if self._authed():
            return True
        self._send(401, {"error": "Sign in required"}, headers={"WWW-Authenticate": 'Basic realm="Net worth"'})
        return False

    @staticmethod
    def _extract_monzo_code(pasted: str) -> str:
        """Pull the ?code=… out of whatever the user pasted — a full URL
        (https://localhost/net-worth-tracker?code=abc&state=…) or a bare code."""
        pasted = pasted.strip()
        if "code=" in pasted:
            parsed = urllib.parse.urlsplit(pasted if "://" in pasted else "https://x/" + pasted.lstrip("/?"))
            return urllib.parse.parse_qs(parsed.query).get("code", [""])[0].strip()
        return pasted if 8 <= len(pasted) <= 512 and not pasted.startswith(("http", "{")) else ""

    def _settings_gate(self) -> bool:
        """Settings carry the keys, so they need Basic auth when configured and
        stay localhost-only when it isn't — except through Home Assistant
        ingress, which already sits behind the HA user's own sign-in."""
        if not auth_user() and not self.base and self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send(403, {"error": "Settings are only reachable from this machine. "
                                      "Open the Home Assistant panel, or set BASIC_AUTH_USER in "
                                      "Settings → Sign-in to use them remotely."})
            return False
        return self._gate()

    def do_GET(self):
        url = self._split()
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/healthz":
            return self._send(200, {"ok": True})
        if not self._gate():
            return
        if url.path in ("/", "/index.html"):
            return self._html("index.html")
        if url.path == "/settings":
            return self._html("settings.html")
        if url.path == "/api/credentials":
            if not self._settings_gate():
                return
            return self._send(200, credentials_state())
        if url.path == "/api/summary":
            return self._send(200, summary())
        if url.path == "/api/history":
            rng = q.get("range", ["3m"])[0]
            if rng not in RANGES and rng != "all":
                rng = "3m"
            return self._send(200, history(rng))
        if url.path == "/monzo/login":
            if not monzo_oauth():
                return self._send(400, "Add your Monzo client ID and secret on the Settings page first.", "text/plain; charset=utf-8")
            state = secrets.token_urlsafe(16)
            PENDING_STATES.add(state)
            params = urllib.parse.urlencode({
                "client_id": os.environ["MONZO_CLIENT_ID"].strip(), "redirect_uri": monzo_redirect(),
                "response_type": "code", "state": state,
            })
            return self._send(302, b"", "text/plain", {"Location": "https://auth.monzo.com/?" + params})
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        url = self._split()
        if not self._gate():
            return
        path = url.path
        if path == "/api/credentials":
            if not self._settings_gate():
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, {"error": "That didn't look like valid JSON."})
            token = self.headers.get("X-Settings-Token", "")
            if not secrets.compare_digest(token.encode("utf-8"), CSRF_TOKEN.encode("utf-8")):
                return self._send(403, {"error": "This page has expired. Reload Settings and try again."})
            try:
                apply_credentials(payload.get("values", {}))
            except SettingsError as e:
                return self._send(400, {"error": str(e)})
            if not DEMO:  # real keys: fetch fresh data with them right away
                threading.Thread(target=poll_once, daemon=True).start()
            return self._send(200, credentials_state())
        if path == "/monzo/connect":
            if not self._settings_gate():
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, {"error": "That didn't look like valid JSON."})
            token = self.headers.get("X-Settings-Token", "")
            if not secrets.compare_digest(token.encode("utf-8"), CSRF_TOKEN.encode("utf-8")):
                return self._send(403, {"error": "This page has expired. Reload Settings and try again."})
            pasted = str(payload.get("url") or payload.get("code") or "").strip()
            code = self._extract_monzo_code(pasted)
            if not code:
                return self._send(400, {"error": "Couldn't find an authorization code in that. "
                                                "Paste the full URL from the browser's address bar "
                                                "after approving Monzo — it contains ?code=…"})
            if not monzo_oauth():
                return self._send(400, {"error": "Add your Monzo client ID and secret (and Save) first."})
            try:
                monzo.exchange(code)
            except ApiError as e:
                return self._send(400, {"error": f"Monzo refused the code ({e.status}): "
                                                f"{humanise_monzo_error(e.message)}"})
            except Exception as e:
                return self._send(400, {"error": f"Sign-in failed unexpectedly ({type(e).__name__}). "
                                                "Try authorising again — codes only work once."})
            if not DEMO:
                threading.Thread(target=poll_once, daemon=True).start()
            return self._send(200, credentials_state())
        if path == "/api/refresh":
            if time.time() - STATE["last_poll_start"] > 8:  # Trading 212 allows 1 request / 5s
                poll_once()
            return self._send(200, summary())
        self._send(404, {"error": "Not found"})


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA, 0o700)  # snapshots, tokens and keys live in here
    except OSError:
        pass
    if DEMO:
        seed_demo()
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Net worth dashboard on http://{HOST}:{PORT}"
          + ("  (demo data)" if DEMO else ""))
    if (HOST not in ("127.0.0.1", "localhost", "::1") and not auth_user()
            and not os.environ.get("SUPERVISOR_TOKEN")):
        print("Warning: listening beyond localhost with no BASIC_AUTH_USER set. "
              "Anyone who can reach this port can see your balances.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
