#!/usr/bin/env python3
"""Download and verify exact source distributions for locked dependencies."""

from __future__ import annotations

import argparse
import email.policy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import urllib.parse
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Callable


PACKAGE_LINE = re.compile(
    r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\s\\]+)"
)
HASH_LINE = re.compile(r"--hash=sha256:([0-9a-f]{64})")
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
USER_AGENT = "classical-astrology-corresponding-source/1"
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_SOURCE_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024


class SourcePreparationError(RuntimeError):
    """Raised when exact third-party source cannot be proven."""


def _wheel_identity(filename: str) -> tuple[str, str, Callable[[str], str]]:
    """Load the build-only wheel parser only in wheel-acquisition paths.

    The public candidate verifier imports this module on a clean host before
    it enters the governed Docker builder.  License-table and lock verification
    are stdlib-only and must not require a pre-existing host virtualenv.
    """
    try:
        from packaging.utils import canonicalize_name, parse_wheel_filename
    except ModuleNotFoundError as error:
        raise SourcePreparationError(
            "wheel acquisition requires the hash-locked build environment"
        ) from error
    wheel_name, wheel_version, _, _ = parse_wheel_filename(filename)
    return canonicalize_name(wheel_name), str(wheel_version), canonicalize_name


ROLE_LABELS = {
    "production": "production",
    "build": "build",
    "development": "development",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_lock(path: Path, role: str) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        package_match = PACKAGE_LINE.match(line)
        if package_match:
            current = {
                "name": package_match.group(1),
                "version": package_match.group(2),
                "roles": [role],
                "allowed_sha256": [],
            }
            packages.append(current)
            continue
        hash_match = HASH_LINE.search(line)
        if hash_match and current is not None:
            current["allowed_sha256"].append(hash_match.group(1))
            continue
        if stripped == "\\":
            continue
        raise SourcePreparationError(
            f"unsupported dependency-lock directive in {path}: {stripped}"
        )
    if not packages or any(not item["allowed_sha256"] for item in packages):
        raise SourcePreparationError(f"cannot parse complete hashes from {path}")
    return packages


def _request_json(url: str) -> dict[str, Any]:
    _require_https_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _https_only_opener().open(  # nosemgrep: dynamic-urllib-use-detected
        request,
        timeout=30,
    ) as response:
        _require_https_url(response.geturl())
        body = _read_bounded(response, MAX_METADATA_BYTES, "PyPI metadata")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise SourcePreparationError("PyPI metadata is not valid JSON") from error
        if not isinstance(payload, dict):
            raise SourcePreparationError("PyPI metadata root must be an object")
        return payload


def _download(url: str, destination: Path) -> None:
    _require_https_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with (
        _https_only_opener().open(  # nosemgrep: dynamic-urllib-use-detected
            request,
            timeout=60,
        ) as response,
        destination.open("wb") as output,
    ):
        _require_https_url(response.geturl())
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > MAX_SOURCE_ARCHIVE_BYTES:
            raise SourcePreparationError("source archive exceeds bounded byte limit")
        written = 0
        while True:
            chunk = response.read(min(1024 * 1024, MAX_SOURCE_ARCHIVE_BYTES - written + 1))
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_SOURCE_ARCHIVE_BYTES:
                raise SourcePreparationError("source archive exceeds bounded byte limit")
            output.write(chunk)


def _read_bounded(handle, limit: int, label: str) -> bytes:
    headers = getattr(handle, "headers", {})
    content_length = headers.get("Content-Length") if headers is not None else None
    if content_length is not None and int(content_length) > limit:
        raise SourcePreparationError(f"{label} exceeds bounded byte limit")
    body = handle.read(limit + 1)
    if len(body) > limit:
        raise SourcePreparationError(f"{label} exceeds bounded byte limit")
    return body


def _require_https_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise SourcePreparationError(
            "third-party source URL must use credential-free HTTPS"
        )


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirect targets before urllib can contact them."""

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        _require_https_url(newurl)
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )


def _https_only_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_HttpsOnlyRedirectHandler())


def _inspect_sdist_license(archive: Path) -> dict[str, Any]:
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = [
                member for member in source.getmembers() if member.isfile()
            ]
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise SourcePreparationError("source archive has too many members")
            if any(member.size > MAX_ARCHIVE_MEMBER_BYTES for member in members):
                raise SourcePreparationError("source archive member exceeds bounded limit")
            pkg_info = next(
                (
                    member
                    for member in members
                    if member.name.endswith("/PKG-INFO")
                ),
                None,
            )
            if pkg_info is None:
                raise SourcePreparationError(
                    f"source archive has no PKG-INFO: {archive.name}"
                )
            handle = source.extractfile(pkg_info)
            if handle is None:
                raise SourcePreparationError(
                    f"cannot read PKG-INFO: {archive.name}"
                )
            metadata = BytesParser(
                policy=email.policy.default
            ).parsebytes(_read_bounded(handle, MAX_ARCHIVE_MEMBER_BYTES, "PKG-INFO"))
            root = pkg_info.name.rsplit("/", 1)[0]
            declared = metadata.get_all("License-File") or []
            candidates = []
            for raw in declared:
                normalized = Path(str(raw)).as_posix()
                candidates.append(f"{root}/{normalized}")
            if not candidates:
                candidates = [
                    member.name
                    for member in members
                    if len(Path(member.name).parts) == 2
                    and Path(member.name).name.lower().startswith(
                        ("license", "copying", "notice")
                    )
                ]
            license_files = sorted(set(candidates))
            license_hashes: dict[str, str] = {}
            member_map = {member.name: member for member in members}
            for relative in license_files:
                member = member_map.get(relative)
                if member is None:
                    raise SourcePreparationError(
                        f"declared license file missing in {archive.name}: "
                        f"{relative}"
                    )
                license_handle = source.extractfile(member)
                if license_handle is None:
                    raise SourcePreparationError(
                        f"cannot read license file in {archive.name}: "
                        f"{relative}"
                    )
                license_hashes[relative] = hashlib.sha256(
                    _read_bounded(
                        license_handle,
                        MAX_ARCHIVE_MEMBER_BYTES,
                        "license file",
                    )
                ).hexdigest()
    except (OSError, tarfile.TarError) as exc:
        raise SourcePreparationError(
            f"cannot inspect source license evidence: {archive.name}: {exc}"
        ) from exc
    return {
        "license_expression": metadata.get("License-Expression") or None,
        "license_metadata": metadata.get("License") or None,
        "license_files": license_files,
        "license_file_sha256": license_hashes,
    }


def _license_evidence_complete(package: dict[str, Any]) -> bool:
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


def _license_identity(package: dict[str, Any]) -> str:
    expression = package.get("license_expression")
    if isinstance(expression, str) and expression.strip():
        return expression.strip()
    classifiers = package.get("license_classifiers")
    if isinstance(classifiers, list) and classifiers:
        normalized = []
        classifier_identities = {
            "License :: OSI Approved :: MIT License": "MIT",
            "License :: OSI Approved :: Apache Software License": "Apache-2.0",
            "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
            "License :: OSI Approved :: ISC License (ISCL)": "ISC",
            "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
            "License :: OSI Approved :: GNU Affero General Public License v3": "AGPL-3.0-only",
            "License :: OSI Approved :: BSD License": (
                "BSD License (Trove classifier; variant unspecified)"
            ),
        }
        for item in classifiers:
            rendered = str(item).strip()
            normalized.append(classifier_identities.get(rendered, rendered))
        return "; ".join(normalized)
    metadata = package.get("license_metadata")
    if isinstance(metadata, str) and metadata.strip():
        rendered = " ".join(metadata.split())
        if len(rendered) > 200:
            raise SourcePreparationError(
                "dependency license metadata is too long for the human table: "
                f"{package.get('name')}=={package.get('version')}"
            )
        return rendered
    raise SourcePreparationError(
        f"dependency has no human-readable license identity: "
        f"{package.get('name')}=={package.get('version')}"
    )


def render_dependency_license_table(manifest: dict[str, Any]) -> str:
    """Render the human view strictly from the machine source manifest."""
    packages = manifest.get("packages")
    if manifest.get("complete") is not True or not isinstance(packages, list):
        raise SourcePreparationError(
            "license table requires a complete source manifest"
        )
    lines = [
        "# Dependency licenses",
        "",
        "This file is generated from `third_party/SOURCE_MANIFEST.json`; "
        "manual edits are not accepted.",
        "",
        "| Package | Version | Roles | License | Source archive | Manifest entry |",
        "|---|---:|---|---|---|---|",
    ]
    for index, package in enumerate(packages):
        if not _license_evidence_complete(package):
            raise SourcePreparationError(
                "dependency license evidence is incomplete: "
                f"{package.get('name')}=={package.get('version')}"
            )
        roles = package.get("roles")
        if (
            not isinstance(roles, list)
            or not roles
            or any(role not in ROLE_LABELS for role in roles)
        ):
            raise SourcePreparationError(
                f"dependency roles are invalid: {package.get('name')}"
            )
        values = (
            str(package.get("name", "")),
            str(package.get("version", "")),
            ", ".join(ROLE_LABELS[role] for role in roles),
            _license_identity(package),
            f"`third_party/sources/{package.get('filename', '')}`",
            f"`third_party/SOURCE_MANIFEST.json#/packages/{index}`",
        )
        escaped = [value.replace("|", "\\|") for value in values]
        lines.append("| " + " | ".join(escaped) + " |")
    lines.extend(
        [
            "",
            "Each row requires archive-level license files and hashes in the "
            "machine manifest. The publication verifier regenerates this table "
            "and fails on drift or missing evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def verify_manifest_lock_inputs(
    manifest: dict[str, Any],
    lock_paths: dict[str, Path],
) -> None:
    """Fail closed when locks and the bundled source inventory diverge."""
    declared_inputs = manifest.get("lock_inputs")
    if manifest.get("schema_version") != 2 or not isinstance(
        declared_inputs, dict
    ):
        raise SourcePreparationError("source manifest has no governed lock inputs")
    expected: dict[tuple[str, str], set[str]] = {}
    for role, path in sorted(lock_paths.items()):
        declared = declared_inputs.get(role)
        if not isinstance(declared, dict):
            raise SourcePreparationError(f"source manifest omits {role} lock")
        if declared.get("filename") != path.name or declared.get(
            "sha256"
        ) != _sha256(path):
            raise SourcePreparationError(f"{role} dependency lock has drifted")
        if declared.get("selection") != "caller_supplied" or declared.get("role") != role:
            raise SourcePreparationError(f"{role} dependency lock identity is ambiguous")
        if not isinstance(declared.get("source_path"), str) or not declared["source_path"]:
            raise SourcePreparationError(f"{role} dependency lock path identity is missing")
        for package in parse_lock(path, role):
            key = (package["name"].lower(), package["version"])
            expected.setdefault(key, set()).add(role)
    actual: dict[tuple[str, str], set[str]] = {}
    for package in manifest.get("packages", []):
        key = (str(package.get("name", "")).lower(), str(package.get("version", "")))
        actual[key] = set(package.get("roles", []))
    if actual != expected:
        raise SourcePreparationError(
            "source manifest package versions or roles differ from dependency locks"
        )


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
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise SourcePreparationError("source filename is not a confined basename")
    destination = sources_dir / filename
    try:
        destination.resolve().relative_to(sources_dir.resolve())
    except ValueError as error:
        raise SourcePreparationError("source filename escapes destination") from error
    _download(artifact["url"], destination)
    actual_hash = _sha256(destination)
    if actual_hash != declared_hash:
        destination.unlink(missing_ok=True)
        raise SourcePreparationError(
            f"{name}=={version} source hash mismatch"
        )
    info = metadata.get("info", {})
    sdist_license = _inspect_sdist_license(destination)
    return {
        "name": name,
        "version": version,
        "roles": package["roles"],
        "filename": filename,
        "sha256": actual_hash,
        "hash_basis": hash_basis,
        "upstream": info.get("project_url")
        or info.get("package_url")
        or PYPI_JSON.format(name=name, version=version),
        "download_url": artifact["url"],
        "license_expression": sdist_license["license_expression"],
        "license_metadata": (
            info.get("license")
            or sdist_license["license_metadata"]
            or None
        ),
        "license_classifiers": [
            classifier
            for classifier in info.get("classifiers", [])
            if classifier.startswith("License ::")
        ],
        "license_files": sdist_license["license_files"],
        "license_file_sha256": sdist_license["license_file_sha256"],
    }


def _prepare_build_wheel_index(
    build_lock: Path,
    destination: Path,
    *,
    source_inventory: list[dict[str, Any]],
    platforms: list[str],
    python_version: str,
    abi: str,
) -> dict[str, Any]:
    """Download one hash-locked Linux wheel per build requirement."""
    wheel_dir = destination / "build_wheels"
    wheel_dir.mkdir()
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--require-hashes",
        "--only-binary=:all:",
    ]
    for platform_name in platforms:
        command.extend(["--platform", platform_name])
    command.extend([
        "--implementation",
        "cp",
        "--python-version",
        python_version,
        "--abi",
        abi,
        "--dest",
        str(wheel_dir),
        "--requirement",
        str(build_lock),
    ])
    environment = os.environ.copy()
    environment["PIP_NO_CACHE_DIR"] = "1"
    subprocess.run(command, check=True, env=environment)
    allowed_hashes = {
        digest
        for package in parse_lock(build_lock, "build")
        for digest in package["allowed_sha256"]
    }
    wheels = []
    for path in sorted(wheel_dir.glob("*.whl")):
        digest = _sha256(path)
        if digest not in allowed_hashes:
            raise SourcePreparationError(
                f"build wheel is not authorized by lock: {path.name}"
            )
        wheel_name, wheel_version, normalize_name = _wheel_identity(path.name)
        source_match = next(
            (
                item for item in source_inventory
                if normalize_name(item["name"]) == wheel_name
                and item["version"] == wheel_version
            ),
            None,
        )
        if source_match is None:
            raise SourcePreparationError(
                f"build wheel has no retained matching sdist: {path.name}"
            )
        wheels.append(
            {
                "filename": path.name,
                "sha256": digest,
                "source_sdist_filename": source_match["filename"],
                "source_sdist_sha256": source_match["sha256"],
            }
        )
    expected_count = len(parse_lock(build_lock, "build"))
    if len(wheels) != expected_count:
        raise SourcePreparationError(
            "build wheel index does not contain exactly one artifact per lock entry"
        )
    return {
        "complete": True,
        "target": {
            "implementation": "cp",
            "python_version": python_version,
            "abi": abi,
            "platforms": platforms,
        },
        "artifacts": wheels,
    }


def _prepare_dependency_wheel_index(
    development_lock: Path,
    destination: Path,
    *,
    source_inventory: list[dict[str, Any]],
    platforms: list[str],
    python_version: str,
    abi: str,
) -> dict[str, Any]:
    """Create a verified offline index for non-source-built dependencies."""
    source_build_packages = {"pyswisseph"}
    packages = [
        package
        for package in parse_lock(development_lock, "development")
        if package["name"].lower() not in source_build_packages
    ]
    wheel_dir = destination / "dependency_wheels"
    wheel_dir.mkdir()
    filtered_lock = destination / ".dependency-wheels.lock"
    filtered_lock.write_text(
        "".join(
            f"{package['name']}=={package['version']} \\\n"
            + "".join(
                f"    --hash=sha256:{digest} \\\n"
                for digest in package["allowed_sha256"][:-1]
            )
            + f"    --hash=sha256:{package['allowed_sha256'][-1]}\n"
            for package in packages
        ),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-deps",
        "--require-hashes",
        "--only-binary=:all:",
    ]
    for platform_name in platforms:
        command.extend(["--platform", platform_name])
    command.extend([
        "--implementation",
        "cp",
        "--python-version",
        python_version,
        "--abi",
        abi,
        "--dest",
        str(wheel_dir),
        "--requirement",
        str(filtered_lock),
    ])
    environment = os.environ.copy()
    environment["PIP_NO_CACHE_DIR"] = "1"
    try:
        subprocess.run(command, check=True, env=environment)
    finally:
        filtered_lock.unlink(missing_ok=True)
    allowed_hashes = {
        digest for package in packages for digest in package["allowed_sha256"]
    }
    wheels = []
    for path in sorted(wheel_dir.glob("*.whl")):
        digest = _sha256(path)
        if digest not in allowed_hashes:
            raise SourcePreparationError(
                f"dependency wheel is not authorized by lock: {path.name}"
            )
        wheel_name, wheel_version, normalize_name = _wheel_identity(path.name)
        source_match = next(
            (
                item for item in source_inventory
                if normalize_name(item["name"]) == wheel_name
                and item["version"] == wheel_version
            ),
            None,
        )
        if source_match is None:
            raise SourcePreparationError(
                f"dependency wheel has no retained matching sdist: {path.name}"
            )
        wheels.append(
            {
                "filename": path.name,
                "sha256": digest,
                "source_sdist_filename": source_match["filename"],
                "source_sdist_sha256": source_match["sha256"],
            }
        )
    if len(wheels) != len(packages):
        raise SourcePreparationError(
            "dependency wheel index does not contain exactly one artifact "
            "per non-source-built development lock entry"
        )
    return {
        "complete": True,
        "source_build_packages": sorted(source_build_packages),
        "target": {
            "implementation": "cp",
            "python_version": python_version,
            "abi": abi,
            "platforms": platforms,
        },
        "artifacts": wheels,
    }


def prepare_sources(
    runtime_lock: Path,
    build_lock: Path,
    destination: Path,
    source_lock_path: Path | None,
    development_lock: Path | None = None,
    build_wheel_platforms: list[str] | None = None,
    build_python_version: str = "3.13",
    build_abi: str = "cp313",
) -> dict[str, Any]:
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
        lock_inputs = {
            "production": runtime_lock,
            "build": build_lock,
        }
        if development_lock is not None:
            lock_inputs["development"] = development_lock
        parsed_packages = [
            package
            for role, lock_path in lock_inputs.items()
            for package in parse_lock(lock_path, role)
        ]
        for package in parsed_packages:
            key = (package["name"].lower(), package["version"])
            if key in combined:
                existing = combined[key]
                existing["roles"] = sorted(
                    set(existing["roles"]) | set(package["roles"])
                )
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
        source_hashes_complete = all(
            item["hash_basis"]
            != "bootstrap_pypi_metadata_uncommitted"
            for item in inventory
        )
        license_evidence_complete = all(
            _license_evidence_complete(item) for item in inventory
        )
        complete = source_hashes_complete and license_evidence_complete
        build_wheel_index = None
        dependency_wheel_index = None
        if build_wheel_platforms:
            build_wheel_index = _prepare_build_wheel_index(
                build_lock,
                destination,
                source_inventory=inventory,
                platforms=build_wheel_platforms,
                python_version=build_python_version,
                abi=build_abi,
            )
            if development_lock is not None:
                dependency_wheel_index = _prepare_dependency_wheel_index(
                    development_lock,
                    destination,
                    source_inventory=inventory,
                    platforms=build_wheel_platforms,
                    python_version=build_python_version,
                    abi=build_abi,
                )
        def lock_identity(role: str, lock_path: Path) -> dict[str, str]:
            try:
                source_path = lock_path.resolve().relative_to(
                    Path(__file__).resolve().parents[2]
                ).as_posix()
            except ValueError:
                source_path = f"external_input/{lock_path.name}"
            return {
                "filename": lock_path.name,
                "sha256": _sha256(lock_path),
                "selection": "caller_supplied",
                "source_path": source_path,
                "role": role,
            }

        payload = {
            "schema_version": 2,
            "complete": complete,
            "source_hashes_complete": source_hashes_complete,
            "license_evidence_complete": license_evidence_complete,
            "source": "PyPI version metadata plus locked SHA-256 verification",
            "lock_inputs": {
                role: lock_identity(role, lock_path)
                for role, lock_path in sorted(lock_inputs.items())
            },
            "reconstruction_complete": bool(
                complete
                and build_wheel_index
                and build_wheel_index.get("complete") is True
                and dependency_wheel_index
                and dependency_wheel_index.get("complete") is True
            ),
            "build_wheel_index": build_wheel_index,
            "dependency_wheel_index": dependency_wheel_index,
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
        if build_wheel_index:
            sums += "".join(
                f"{item['sha256']}  build_wheels/{item['filename']}\n"
                for item in build_wheel_index["artifacts"]
            )
        if dependency_wheel_index:
            sums += "".join(
                f"{item['sha256']}  dependency_wheels/{item['filename']}\n"
                for item in dependency_wheel_index["artifacts"]
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
        return payload
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--build-lock", type=Path, required=True)
    parser.add_argument("--development-lock", type=Path)
    parser.add_argument(
        "--build-wheel-platform",
        action="append",
        dest="build_wheel_platforms",
    )
    parser.add_argument("--build-python-version", default="3.13")
    parser.add_argument("--build-abi", default="cp313")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path)
    return parser.parse_args()


def _preparation_exit_status(payload: dict[str, Any]) -> int:
    return 0 if payload.get("complete") is True else 2


def main() -> int:
    args = _parse_args()
    try:
        payload = prepare_sources(
            args.runtime_lock,
            args.build_lock,
            args.destination,
            args.source_lock,
            args.development_lock,
            args.build_wheel_platforms,
            args.build_python_version,
            args.build_abi,
        )
    except (OSError, SourcePreparationError, urllib.error.URLError) as exc:
        print(f"THIRD-PARTY SOURCE PREPARATION FAILED: {exc}", file=sys.stderr)
        return 1
    status = _preparation_exit_status(payload)
    if status:
        print(
            "THIRD-PARTY SOURCE BOOTSTRAP INCOMPLETE: "
            f"{args.destination}; review SOURCE_LOCK.json, adopt it through "
            "change control, then rerun into a new destination with "
            "--source-lock",
            file=sys.stderr,
        )
        return status
    print(f"THIRD-PARTY SOURCE PREPARATION PASSED: {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
