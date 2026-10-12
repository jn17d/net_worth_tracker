#!/usr/bin/env bash
# Net Worth Tracker — Home Assistant add-on entry point.
#
# All configuration (Trading 212 keys, Monzo OAuth, poll interval, …) lives on
# the app's own Settings page, stored under /data. This script only sets the
# runtime defaults and starts the server.
set -e

export DATA_DIR="${DATA_DIR:-/data}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8787}"

cd "${APP_DIR:-/app}"
exec python3 -u server.py
