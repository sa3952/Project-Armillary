"""Fail-closed identity verification for mounted frontend releases."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Mapping

from .frontend_assets import (
    MANIFEST_NAME,
    asset_parent_directories,
    validate_asset_name,
)


CONTRACT_PATH = Path("deploy/frontend-contract.json")
SOURCE_URL_PREFIX = "https://github.com/sa3952/Project-Armillary/tree/"
SOURCE_ARCHIVE_URL_PREFIX = "https://github.com/sa3952/Project-Armillary/archive/"
REQUIRE_ENV = "CLASSICAL_ASTROLOGY_REQUIRE_FRONTEND_RELEASE"
ROOT_ENV = "CLASSICAL_ASTROLOGY_FRONTEND_ROOT"
DIGEST_ENV = "CLASSICAL_ASTROLOGY_FRONTEND_RELEASE_DIGEST"
COMBINED_ENV = "CLASSICAL_ASTROLOGY_COMBINED_RELEASE_ID"
BACKEND_IMAGE_ENV = "CLASSICAL_ASTROLOGY_BACKEND_IMAGE_ID"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FORMAL_LEGAL_FIELDS = {
    "effective_date",
    "release_version",
    "public_revision",
    "source_archive_url",
    "source_archive_sha256",
}


def _load_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"required regular JSON file missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def exact_contract(authority_root: Path) -> dict[str, str | int]:
    contract = _load_object(authority_root / CONTRACT_PATH)
    expected_keys = {
        "schema_version",
        "api_schema_version",
        "dossier_version",
        "export_contract_version",
    }
    if set(contract) != expected_keys or contract.get("schema_version") != 1:
        raise ValueError("unsupported frontend contract policy")
    for key in expected_keys - {"schema_version"}:
        value = contract.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"0\.\d+\.\d+", value):
            raise ValueError(f"invalid exact frontend contract: {key}")
    return contract


def artifact_digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _release_tree(release: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()

    def collect(directory: Path, relative_parent: PurePosixPath | None = None) -> None:
        for entry in directory.iterdir():
            relative = (
                PurePosixPath(entry.name)
                if relative_parent is None
                else relative_parent / entry.name
            )
            metadata = entry.lstat()
            if entry.is_symlink():
                raise ValueError(f"frontend release contains symlink: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(relative.as_posix())
                collect(entry, relative)
            elif stat.S_ISREG(metadata.st_mode):
                files.add(relative.as_posix())
            else:
                raise ValueError(f"frontend release contains non-file: {relative}")
    collect(release)
    return files, directories


def verify_release(
    release_directory: Path,
    *,
    authority_root: Path,
    require_digest_directory_name: bool = True,
) -> dict[str, object]:
    release = Path(os.path.abspath(release_directory))
    metadata = release.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or release.is_symlink():
        raise ValueError("frontend release must be a non-symlink directory")
    manifest = _load_object(release / MANIFEST_NAME)
    required_keys = {
        "schema_version",
        "artifact_type",
        "frontend_public_source_revision",
        "source_url",
        # A manifest is immutable, so the URL it names cannot be corrected
        # later. Until the Corresponding Source is published this field says
        # the URL is provisional rather than letting the reader assume it
        # resolves (`PIA-2026-08-06-011`).
        "source_url_status",
        "source_publication",
        "formal_legal_release_fields",
        "required_contracts",
        "files",
        "artifact_digest",
    }
    if set(manifest) != required_keys:
        raise ValueError("frontend release manifest key set is invalid")
    if manifest.get("source_url_status") not in {
        "published_and_expected_anonymously_reachable",
        "provisional_pending_publication",
    }:
        raise ValueError("frontend source URL status is invalid")
    publication = manifest.get("source_publication")
    legal_fields = manifest.get("formal_legal_release_fields")
    if manifest.get("source_url_status") == "provisional_pending_publication":
        if publication is not None or legal_fields is not None:
            raise ValueError("provisional frontend source must not carry publication evidence")
    elif (
        not isinstance(publication, dict)
        or set(publication)
        != {
            "schema_version",
            "status",
            "effective_date",
            "release_version",
            "public_source_revision",
            "source_url",
            "source_archive_url",
            "source_archive_sha256",
            "anonymous_checkout_sha256",
            "evidence_sha256",
        }
        or publication.get("schema_version")
        != "corresponding-source-publication-receipt-v1"
        or publication.get("status") != "published_anonymously_reachable"
        or not isinstance(legal_fields, dict)
        or set(legal_fields) != _FORMAL_LEGAL_FIELDS
    ):
        raise ValueError("published frontend source evidence is invalid")
    revision = manifest.get("frontend_public_source_revision")
    digest = manifest.get("artifact_digest")
    if manifest.get("schema_version") != 3 or manifest.get(
        "artifact_type"
    ) != "classical-astrology-frontend-release":
        raise ValueError("frontend release manifest type is invalid")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ValueError("frontend source revision is invalid")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("frontend artifact digest is invalid")
    if manifest.get("source_url") != f"{SOURCE_URL_PREFIX}{revision}":
        raise ValueError("frontend source URL is not pinned to its revision")
    if publication is not None and (
        publication.get("public_source_revision") != revision
        or publication.get("source_url") != manifest.get("source_url")
        or publication.get("source_archive_url")
        != f"{SOURCE_ARCHIVE_URL_PREFIX}{revision}.tar.gz"
        or legal_fields
        != {
            "effective_date": publication.get("effective_date"),
            "release_version": publication.get("release_version"),
            "public_revision": revision,
            "source_archive_url": publication.get("source_archive_url"),
            "source_archive_sha256": publication.get("source_archive_sha256"),
        }
        or not _RELEASE_VERSION.fullmatch(str(publication.get("release_version", "")))
        or any(
            not _DIGEST.fullmatch(str(publication.get(key, "")))
            for key in (
                "source_archive_sha256",
                "anonymous_checkout_sha256",
                "evidence_sha256",
            )
        )
    ):
        raise ValueError("frontend source publication evidence identity is invalid")
    if publication is not None:
        try:
            effective_date = date.fromisoformat(str(publication.get("effective_date", "")))
        except ValueError:
            effective_date = None
        if effective_date is None or effective_date.isoformat() != publication.get(
            "effective_date"
        ):
            raise ValueError("frontend source publication effective date is invalid")
    contract = exact_contract(authority_root)
    if manifest.get("required_contracts") != {
        "api_schema_version": contract["api_schema_version"],
        "dossier_version": contract["dossier_version"],
        "export_contract_version": contract["export_contract_version"],
    }:
        raise ValueError("frontend exact contract mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("frontend release file receipt is not exact-set")
    expected_assets = {validate_asset_name(name) for name in files}
    if (
        len(expected_assets) != len(files)
        or "zh-TW/index.html" not in expected_assets
    ):
        raise ValueError("frontend release file receipt is not exact-set")
    actual_files, actual_directories = _release_tree(release)
    expected_files = expected_assets | {MANIFEST_NAME}
    expected_directories = set(asset_parent_directories(expected_assets))
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError(
            "frontend release has missing or unexpected entries: "
            f"missing={sorted(expected_files - actual_files)}, "
            f"unexpected={sorted(actual_files - expected_files)}, "
            f"unexpected_directories={sorted(actual_directories - expected_directories)}"
        )
    for name in sorted(expected_assets):
        path = release / name
        file_metadata = path.lstat()
        if not stat.S_ISREG(file_metadata.st_mode) or path.is_symlink():
            raise ValueError(f"frontend release asset is a symlink or non-file: {name}")
        receipt = files.get(name)
        if not isinstance(receipt, dict) or set(receipt) != {"sha256", "size_bytes"}:
            raise ValueError(f"frontend release receipt is malformed: {name}")
        if receipt.get("size_bytes") != file_metadata.st_size:
            raise ValueError(f"frontend release size mismatch: {name}")
        if receipt.get("sha256") != _sha256(path):
            raise ValueError(f"frontend release SHA-256 mismatch: {name}")
    payload = {key: value for key, value in manifest.items() if key != "artifact_digest"}
    if artifact_digest(payload) != digest:
        raise ValueError("frontend artifact digest mismatch")
    if require_digest_directory_name and release.name != digest:
        raise ValueError("frontend release directory identity mismatch")
    return {
        "artifact_digest": digest,
        "frontend_public_source_revision": revision,
        "source_url": manifest["source_url"],
        "source_url_status": manifest["source_url_status"],
        "source_publication": publication,
        "formal_legal_release_fields": legal_fields,
        "required_contracts": manifest["required_contracts"],
        "files": files,
    }


def combined_release_id(
    *,
    backend_image_id: str,
    backend_public_source_revision: str,
    frontend_artifact_digest: str,
    frontend_public_source_revision: str,
) -> str:
    if not _IMAGE_ID.fullmatch(backend_image_id):
        raise ValueError("backend image ID is invalid")
    if not _REVISION.fullmatch(backend_public_source_revision):
        raise ValueError("backend public source revision is invalid")
    if not _DIGEST.fullmatch(frontend_artifact_digest):
        raise ValueError("frontend artifact digest is invalid")
    if not _REVISION.fullmatch(frontend_public_source_revision):
        raise ValueError("frontend public source revision is invalid")
    return hashlib.sha256(_canonical_bytes({
        "backend_image_id": backend_image_id,
        "backend_public_source_revision": backend_public_source_revision,
        "frontend_artifact_digest": frontend_artifact_digest,
        "frontend_public_source_revision": frontend_public_source_revision,
    })).hexdigest()


def load_runtime_release(
    *,
    environment: Mapping[str, str],
    authority_root: Path,
    backend_public_source_revision: str | None,
    api_schema_version: str,
    dossier_version: str,
) -> tuple[Path | None, dict[str, object] | None, frozenset[str] | None]:
    requirement = environment.get(REQUIRE_ENV)
    if requirement in (None, ""):
        return None, None, None
    if requirement != "1":
        raise RuntimeError("frontend release requirement must be exactly '1'")
    if backend_public_source_revision is None:
        raise RuntimeError("external frontend requires a backend public revision")
    raw_root = environment.get(ROOT_ENV)
    expected_digest = environment.get(DIGEST_ENV)
    combined = environment.get(COMBINED_ENV)
    backend_image_id = environment.get(BACKEND_IMAGE_ENV)
    if not raw_root or not Path(raw_root).is_absolute():
        raise RuntimeError("external frontend root must be absolute")
    if not expected_digest or not _DIGEST.fullmatch(expected_digest):
        raise RuntimeError("external frontend digest is missing or invalid")
    if not combined or not _DIGEST.fullmatch(combined):
        raise RuntimeError("combined release ID is missing or invalid")
    if not backend_image_id or not _IMAGE_ID.fullmatch(backend_image_id):
        raise RuntimeError("backend image ID is missing or invalid")
    release_root = Path(raw_root)
    try:
        verified = verify_release(
            release_root,
            authority_root=authority_root,
            require_digest_directory_name=False,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"frontend release verification failed: {exc}") from exc
    if verified["artifact_digest"] != expected_digest:
        raise RuntimeError("mounted frontend digest differs from deployment identity")
    contracts = verified["required_contracts"]
    if not isinstance(contracts, dict) or contracts.get(
        "api_schema_version"
    ) != api_schema_version or contracts.get("dossier_version") != dossier_version:
        raise RuntimeError("mounted frontend is incompatible with backend contracts")
    expected_combined = combined_release_id(
        backend_image_id=backend_image_id,
        backend_public_source_revision=backend_public_source_revision,
        frontend_artifact_digest=expected_digest,
        frontend_public_source_revision=str(
            verified["frontend_public_source_revision"]
        ),
    )
    if expected_combined != combined:
        raise RuntimeError("combined release ID does not match mounted components")
    identity: dict[str, object] = {
        "status": "available",
        "combined_release_id": combined,
        "backend": {
            "image_id": backend_image_id,
            "public_source_revision": backend_public_source_revision,
            "source_url": f"{SOURCE_URL_PREFIX}{backend_public_source_revision}",
        },
        "frontend": {
            "artifact_digest": expected_digest,
            "public_source_revision": verified[
                "frontend_public_source_revision"
            ],
            "source_url": verified["source_url"],
        },
        "contracts": contracts,
    }
    verified_files = verified["files"]
    if not isinstance(verified_files, dict):
        raise RuntimeError("verified frontend file receipt is invalid")
    return release_root, identity, frozenset(verified_files)
