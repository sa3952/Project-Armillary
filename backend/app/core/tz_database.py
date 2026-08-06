"""Identify the IANA time zone database the running process is using.

Every chart converts a local birth time to UTC through an IANA zone, so the tz
database is an *input* to the calculation in exactly the way the ephemeris files
are.  The Calculation Dossier recorded the pyswisseph and Swiss Ephemeris
versions and not this one, which made it possible for the same request, on the
same code, to produce a different chart with nothing in the receipt changing.

That is not hypothetical.  IANA publishes several releases a year and a large
share of them *correct historical offsets* — a zone's UTC offset for a date in
1953 is a research finding, not a constant, and it gets revised.  A birth in a
revised window converts to a different UTC, and therefore to a different
Ascendant, different houses, different everything.

The version is also not pinned here: on macOS the database comes from the
operating system, so a system update changes it silently.  In the container it
comes from whichever tzdata the image has.  Recording it does not stop it
changing; it makes the change visible when someone asks why a chart moved.

Resolution order, most to least specific:

1. the ``tzdata`` PyPI package, when installed;
2. the ``+VERSION`` file shipped in a zoneinfo directory;
3. the ``# version`` line of ``tzdata.zi``;
4. otherwise ``unavailable``, with a reason code, never a silent omission.
"""
from __future__ import annotations

import importlib.metadata
import zoneinfo
from pathlib import Path
from typing import Final

# "2026b", "2024a" and so on.  Two or three digits are not valid tzdata
# releases, so a loose match here would let a truncated read through.
_VERSION_FILES: Final = ("+VERSION", "tzdata.zi")


def _from_pip() -> tuple[str, str] | None:
    try:
        version = importlib.metadata.version("tzdata")
    except importlib.metadata.PackageNotFoundError:
        return None
    return version, "pypi_tzdata_package"


def _from_zoneinfo_files() -> tuple[str, str] | None:
    for base in zoneinfo.TZPATH:
        for filename in _VERSION_FILES:
            path = Path(base) / filename
            try:
                first_line = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            first_line = first_line.splitlines()[0] if first_line else ""
            # "+VERSION" holds the bare version; tzdata.zi starts "# version 2026b".
            candidate = first_line.replace("# version", "").strip()
            if candidate:
                return candidate, f"zoneinfo_file:{filename}"
    return None


def resolve_tz_database_version() -> dict:
    """Return a receipt for the tz database, never an omission."""

    for resolver in (_from_pip, _from_zoneinfo_files):
        found = resolver()
        if found is not None:
            version, source = found
            return {
                "version": version,
                "source": source,
                "available": True,
                "reason_code": None,
            }
    return {
        "version": None,
        "source": None,
        "available": False,
        # The chart is still computed — the conversion happened, we just cannot
        # name which database performed it.  That is a weaker receipt, not a
        # failed calculation, so it is reported rather than raised.
        "reason_code": "tz_database_version_not_identifiable",
    }


# Resolved once at import: the database does not change inside a process, and a
# per-request stat of four directories would buy nothing.  A system update that
# replaces it takes effect on the next restart, which is also when the recorded
# version changes — the two stay consistent with each other.
TZ_DATABASE: Final = resolve_tz_database_version()
