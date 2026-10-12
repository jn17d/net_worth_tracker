#!/usr/bin/env bash
# Net Worth Tracker — Home Assistant add-on entry point.
#
# Maps the add-on's Options (/data/options.json) to the environment variables
# the server understands. Only options the user actually filled in are set, so
# anything left blank stays editable on the in-app Settings page.
#
# Options are parsed straight from /data/options.json with python3 (which the
# container ships with) rather than via bashio's Supervisor API calls. This
# avoids the "Unable to access the API, forbidden" failure mode entirely and
# keeps the add-on working even if API access is denied.
set -e

export DATA_DIR="${DATA_DIR:-/data}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8787}"

export OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"

# Export env vars from options.json. A python one-liner prints shell-safe
# `export KEY=value` lines for every option that has a non-empty value, and we
# eval them. List options (t212_accounts) become T212_N_KEY/T212_N_SECRET/etc.
parsed="$(python3 - "$OPTIONS_FILE" <<'PYEOF'
import json, shlex, sys

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        opts = json.load(f)
except Exception:
    sys.exit(0)

SCALARS = ("demo", "t212_env", "poll_minutes",
           "monzo_client_id", "monzo_client_secret")

def emit(key, value):
    print(f"export {key}={shlex.quote(str(value))}")

for name in SCALARS:
    value = opts.get(name)
    if value in (None, "", []):
        continue
    if name == "demo":
        if value:
            emit("DEMO", 1)
        continue
    emit(name.upper(), value)

accounts = opts.get("t212_accounts") or []
n = 0
for acct in accounts:
    if not isinstance(acct, dict):
        continue
    key = (acct.get("key") or "").strip()
    secret = (acct.get("secret") or "").strip()
    if not key or not secret:
        continue
    n += 1
    emit(f"T212_{n}_KEY", key)
    emit(f"T212_{n}_SECRET", secret)
    label = (acct.get("label") or "").strip()
    if label:
        emit(f"T212_{n}_LABEL", label)
PYEOF
)"

if [ -n "${parsed}" ]; then
    # shellcheck disable=SC2163
    eval "${parsed}"
fi

cd "${APP_DIR:-/app}"
exec python3 -u server.py
