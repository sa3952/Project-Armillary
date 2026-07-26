#!/usr/bin/env python3
"""Download and verify exact source distributions for locked dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PACKAGE_LINE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")
HASH_LINE = re.compile(r"--hash=sha256:([0-9a-f]{64})")
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
USER_AGENT = "classical-astrology-corresponding-source/1"


class SourcePreparationError(RuntimeError):
    """Raised when exact third-party source cannot be proven."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_lock(path: Path, purpose: str) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        package_match = PACKAGE_LINE.match(line)
        if package_match:
            current = {
                "name": package_match.group(1),
                "version": package_match.group(2),
                "purpose": purpose,
                "allowed_sha256": [],
            }
            packages.append(current)
            continue
        hash_match = HASH_LINE.search(line)
        if hash_match and current is not None:
            current["allowed_sha256"].append(hash_match.group(1))
    if not packages or any(not item["allowed_sha256"] for item in packages):
        raise SourcePreparationError(f"cannot parse complete hashes from {path}")
    return packages


def _request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with (
        urllib.request.urlopen(request, timeout=60) as response,
        destination.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)


def _prepare_package(
    package: dict[str, Any],
    sources_dir: Path,
    source_lock: dict[tuple[str, str], str],
) -> dict[str, Any]:
    name = package["name"]
    version = package["version"]
    metadata = _request_json(PYPI_JSON.format(name=name, version=version))
    sdists = [
        item
        for item in metadata.get("urls", [])
        if item.get("packagetype") == "sdist"
    ]
    if len(sdists) != 1:
        raise SourcePreparationError(
            f"{name}=={version} has {len(sdists)} source distributions"
        )
    artifact = sdists[0]
    declared_hash = artifact.get("digests", {}).get("sha256")
    source_lock_hash = source_lock.get((name.lower(), version))
    if declared_hash in package["allowed_sha256"]:
        hash_basis = "dependency_lock"
    elif declared_hash == source_lock_hash:
        hash_basis = "committed_source_lock"
    elif source_lock_hash is None:
        hash_basis = "bootstrap_pypi_metadata_uncommitted"
    else:
        raise SourcePreparationError(
            f"{name}=={version} sdist hash differs from the source lock"
        )
    filename = artifact["filename"]
    destination = sources_dir / filename
    _download(artifact["url"], destination)
    actual_hash = _sha256(destination)
    if actual_hash != declared_hash:
        destination.unlink(missing_ok=True)
        raise SourcePreparationError(
            f"{name}=={version} source hash mismatch"
        )
    info = metadata.get("info", {})
    return {
        "name": name,
        "version": version,
        "purpose": package["purpose"],
        "filename": filename,
        "sha256": actual_hash,
        "hash_basis": hash_basis,
        "upstream": info.get("project_url")
        or info.get("package_url")
        or PYPI_JSON.format(name=name, version=version),
        "download_url": artifact["url"],
        "license_metadata": info.get("license") or None,
        "license_classifiers": [
            classifier
            for classifier in info.get("classifiers", [])
            if classifier.startswith("License ::")
        ],
    }


def prepare_sources(
    runtime_lock: Path,
    build_lock: Path,
    destination: Path,
    source_lock_path: Path | None,
) -> None:
    if destination.exists():
        raise SourcePreparationError(
            f"destination already exists: {destination}"
        )
    destination.mkdir(parents=True)
    sources_dir = destination / "sources"
    sources_dir.mkdir()
    try:
        source_lock: dict[tuple[str, str], str] = {}
        if source_lock_path is not None:
            payload = json.loads(source_lock_path.read_text(encoding="utf-8"))
            source_lock = {
                (item["name"].lower(), item["version"]): item["sha256"]
                for item in payload["packages"]
            }
        combined: dict[tuple[str, str], dict[str, Any]] = {}
        for package in (
            parse_lock(runtime_lock, "runtime")
            + parse_lock(build_lock, "build")
        ):
            key = (package["name"].lower(), package["version"])
            if key in combined:
                existing = combined[key]
                existing["purpose"] = "runtime_and_build"
                existing["allowed_sha256"] = sorted(
                    set(existing["allowed_sha256"])
                    | set(package["allowed_sha256"])
                )
            else:
                combined[key] = package

        inventory = [
            _prepare_package(package, sources_dir, source_lock)
            for _, package in sorted(combined.items())
        ]
        complete = all(
            item["hash_basis"]
            != "bootstrap_pypi_metadata_uncommitted"
            for item in inventory
        )
        payload = {
            "schema_version": 1,
            "complete": complete,
            "source": "PyPI version metadata plus locked SHA-256 verification",
            "packages": inventory,
        }
        (destination / "SOURCE_MANIFEST.json").write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        sums = "".join(
            f"{item['sha256']}  sources/{item['filename']}\n"
            for item in inventory
        )
        (destination / "SHA256SUMS").write_text(sums, encoding="ascii")
        source_lock_candidate = {
            "schema_version": 1,
            "packages": [
                {
                    "name": item["name"],
                    "version": item["version"],
                    "sha256": item["sha256"],
                }
                for item in inventory
            ],
        }
        (destination / "SOURCE_LOCK.json").write_text(
            json.dumps(
                source_lock_candidate,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--build-lock", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        prepare_sources(
            args.runtime_lock,
            args.build_lock,
            args.destination,
            args.source_lock,
        )
    except (OSError, SourcePreparationError, urllib.error.URLError) as exc:
        print(f"THIRD-PARTY SOURCE PREPARATION FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"THIRD-PARTY SOURCE PREPARATION PASSED: {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
