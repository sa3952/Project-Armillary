#!/usr/bin/env python3
"""Fail closed on incomplete or privately contaminated public source trees."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
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
LITERAL_REMOTE_URL = re.compile(r"https?://", re.IGNORECASE)
PUBLIC_FILE_INVENTORY = "PUBLICATION_FILES.json"


class CandidateFailure(RuntimeError):
    """Raised when a public candidate is unsafe or incomplete."""


def _has_forbidden_path_part(relative: Path) -> bool:
    rendered = relative.as_posix()
    return any(
        (
            forbidden in relative.parts
            if "/" not in forbidden
            else rendered == forbidden
            or rendered.startswith(forbidden.rstrip("/") + "/")
        )
        for forbidden in FORBIDDEN_PATH_PARTS
    )


def _has_literal_remote_url(text: str) -> bool:
    return LITERAL_REMOTE_URL.search(text) is not None


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


def _validate_inventory_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise CandidateFailure("public file inventory contains an invalid path")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != raw:
        raise CandidateFailure(f"unsafe public file inventory path: {raw!r}")
    return raw


def _verify_public_file_inventory(root: Path) -> None:
    inventory_path = root / PUBLIC_FILE_INVENTORY
    payload = _load_json(inventory_path)
    if (
        payload.get("schema_version") != 1
        or payload.get("hash_algorithm") != "sha256"
    ):
        raise CandidateFailure("public file inventory metadata is invalid")
    raw_entries = payload.get("files")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CandidateFailure("public file inventory is empty")

    expected: dict[str, tuple[str, int]] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise CandidateFailure("public file inventory entry is invalid")
        relative = _validate_inventory_path(entry.get("path"))
        sha256 = entry.get("sha256")
        size_bytes = entry.get("size_bytes")
        if relative == PUBLIC_FILE_INVENTORY or relative in expected:
            raise CandidateFailure(
                f"duplicate or self-referential inventory path: {relative}"
            )
        if not isinstance(sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", sha256
        ):
            raise CandidateFailure(
                f"invalid public file hash for {relative}"
            )
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise CandidateFailure(
                f"invalid public file size for {relative}"
            )
        expected[relative] = (sha256, size_bytes)

    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }
    expected_paths = set(expected) | {PUBLIC_FILE_INVENTORY}
    missing = sorted(expected_paths - actual.keys())
    if missing:
        raise CandidateFailure(f"public inventory files missing: {missing}")
    unexpected = sorted(actual.keys() - expected_paths)
    if unexpected:
        raise CandidateFailure(f"unexpected public files: {unexpected}")
    for relative, (expected_hash, expected_size) in expected.items():
        path = actual[relative]
        if path.stat().st_size != expected_size:
            raise CandidateFailure(
                f"public file size mismatch for {relative}"
            )
        if _sha256(path) != expected_hash:
            raise CandidateFailure(
                f"public file hash mismatch for {relative}"
            )


def _license_evidence_complete(package: dict) -> bool:
    has_identity = bool(
        package.get("license_expression")
        or package.get("license_metadata")
        or package.get("license_classifiers")
    )
    license_files = package.get("license_files")
    license_hashes = package.get("license_file_sha256")
    return bool(
        has_identity
        and isinstance(license_files, list)
        and license_files
        and isinstance(license_hashes, dict)
        and set(license_files) == set(license_hashes)
        and all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in license_hashes.values()
        )
    )


def _verify_license_files(archive: Path, package: dict) -> None:
    if not _license_evidence_complete(package):
        raise CandidateFailure(
            f"license evidence incomplete for {package.get('name')}"
        )
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = {
                member.name: member
                for member in source.getmembers()
                if member.isfile()
            }
            for relative in package["license_files"]:
                member = members.get(relative)
                if member is None:
                    raise CandidateFailure(
                        "license file missing from source archive for "
                        f"{package['name']}: {relative}"
                    )
                handle = source.extractfile(member)
                if handle is None:
                    raise CandidateFailure(
                        f"cannot read license file for {package['name']}"
                    )
                actual = hashlib.sha256(handle.read()).hexdigest()
                if actual != package["license_file_sha256"][relative]:
                    raise CandidateFailure(
                        "license file hash mismatch for "
                        f"{package['name']}: {relative}"
                    )
    except (OSError, tarfile.TarError) as exc:
        raise CandidateFailure(
            f"cannot inspect license evidence for {package['name']}: {exc}"
        ) from exc


def _verify_sha256sums(
    base: Path,
    sums_path: Path,
    expected_paths: set[str] | None = None,
) -> None:
    entries: dict[str, str] = {}
    try:
        lines = sums_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CandidateFailure(f"cannot read {sums_path}: {exc}") from exc
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\s]+)", line)
        if match is None:
            raise CandidateFailure(f"invalid SHA256SUMS line: {line!r}")
        relative = _validate_inventory_path(match.group(2))
        if relative in entries:
            raise CandidateFailure(f"duplicate SHA256SUMS path: {relative}")
        entries[relative] = match.group(1)
    if expected_paths is not None and set(entries) != expected_paths:
        raise CandidateFailure("SHA256SUMS paths do not match source manifest")
    for relative, expected_hash in entries.items():
        path = base / relative
        if not path.is_file() or _sha256(path) != expected_hash:
            raise CandidateFailure(f"SHA256SUMS mismatch for {relative}")


def verify_candidate(root: Path) -> None:
    root = root.resolve()
    required = {
        "LICENSE",
        PUBLIC_FILE_INVENTORY,
        "README.md",
        "SECURITY.md",
        "SOURCE_EXPORT.json",
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
    _verify_public_file_inventory(root)

    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise CandidateFailure(f"symlink is forbidden: {relative}")
        if _has_forbidden_path_part(path.relative_to(root)):
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
        _verify_license_files(archive, package)
    source_paths = {
        f"sources/{package['filename']}" for package in packages
    }
    _verify_sha256sums(
        root / "third_party",
        files["third_party/SHA256SUMS"],
        source_paths,
    )
    _verify_sha256sums(
        root / "third_party" / "pyswisseph",
        root / "third_party" / "pyswisseph" / "SHA256SUMS",
    )

    export_receipt = _load_json(files["SOURCE_EXPORT.json"])
    if export_receipt.get("export_mode") != "closed_allowlist":
        raise CandidateFailure("source export was not closed-allowlist")
    if (
        export_receipt.get("status")
        != "public_revision_intentionally_out_of_band"
        or not isinstance(
            export_receipt.get("public_revision_source"), str
        )
    ):
        raise CandidateFailure("source export revision status is misleading")
    private_revision = export_receipt.get("private_source_revision")
    if not isinstance(private_revision, str) or not re.fullmatch(
        r"[0-9a-f]{40}", private_revision
    ):
        raise CandidateFailure("private source revision is missing or invalid")

    dockerfile = files["deploy/Dockerfile"].read_text(encoding="utf-8")
    if "ADD --checksum=" in dockerfile:
        raise CandidateFailure(
            "production build fetches pyswisseph instead of bundled source"
        )
    if _has_literal_remote_url(dockerfile):
        raise CandidateFailure(
            "production Dockerfile contains a literal remote URL"
        )
    expected_copy = (
        "COPY third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz "
        "/source/pyswisseph-2.10.3.2.tar.gz"
    )
    if expected_copy not in dockerfile:
        raise CandidateFailure("Dockerfile does not use bundled pyswisseph")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.verification.verify_docker_context",
            "--check",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise CandidateFailure(f"closed Docker context failed: {detail}")


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
