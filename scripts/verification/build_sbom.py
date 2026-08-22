#!/usr/bin/env python3
"""Produce a CycloneDX SBOM for what this product actually ships.

The repository already demanded provenance evidence for vendored third-party
source and verified the container image's supply chain, while having no bill of
materials of its own — the asymmetry finding F-4 raised on 2026-08-04, of which
the lockfiles were only half.  A lockfile says what pip should install; an SBOM
says what is in the delivered thing, in a format a reader outside this project
can consume.

Scope is deliberately the *runtime* set, not the development set.  The
distinction matters for AGPL correspondence: what has to be offered is the
source of the program being run, and pytest is not part of it.  Development
dependencies are recorded separately and marked as such rather than omitted, so
the file cannot be misread as "these are all the packages involved".

The component list is read from the hash-pinned lockfiles rather than from the
installed environment.  The installed environment is whatever happens to be on
one machine; the lockfile is what any environment is required to reproduce.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from packaging.requirements import InvalidRequirement, Requirement

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# `CLA-2026-08-06-B`, second half.  The guard now compares artifact hashes
# across the two locks, but it still had to compare them because the SBOM was
# built from `backend/requirements.lock` while the Dockerfile installs from
# `deploy/requirements.lock`.  Two files that must stay identical by
# maintenance is a standing hazard, and the answer to "which one is
# authoritative" is not a matter of taste: the SBOM is a bill of materials for
# the shipped image, so it has to be derived from the file the image installs.
# The cross-lock guard stays, now as a check that the development lock has not
# drifted from the shipped one rather than as the only thing holding the
# shipped claim together.
RUNTIME_LOCK = PROJECT_ROOT / "deploy" / "requirements.lock"
DEV_LOCK = PROJECT_ROOT / "backend" / "requirements-dev.lock"

_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def _components(lock: Path, scope: str) -> list[dict]:
    components: list[dict] = []
    logical: list[str] = []
    buffer = ""
    for raw in lock.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("--", "-r", "-c")) and not buffer:
            raise ValueError(f"unsupported top-level lock directive: {stripped}")
        buffer += (" " if buffer else "") + stripped.removesuffix("\\").strip()
        if not stripped.endswith("\\"):
            logical.append(buffer)
            buffer = ""
    if buffer:
        raise ValueError("unterminated lock requirement continuation")
    for record in logical:
        requirement_text = record.split("--hash=", 1)[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as error:
            raise ValueError(f"cannot parse lock requirement: {requirement_text}") from error
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise ValueError(f"lock requirement is not exactly pinned: {requirement_text}")
        hashes = _HASH.findall(record)
        if not hashes:
            raise ValueError(f"lock requirement has no SHA-256: {requirement_text}")
        component = {
            "type": "library",
            "name": requirement.name,
            "version": pins[0].version,
            "purl": f"pkg:pypi/{requirement.name}@{pins[0].version}",
            "scope": scope,
            "hashes": [
                {"alg": "SHA-256", "content": digest}
                for digest in hashes
            ],
        }
        if requirement.marker is not None:
            component["properties"] = [
                {"name": "pep508_marker", "value": str(requirement.marker)}
            ]
        components.append(component)
    return components


def _lock_digest_properties() -> list[dict]:
    """Record what the component list is actually derived from.

    This replaced a ``source_revision`` property carrying ``git rev-parse
    HEAD``.  That claim could not be true of this file: the SBOM is a pure
    function of the two lockfiles, so it does not change when the commit does,
    and the committed copy therefore named whatever commit happened to be
    checked out when someone last regenerated it.  ``--check`` had to strip
    the property to avoid failing on every commit, which meant a wrong value
    could never be detected — the artifact asserted provenance that no gate
    verified (`PIA-2026-08-06-003`).

    The lockfile digest is the identity that is both true and falsifiable:
    edit a lock without regenerating, and the check fails.
    """
    return [
        {
            "name": f"lock_digest:{lock.name}",
            "value": hashlib.sha256(lock.read_bytes()).hexdigest(),
        }
        for lock in (RUNTIME_LOCK, DEV_LOCK)
    ]


def build() -> dict:
    runtime = _components(RUNTIME_LOCK, "required")
    development = _components(DEV_LOCK, "optional")
    # dev lock is a superset of the runtime lock; keep only what it adds, so a
    # reader is not told a package is both required and optional.
    runtime_names = {component["name"] for component in runtime}
    development = [c for c in development if c["name"] not in runtime_names]

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "classical-astrology-app",
                "description": (
                    "Classical Western astrology astronomical calculation service"
                ),
            },
            "properties": [
                *_lock_digest_properties(),
                {
                    "name": "component_source",
                    "value": (
                        "deploy/requirements.lock (the file the image installs "
                        "from) and backend/requirements-dev.lock, not the "
                        "installed environment"
                    ),
                },
                {
                    "name": "scope_semantics",
                    "value": (
                        "required = shipped and executed at runtime; optional = "
                        "development and test only, not part of the AGPL "
                        "corresponding source of the running program"
                    ),
                },
                {
                    "name": "limitations",
                    "value": (
                        "PyPI packages only. It does not cover the Swiss "
                        "Ephemeris data files, the vendored third-party source "
                        "under third_party/, the base image's OS packages, or "
                        "the IANA time zone database — each of which has its "
                        "own evidence path and none of which are pip packages."
                    ),
                },
            ],
        },
        "components": runtime + development,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed SBOM differs from a fresh build",
    )
    args = parser.parse_args(argv)

    document = build()
    rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    output = Path(args.output)

    if args.check:
        if not output.is_file():
            print(f"SBOM MISSING: {output}")
            return 1
        committed = output.read_text(encoding="utf-8")
        # Nothing is excluded from the comparison any more.  The properties
        # are now all functions of the lockfiles, so a freshly built document
        # is byte-comparable with the committed one; excluding a field is what
        # let a false provenance value survive the gate.
        try:
            committed_payload = json.loads(committed)
        except json.JSONDecodeError:
            print("SBOM UNREADABLE: committed file is not valid JSON")
            return 1
        if json.dumps(committed_payload, sort_keys=True) != json.dumps(
            document, sort_keys=True
        ):
            print(
                "SBOM STALE: committed document differs from a fresh build "
                "of the lockfiles"
            )
            return 1
        counts = json.loads(committed)["components"]
        print(f"SBOM CURRENT: {len(counts)} components")
        return 0

    output.write_text(rendered, encoding="utf-8")
    required = sum(1 for c in document["components"] if c["scope"] == "required")
    optional = len(document["components"]) - required
    print(
        f"SBOM WRITTEN: {output} — {required} runtime, {optional} development-only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
