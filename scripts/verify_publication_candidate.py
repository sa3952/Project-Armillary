#!/usr/bin/env python3
"""Fail closed on incomplete or privately contaminated public source trees."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


TEXT_SUFFIXES = {
    ".c",
    ".css",
    ".h",
    ".html",
    ".in",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_PATH_PARTS = {
    ".DS_Store",
    ".env",
    ".pytest_cache",
    "__pycache__",
    "docs/archive",
    "docs/development",
    "docs/red_team",
}
FORBIDDEN_TEXT = {
    "/" + "Users" + "/",
    "treatment" + "-resistant",
    "@" + "gmail.com",
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[opsu]_[A-Za-z0-9_]{20,}\b"),
    "generic API token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
}


class CandidateFailure(RuntimeError):
    """Raised when a public candidate is unsafe or incomplete."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateFailure(f"cannot read {path}: {exc}") from exc


def verify_candidate(root: Path) -> None:
    root = root.resolve()
    required = {
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "backend/app/main.py",
        "backend/ephe/sepl_18.se1",
        "deploy/Dockerfile",
        "deploy/requirements.lock",
        "frontend/index.html",
        "third_party/SOURCE_MANIFEST.json",
        "third_party/SHA256SUMS",
        "third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz",
    }
    files = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }
    missing = sorted(required - files.keys())
    if missing:
        raise CandidateFailure(f"required files missing: {missing}")

    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise CandidateFailure(f"symlink is forbidden: {relative}")
        if any(
            relative == part or relative.startswith(part.rstrip("/") + "/")
            for part in FORBIDDEN_PATH_PARTS
        ):
            raise CandidateFailure(f"private path is forbidden: {relative}")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="strict")
            for marker in FORBIDDEN_TEXT:
                if marker.lower() in text.lower():
                    raise CandidateFailure(
                        f"private marker {marker!r} in {relative}"
                    )
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(text):
                    raise CandidateFailure(
                        f"possible {label} in {relative}"
                    )

    license_text = files["LICENSE"].read_text(encoding="utf-8")
    if (
        "GNU AFFERO GENERAL PUBLIC LICENSE" not in license_text
        or "Version 3, 19 November 2007" not in license_text
    ):
        raise CandidateFailure("LICENSE is not the expected AGPLv3 text")

    source_manifest = _load_json(files["third_party/SOURCE_MANIFEST.json"])
    if source_manifest.get("complete") is not True:
        raise CandidateFailure("third-party source manifest is incomplete")
    packages = source_manifest.get("packages", [])
    if not packages:
        raise CandidateFailure("third-party source manifest is empty")
    for package in packages:
        archive = root / "third_party" / "sources" / package["filename"]
        if not archive.is_file():
            raise CandidateFailure(
                f"source archive missing for {package['name']}"
            )
        if _sha256(archive) != package["sha256"]:
            raise CandidateFailure(
                f"source archive hash mismatch for {package['name']}"
            )

    dockerfile = files["deploy/Dockerfile"].read_text(encoding="utf-8")
    if "ADD --checksum=" in dockerfile or "files.pythonhosted.org" in dockerfile:
        raise CandidateFailure(
            "production build fetches pyswisseph instead of bundled source"
        )
    expected_copy = (
        "COPY third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz "
        "/source/pyswisseph-2.10.3.2.tar.gz"
    )
    if expected_copy not in dockerfile:
        raise CandidateFailure("Dockerfile does not use bundled pyswisseph")

    dockerignore = files[".dockerignore"].read_text(encoding="utf-8")
    for required_ignore in {
        ".git",
        "backend/tests",
        "frontend/tests",
        "docs",
        "third_party",
    }:
        if required_ignore not in dockerignore.splitlines():
            raise CandidateFailure(
                f".dockerignore omits {required_ignore}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify_candidate(args.root)
    except (CandidateFailure, OSError, UnicodeError) as exc:
        print(f"PUBLICATION CANDIDATE FAILED: {exc}", file=sys.stderr)
        return 1
    print("PUBLICATION CANDIDATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
