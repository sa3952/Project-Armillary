#!/usr/bin/env python3
"""Verify the exact runtime ephemeris inputs before serving requests."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "deploy" / "ephemeris.sha256"
EXPECTED_PATHS = frozenset(
    {
        "backend/ephe/seorbel.txt",
        "backend/ephe/seas_18.se1",
        "backend/ephe/sepl_18.se1",
        "backend/ephe/semo_18.se1",
        "backend/ephe/sefstars.txt",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
# This verifier runs both inside the image, where the ephemeris directory is
# materialized from `git archive` and cannot contain an untracked file, and
# against a developer working tree, where the operating system writes its own
# artifacts alongside the datasets.  Skipping this closed set of names keeps
# the in-image check exactly as strict: none of them can be present there.
IGNORABLE_LOCAL_ARTIFACT_NAMES = frozenset({".DS_Store"})


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify(manifest_path: Path) -> list[str]:
    errors: list[str] = []
    entries: dict[str, str] = {}
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [f"cannot read manifest: {exc.__class__.__name__}"]

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or SHA256_RE.fullmatch(parts[0]) is None:
            errors.append(f"invalid manifest line {line_number}")
            continue
        digest, relative_path = parts
        pure_path = PurePosixPath(relative_path)
        if (
            pure_path.is_absolute()
            or ".." in pure_path.parts
            or relative_path != pure_path.as_posix()
        ):
            errors.append(f"unsafe manifest path at line {line_number}")
            continue
        if relative_path in entries:
            errors.append(f"duplicate manifest path: {relative_path}")
            continue
        entries[relative_path] = digest

    missing_entries = EXPECTED_PATHS - entries.keys()
    unexpected_entries = entries.keys() - EXPECTED_PATHS
    for relative_path in sorted(missing_entries):
        errors.append(f"missing manifest entry: {relative_path}")
    for relative_path in sorted(unexpected_entries):
        errors.append(f"unexpected manifest entry: {relative_path}")

    ephemeris_dir = PROJECT_ROOT / "backend" / "ephe"
    try:
        actual_inputs = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in ephemeris_dir.iterdir()
            if path.name not in IGNORABLE_LOCAL_ARTIFACT_NAMES
        }
    except OSError as exc:
        errors.append(
            f"cannot enumerate ephemeris inputs: {exc.__class__.__name__}"
        )
        actual_inputs = set()
    for relative_path in sorted(actual_inputs - EXPECTED_PATHS):
        errors.append(f"unexpected ephemeris input: {relative_path}")

    for relative_path, expected_digest in sorted(entries.items()):
        path = PROJECT_ROOT / relative_path
        if path.is_symlink() or not path.is_file():
            errors.append(f"missing or non-regular input: {relative_path}")
            continue
        if _digest(path) != expected_digest:
            errors.append(f"digest mismatch: {relative_path}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
    )
    args = parser.parse_args()
    errors = verify(args.manifest.resolve())
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"OK: verified {len(EXPECTED_PATHS)} ephemeris inputs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
