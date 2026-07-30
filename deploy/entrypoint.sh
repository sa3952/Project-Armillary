#!/bin/sh
set -eu

# The hosted security posture (16 KiB body bound, JSON content-type gate,
# minimized error bodies, noindex header) is selected by the application
# profile.  load_settings() defaults to the permissive local profile when the
# variable is absent, so absence must be refused here rather than silently
# downgrading a deployed image.
: "${CLASSICAL_ASTROLOGY_PROFILE:?refusing to start without an explicit application profile}"
if [ "${CLASSICAL_ASTROLOGY_PROFILE}" != "private_alpha" ]; then
    echo "refusing to start: this image requires the private_alpha profile" >&2
    exit 1
fi

# Both bundled immutable datasets are verified before the listener starts.
# The place catalog is opened with immutable=1 at runtime, which instructs
# SQLite to trust the file, so its integrity gate is not optional.
/opt/venv/bin/python /app/scripts/verification/verify_ephemeris_integrity.py --check
/opt/venv/bin/python /app/scripts/verification/verify_place_catalog.py --check
exec "$@"
