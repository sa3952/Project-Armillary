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
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from scripts.publication.prepare_third_party_sources import (
    SourcePreparationError,
    license_evidence_complete,
    render_dependency_license_table,
    verify_manifest_lock_inputs,
)
from scripts.tools.source_tree_identity import observe_publication_tree, sha256_file as _sha256
from scripts.tools.closed_set import ClosedSetError, require_closed_set


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
    "信任說明草案待 " + "Sebastian 定稿",
}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"\bgh[opsu]_[A-Za-z0-9_]{20,}\b"),
    "GitHub fine-grained token": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Google API key": re.compile(rb"\bAIza[A-Za-z0-9_-]{35}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Stripe live key": re.compile(rb"\bsk_live_[A-Za-z0-9]{20,}\b"),
    "generic API token": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
}
LITERAL_REMOTE_URL = re.compile(r"https?://", re.IGNORECASE)
QUOTED_CANDIDATE_PATH = re.compile(r"`([A-Za-z0-9_./-]+)`")
PUBLIC_FILE_INVENTORY = "PUBLICATION_FILES.json"
SECURITY_TXT_PATH = ".well-known/security.txt"
RECONSTRUCTION_PLATFORM = "linux/amd64"
RECONSTRUCTION_BUILDER_IMAGE = (
    "python:3.14.7-trixie@"
    "sha256:48651f00145ad01e9f83d468c57cec40fac72081950f9730205b87abc6087552"
)
RECONSTRUCTION_TEST_PATHS = (
    "tests/integration/test_hosted_live_server.py",
    "tests/integration/test_hosted_profile.py",
)


class CandidateFailure(RuntimeError):
    """Raised when a public candidate is unsafe or incomplete."""


def scan_candidate_content(
    root: Path,
    *,
    forbidden_markers: set[str],
    secret_patterns: dict[str, re.Pattern[bytes]] | None = None,
) -> None:
    """Scan every regular candidate byte stream, independent of suffix."""

    literals = {
        marker: marker.encode("utf-8").lower()
        for marker in forbidden_markers
    }
    patterns = secret_patterns or {}
    overlap = max([512, *(len(value) for value in literals.values())])
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if not path.is_file() or ".git" in relative_path.parts:
            continue
        tail = b""
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    window = tail + chunk
                    lowered = window.lower()
                    for marker, encoded in literals.items():
                        if encoded in lowered:
                            raise CandidateFailure(
                                f"private marker {marker!r} in {relative_path.as_posix()}"
                            )
                    for label, pattern in patterns.items():
                        if pattern.search(window):
                            raise CandidateFailure(
                                f"possible {label} in {relative_path.as_posix()}"
                            )
                    tail = window[-overlap:]
        except OSError as error:
            raise CandidateFailure(
                f"cannot scan candidate content {relative_path.as_posix()}: {error}"
            ) from None


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
    tests = " \\\n  ".join(RECONSTRUCTION_TEST_PATHS)
    checks = rf'''
cd /candidate
PYTHONPATH=.:backend "$VENV/python" -m pytest \
  -p no:cacheprovider \
  {tests} -q {test_filter}
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


def _verify_vendored_license_closure(root: Path) -> None:
    """Require every distinct license in the built sdist to reach delivery."""

    vendor = root / "third_party" / "pyswisseph"
    archive_path = vendor / "pyswisseph-2.10.3.2.tar.gz"
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            required: dict[str, str] = {}
            for member in archive.getmembers():
                basename = PurePosixPath(member.name).name.casefold()
                if not member.isfile() or not basename.startswith(
                    ("license", "copying", "notice")
                ):
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise CandidateFailure(
                        f"cannot read vendored license member: {member.name}"
                    )
                required[member.name] = hashlib.sha256(extracted.read()).hexdigest()
    except (OSError, tarfile.TarError) as error:
        raise CandidateFailure(f"cannot inspect vendored source licenses: {error}") from error
    delivered = {
        _sha256(path)
        for path in vendor.rglob("*")
        if path.is_file()
        and path != archive_path
        and path.name != "SHA256SUMS"
    }
    missing = sorted(
        member for member, digest in required.items() if digest not in delivered
    )
    if missing:
        raise CandidateFailure(
            "vendored compiled source license material is absent from delivery: "
            + ", ".join(missing)
        )


def verify_candidate_reference_graph(root: Path) -> None:
    """Resolve executable and vendored-license references inside the candidate."""

    missing = [path for path in RECONSTRUCTION_TEST_PATHS if not (root / path).is_file()]
    if missing:
        raise CandidateFailure(
            "reconstruction references absent candidate paths: " + ", ".join(missing)
        )
    _verify_vendored_license_closure(root)


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
    try:
        observe_publication_tree(root)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise CandidateFailure(str(error)) from None


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


def _verify_discovery_surfaces(root: Path) -> None:
    manifest = _load_json(root / "frontend/surfaces.json")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("origin") != "https://projectarmillary.com"
        or not isinstance(manifest.get("surfaces"), list)
    ):
        raise CandidateFailure("frontend surface manifest is invalid")
    origin = manifest["origin"]
    indexable: set[str] = set()
    outputs: set[str] = set()
    for item in manifest["surfaces"]:
        if not isinstance(item, dict):
            raise CandidateFailure("frontend surface row is invalid")
        output = item.get("output")
        surface = item.get("surface")
        title = item.get("title")
        description = item.get("description")
        if not (
            isinstance(output, str) and output
            and isinstance(surface, str) and surface
            and isinstance(title, str) and title
            and isinstance(description, str) and description
        ):
            raise CandidateFailure("frontend surface identity is invalid")
        if output in outputs:
            raise CandidateFailure("frontend surface output is duplicated")
        outputs.add(output)
        path = root / "frontend" / output
        if not path.is_file() or path.is_symlink():
            raise CandidateFailure(f"frontend surface output is missing: {output}")
        text = path.read_text(encoding="utf-8")
        canonical = origin + surface
        expected = (
            f"<title>{title}</title>",
            f'<meta name="description" content="{description}">',
            f'<link rel="canonical" href="{canonical}">',
            f'<meta property="og:url" content="{canonical}">',
        )
        if any(text.count(token) != 1 for token in expected):
            raise CandidateFailure(f"frontend discovery metadata differs: {surface}")
        robots = (
            "index, follow"
            if item.get("indexable") is True
            else "noindex, follow"
        )
        if text.count(f'<meta name="robots" content="{robots}">') != 1:
            raise CandidateFailure(f"frontend indexing policy differs: {surface}")
        if item.get("indexable") is True:
            indexable.add(canonical)

    robots_path = root / "frontend/robots.txt"
    sitemap_path = root / "frontend/sitemap.xml"
    # The surface manifest and its generator own the indexing posture.
    from scripts.frontend.build_pages import robots_text

    if robots_path.read_text(encoding="ascii") != robots_text(manifest):
        raise CandidateFailure("robots.txt differs from its declared posture")
    try:
        tree = ET.parse(sitemap_path)
    except (OSError, ET.ParseError) as error:
        raise CandidateFailure(f"sitemap.xml is invalid: {error}") from None
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    sitemap_urls = {
        element.text
        for element in tree.findall(f"{namespace}url/{namespace}loc")
    }
    if sitemap_urls != indexable:
        raise CandidateFailure("sitemap URL set differs from indexable surfaces")

    key = manifest.get("indexnow_key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,128}", key):
        raise CandidateFailure("IndexNow key is invalid")
    key_path = root / "frontend" / f"{key}.txt"
    if key_path.read_text(encoding="ascii") != key + "\n":
        raise CandidateFailure("IndexNow verification file differs")


def _verify_license_files(archive: Path, package: dict) -> None:
    if not license_evidence_complete(package):
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
    if expected_paths is not None:
        try:
            require_closed_set(
                expected_paths,
                entries,
                role=f"{sums_path.name} source paths",
            )
        except ClosedSetError as error:
            raise CandidateFailure(str(error)) from None
    elif not entries:
        raise CandidateFailure("SHA256SUMS path set is empty")
    for relative, expected_hash in entries.items():
        path = base / relative
        if not path.is_file() or _sha256(path) != expected_hash:
            raise CandidateFailure(f"SHA256SUMS mismatch for {relative}")


def verify_candidate(
    root: Path,
    third_party_source_lock: Path | None = None,
) -> None:
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
        # The production install is owned by deploy/requirements.lock alone; the
        # candidate no longer carries a second copy of the same package set.
        "backend/requirements-dev.lock",
        "deploy/build-requirements.lock",
        "docs/DEPENDENCY_LICENSES.md",
        "frontend/surfaces.json",
        "frontend/robots.txt",
        "frontend/sitemap.xml",
        "frontend/zh-TW/index.html",
        "publication/third_party_source_lock.json",
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
    verify_candidate_reference_graph(root)
    _verify_public_file_inventory(root)
    _verify_security_txt(root)
    _verify_discovery_surfaces(root)
    _verify_notice_paths(root, files["THIRD_PARTY_NOTICES.md"])

    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise CandidateFailure(f"symlink is forbidden: {relative}")
        if _has_forbidden_path_part(path.relative_to(root)):
            raise CandidateFailure(f"private path is forbidden: {relative}")
    scan_candidate_content(
        root,
        forbidden_markers=FORBIDDEN_TEXT,
        secret_patterns=SECRET_PATTERNS,
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
        {
            "pyswisseph-2.10.3.2.tar.gz",
            "LICENSE.txt",
            "SWISS_EPHEMERIS_LICENSE",
            "SWEPHELP_LICENSE",
            "SWEPHELP_README.txt",
        },
    )
    candidate_source_lock = files["publication/third_party_source_lock.json"]
    authority_source_lock = third_party_source_lock or candidate_source_lock
    if third_party_source_lock is not None and (
        not third_party_source_lock.is_file()
        or _sha256(third_party_source_lock) != _sha256(candidate_source_lock)
    ):
        raise CandidateFailure(
            "candidate third-party source lock differs from release authority"
        )
    source_lock = _load_json(authority_source_lock)
    locked_pyswisseph = [
        item for item in source_lock.get("packages", [])
        if isinstance(item, dict) and item.get("name") == "pyswisseph"
    ]
    if len(locked_pyswisseph) != 1 or _sha256(
        root / "third_party" / "pyswisseph" / "pyswisseph-2.10.3.2.tar.gz"
    ) != locked_pyswisseph[0].get("sha256"):
        raise CandidateFailure(
            "pyswisseph source differs from the independent third-party source lock"
        )
    if {
        key: locked_pyswisseph[0].get(key)
        for key in ("upstream", "source_role", "license_material")
    } != {
        "upstream": "https://pypi.org/project/pyswisseph/2.10.3.2/",
        "source_role": (
            "production runtime sdist compiled into the native swisseph extension"
        ),
        "license_material": [
            "AGPL-3.0-only",
            "GPL-2.0-only",
            "Swiss-Ephemeris-dual-license",
        ],
    }:
        raise CandidateFailure(
            "pyswisseph source lock omits upstream, role, or license material"
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
    parser.add_argument("--third-party-source-lock", type=Path)
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
        verify_candidate(args.root, args.third_party_source_lock)
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
