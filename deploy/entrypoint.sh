#!/bin/sh
set -eu

/opt/venv/bin/python /app/scripts/verify_ephemeris_integrity.py --check
exec "$@"
