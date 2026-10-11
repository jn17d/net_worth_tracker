#!/usr/bin/env bash
# Net Worth Tracker — Home Assistant add-on entry point.
#
# Maps the add-on's Options (/data/options.json) to the environment variables
# the server understands. Only options the user actually filled in are set, so
# anything left blank stays editable on the in-app Settings page.
set -e

export DATA_DIR="${DATA_DIR:-/data}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8787}"

if [ -f "${BASHIO_LIB:-/usr/lib/bashio/bashio.sh}" ]; then
    # shellcheck disable=SC1090
    . "${BASHIO_LIB:-/usr/lib/bashio/bashio.sh}"

    if bashio::config.true 'demo'; then
        export DEMO=1
    fi

    if bashio::config.has_value 't212_env'; then
        export T212_ENV="$(bashio::config 't212_env')"
    fi
    if bashio::config.has_value 'poll_minutes'; then
        export POLL_MINUTES="$(bashio::config 'poll_minutes')"
    fi
    if bashio::config.has_value 'monzo_client_id'; then
        export MONZO_CLIENT_ID="$(bashio::config 'monzo_client_id')"
    fi
    if bashio::config.has_value 'monzo_client_secret'; then
        export MONZO_CLIENT_SECRET="$(bashio::config 'monzo_client_secret')"
    fi
    if bashio::config.has_value 'monzo_redirect_uri'; then
        export MONZO_REDIRECT_URI="$(bashio::config 'monzo_redirect_uri')"
    fi

    # Trading 212 accounts: [{label, key, secret}, …] → T212_1_KEY, T212_1_SECRET, …
    if bashio::config.exists 't212_accounts'; then
        count="$(bashio::config 't212_accounts | length')"
        n=0
        if [ "${count:-0}" -gt 0 ] 2>/dev/null; then
            for i in $(seq 0 $((count - 1))); do
                key="$(bashio::config "t212_accounts[$i].key // empty")"
                secret="$(bashio::config "t212_accounts[$i].secret // empty")"
                if [ -n "${key}" ] && [ -n "${secret}" ]; then
                    n=$((n + 1))
                    export "T212_${n}_KEY=${key}" "T212_${n}_SECRET=${secret}"
                    label="$(bashio::config "t212_accounts[$i].label // empty")"
                    if [ -n "${label}" ]; then
                        export "T212_${n}_LABEL=${label}"
                    fi
                fi
            done
        fi
    fi
fi

cd "${APP_DIR:-/app}"
exec python3 -u server.py
