#!/usr/bin/env python3
"""Fail closed on incomplete or privately contaminated public source trees."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from scripts.publication.prepare_third_party_sources import (
    SourcePreparationError,
    render_dependency_license_table,
    verify_manifest_lock_inputs,
)


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
QUOTED_CANDIDATE_PATH = re.compile(r"`([A-Za-z0-9_./-]+)`")
PUBLIC_FILE_INVENTORY = "PUBLICATION_FILES.json"
SECURITY_TXT_PATH = ".well-known/security.txt"
RECONSTRUCTION_PLATFORM = "linux/amd64"
RECONSTRUCTION_BUILDER_IMAGE = (
    "python:3.13.14-trixie@"
    "sha256:153e964bee18ef816ff55c8b026a345c62d4ccf05ad119ce5d7c10dee79574d7"
)


class CandidateFailure(RuntimeError):
    """Raised when a public candidate is unsafe or incomplete."""


def _verify_notice_paths(root: Path, notice_path: Path) -> None:
    named = {
        token
        for token in QUOTED_CANDIDATE_PATH.findall(
            notice_path.read_text(encoding="utf-8")
        )
        if "/" in token and not token.startswith(("http:", "https:"))
    }
    if not named:
        raise CandidateFailure("third-party notices name no candidate paths")
    for relative in sorted(named):
        candidate = root / relative.rstrip("/")
        try:
            candidate.resolve().relative_to(root)
        except ValueError as error:
            raise CandidateFailure(f"notice path escapes candidate: {relative}") from error
        if not candidate.exists():
            raise CandidateFailure(f"notice path is absent from candidate: {relative}")


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


def _candidate_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    """Capture content, type and mode for every candidate entry.

    Only the candidate's own top-level Git administrative directory is
    excluded.  A nested ``.git`` path is publication payload and must remain
    observable.
    """
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        rendered = relative.as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            snapshot[rendered] = ("symlink", mode, os.readlink(path))
        elif stat.S_ISREG(metadata.st_mode):
            snapshot[rendered] = (
                "file",
                mode,
                metadata.st_size,
                _sha256(path),
            )
        elif stat.S_ISDIR(metadata.st_mode):
            snapshot[rendered] = ("directory", mode)
        else:
            snapshot[rendered] = ("special", mode, metadata.st_mode)
    return snapshot


def _verify_docker_context_without_candidate_side_effects(root: Path) -> None:
    before = _candidate_snapshot(root)
    with tempfile.TemporaryDirectory(
        prefix="publication-verifier-pycache-"
    ) as pycache:
        child_environment = os.environ.copy()
        child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        child_environment["PYTHONPYCACHEPREFIX"] = pycache
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
            env=child_environment,
        )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise CandidateFailure(f"closed Docker context failed: {detail}")

    # The child is the final executable boundary.  Re-run the manifest check
    # afterwards, then compare every candidate file byte-for-byte with the
    # pre-child snapshot.  This catches both undeclared cache files and edits
    # to declared inputs.
    _verify_public_file_inventory(root)
    after = _candidate_snapshot(root)
    if after != before:
        added = sorted(after.keys() - before.keys())
        removed = sorted(before.keys() - after.keys())
        changed = sorted(
            path for path in before.keys() & after.keys() if before[path] != after[path]
        )
        raise CandidateFailure(
            "candidate changed during verification: "
            f"added={added}, removed={removed}, changed={changed}"
        )


def _reconstruction_script(mode: str) -> str:
    common = r'''
set -eu
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_CACHE_DIR=1
export PYTHONDONTWRITEBYTECODE=1
python -m venv /work/venv
VENV=/work/venv/bin
export PATH="$VENV:$PATH"
'''
    if mode == "online-clean":
        install = r'''
"$VENV/pip" install --require-hashes \
  --requirement /candidate/backend/requirements-dev.lock
'''
    elif mode == "offline-source-only":
        install = r'''
"$VENV/pip" install --no-index \
  --find-links=/candidate/third_party/build_wheels \
  --require-hashes \
  --requirement /candidate/deploy/build-requirements.lock
"$VENV/pip" install --no-index \
  --find-links=/candidate/third_party/dependency_wheels \
  --find-links=/candidate/third_party/sources \
  --only-binary=:all: --no-binary=pyswisseph \
  --no-build-isolation --require-hashes \
  --requirement /candidate/backend/requirements-dev.lock
'''
    else:
        raise CandidateFailure(f"unsupported reconstruction mode: {mode}")
    # This gate runs linux/amd64 under QEMU on the current arm64 verifier.
    # The concurrency test has a product timeout and is replayed in the native
    # fresh-environment gate; including it here tests emulation speed, not the
    # dependency reconstruction invariant.
    test_filter = "-k 'not test_concurrent_requests_do_not_cross_contaminate'"
    checks = rf'''
cd /candidate
PYTHONPATH=.:backend "$VENV/python" -m pytest \
  -p no:cacheprovider \
  tests/backend/test_chart_api.py \
  tests/backend/test_calculation_dossier.py -q {test_filter}
"$VENV/python" -m scripts.verification.build_sbom \
  --output deploy/sbom.cyclonedx.json --check
PYTHONPATH=.:backend "$VENV/python" -m uvicorn \
  app.main:create_app --factory --app-dir /candidate/backend \
  --host 127.0.0.1 --port 8765 --no-access-log >/work/server.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
"$VENV/python" -c 'import json,time,urllib.request
for attempt in range(100):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=1) as response:
            assert response.status == 200
            payload = json.load(response)
            assert payload["status"] == "ok"
            if payload.get("status") != "ok" or payload.get("ready") is not True:
                raise RuntimeError("candidate health response is not ready")
            break
    except Exception:
        if attempt == 99: raise
        time.sleep(0.1)'
kill "$server_pid"
wait "$server_pid" || true
trap - EXIT
'''
    return common + install + checks


def verify_dependency_reconstruction(
    root: Path,
    *,
    mode: str,
    builder_image: str,
) -> None:
    if not builder_image or any(character.isspace() for character in builder_image):
        raise CandidateFailure("builder image identity is missing or unsafe")
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            RECONSTRUCTION_PLATFORM,
            "--network",
            "none" if mode == "offline-source-only" else "bridge",
            "--volume",
            f"{root.resolve()}:/candidate:ro",
            "--tmpfs",
            "/work:exec,size=4g",
            builder_image,
            "sh",
            "-c",
            _reconstruction_script(mode),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = (completed.stderr + "\n" + completed.stdout).strip()
        raise CandidateFailure(
            f"{mode} dependency reconstruction failed: {detail[-8000:]}"
        )


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


def _verify_security_txt(root: Path) -> None:
    path = root / SECURITY_TXT_PATH
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 4096:
        raise CandidateFailure("security.txt is missing or unsafe")
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise CandidateFailure(f"security.txt is unreadable: {error}") from None
    entries: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line or ": " not in line:
            raise CandidateFailure("security.txt contains an invalid line")
        key, value = line.split(": ", 1)
        entries.setdefault(key, []).append(value)
    expected = {
        "Contact": [
            "https://github.com/sa3952/Project-Armillary/security/advisories/new",
            "mailto:privacy@projectarmillary.com",
        ],
        "Preferred-Languages": ["zh-TW, en"],
        "Canonical": [
            "https://projectarmillary.com/.well-known/security.txt"
        ],
        "Policy": [
            "https://github.com/sa3952/Project-Armillary/blob/main/SECURITY.md"
        ],
    }
    if any(entries.get(key) != value for key, value in expected.items()):
        raise CandidateFailure("security.txt contact, canonical or policy is invalid")
    if set(entries) != set(expected) | {"Expires"} or len(entries["Expires"]) != 1:
        raise CandidateFailure("security.txt field set is invalid")
    try:
        expires = datetime.fromisoformat(
            entries["Expires"][0].replace("Z", "+00:00")
        )
    except ValueError:
        expires = None
    now = datetime.now(timezone.utc)
    if (
        expires is None
        or expires.tzinfo is None
        or expires <= now
        or expires > now + timedelta(days=366)
    ):
        raise CandidateFailure("security.txt expiry is invalid")
    if "security@" in text.casefold():
        raise CandidateFailure("security.txt names an unavailable contact")


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
        SECURITY_TXT_PATH,
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
        "deploy/sbom.cyclonedx.json",
        "backend/requirements.lock",
        "backend/requirements-dev.lock",
        "deploy/build-requirements.lock",
        "docs/DEPENDENCY_LICENSES.md",
        "frontend/zh-TW/index.html",
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
    _verify_security_txt(root)
    _verify_notice_paths(root, files["THIRD_PARTY_NOTICES.md"])

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
    if source_manifest.get("reconstruction_complete") is not True:
        raise CandidateFailure(
            "third-party source manifest lacks the verified build wheel index"
        )
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
    try:
        verify_manifest_lock_inputs(
            source_manifest,
            {
                "production": files["deploy/requirements.lock"],
                "build": files["deploy/build-requirements.lock"],
                "development": files["backend/requirements-dev.lock"],
            },
        )
        expected_license_table = render_dependency_license_table(
            source_manifest
        )
    except SourcePreparationError as exc:
        raise CandidateFailure(str(exc)) from exc
    if (
        files["docs/DEPENDENCY_LICENSES.md"].read_text(encoding="utf-8")
        != expected_license_table
    ):
        raise CandidateFailure(
            "human-readable dependency license table has drifted"
        )
    source_paths = {
        f"sources/{package['filename']}" for package in packages
    }
    build_wheel_index = source_manifest.get("build_wheel_index")
    if not isinstance(build_wheel_index, dict) or build_wheel_index.get(
        "complete"
    ) is not True:
        raise CandidateFailure("verified build wheel index is missing")
    build_wheel_paths = set()
    for artifact in build_wheel_index.get("artifacts", []):
        relative = f"build_wheels/{artifact['filename']}"
        path = root / "third_party" / relative
        if not path.is_file() or _sha256(path) != artifact.get("sha256"):
            raise CandidateFailure(
                f"build wheel index mismatch for {artifact.get('filename')}"
            )
        source_path = root / "third_party" / "sources" / str(
            artifact.get("source_sdist_filename", "")
        )
        if (
            not source_path.is_file()
            or _sha256(source_path) != artifact.get("source_sdist_sha256")
        ):
            raise CandidateFailure(
                f"build wheel lacks retained sdist binding for {artifact.get('filename')}"
            )
        build_wheel_paths.add(relative)
    dependency_wheel_index = source_manifest.get("dependency_wheel_index")
    if not isinstance(
        dependency_wheel_index, dict
    ) or dependency_wheel_index.get("complete") is not True:
        raise CandidateFailure("verified dependency wheel index is missing")
    dependency_wheel_paths = set()
    for artifact in dependency_wheel_index.get("artifacts", []):
        relative = f"dependency_wheels/{artifact['filename']}"
        path = root / "third_party" / relative
        if not path.is_file() or _sha256(path) != artifact.get("sha256"):
            raise CandidateFailure(
                f"dependency wheel index mismatch for {artifact.get('filename')}"
            )
        source_path = root / "third_party" / "sources" / str(
            artifact.get("source_sdist_filename", "")
        )
        if (
            not source_path.is_file()
            or _sha256(source_path) != artifact.get("source_sdist_sha256")
        ):
            raise CandidateFailure(
                "dependency wheel lacks retained sdist binding for "
                f"{artifact.get('filename')}"
            )
        dependency_wheel_paths.add(relative)
    _verify_sha256sums(
        root / "third_party",
        files["third_party/SHA256SUMS"],
        source_paths | build_wheel_paths | dependency_wheel_paths,
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

    _verify_docker_context_without_candidate_side_effects(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--reconstruction-mode",
        choices=("online-clean", "offline-source-only"),
    )
    parser.add_argument(
        "--builder-image",
        default=RECONSTRUCTION_BUILDER_IMAGE,
        help=(
            "digest-pinned builder; defaults to the documented linux/amd64 "
            "image containing the C toolchain required by pyswisseph"
        ),
    )
    args = parser.parse_args()
    try:
        verify_candidate(args.root)
        if args.reconstruction_mode:
            verify_dependency_reconstruction(
                args.root,
                mode=args.reconstruction_mode,
                builder_image=args.builder_image,
            )
    except (CandidateFailure, OSError, UnicodeError) as exc:
        print(f"PUBLICATION CANDIDATE FAILED: {exc}", file=sys.stderr)
        return 1
    print("PUBLICATION CANDIDATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
