#!/usr/bin/env python3
"""Exact release-packet identity and archive verification."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
from scripts.deployment.authorization_markers import sha256_file
IMAGE_REPOSITORY = "classical-astrology-private-alpha"
FRONTEND_MODE_LABEL = "org.classical-astrology.frontend.mode"
EXTERNAL_FRONTEND_MODE = "external-release-v1"
_sha256 = sha256_file

def _inspect(image: str) -> dict:
    completed = subprocess.run(
        ["docker", "image", "inspect", image],
        check=True,
        text=True,
        capture_output=True,
    )
    records = json.loads(completed.stdout)
    if len(records) != 1:
        raise RuntimeError("expected exactly one inspected image")
    return records[0]


def _validated_identity(inspection: dict, expected_revision: str) -> dict[str, str]:
    config = inspection.get("Config") or {}
    labels = config.get("Labels") or {}
    revision = labels.get("org.opencontainers.image.revision")
    user = config.get("User") or ""
    if inspection.get("Os") != "linux" or inspection.get("Architecture") != "amd64":
        raise ValueError("image must be linux/amd64")
    if user in ("", "0", "root", "0:0"):
        raise ValueError("image must declare a non-root user")
    if revision != expected_revision:
        raise ValueError(
            "image VCS revision label does not match the expected public "
            f"source revision: label={revision!r} expected={expected_revision!r}. "
            "The label is the only route a recipient has to the Corresponding "
            "Source, so it must name a revision that resolves in the public "
            "repository. Build the release image inside a checkout of the "
            "published tree, not the private one"
        )
    image_id = inspection.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ValueError("image ID is missing or malformed")
    frontend_mode = labels.get(FRONTEND_MODE_LABEL)
    if frontend_mode != EXTERNAL_FRONTEND_MODE:
        raise ValueError("image must declare the external frontend mode")
    return {
        "image_id": image_id,
        "vcs_revision": revision,
        "frontend_mode": frontend_mode,
    }


def _full_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError(f"{label} must be a full lowercase Git SHA")
    return value


def _archive_identity(path: Path) -> dict[str, object]:
    return {
        "filename": path.name,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _validate_registry_digest(receipt: dict) -> None:
    digest = receipt.get("registry_digest")
    status = receipt.get("registry_digest_status")
    if digest is None:
        if status != "absent_registry_free_or_multiple_references":
            raise ValueError("registry digest status is inconsistent")
        return
    if (
        status != "informational_unverified_not_used_for_load"
        or not isinstance(digest, str)
    ):
        raise ValueError("registry digest status is inconsistent")
    match = re.fullmatch(r"([^@\s]+)@sha256:([0-9a-f]{64})", digest)
    if match is None or match.group(1) != IMAGE_REPOSITORY:
        raise ValueError("registry digest repository or syntax is invalid")


def _validate_tar_members(members: list[tarfile.TarInfo], label: str) -> None:
    seen: set[str] = set()
    for member in members:
        relative = Path(member.name)
        if member.name in seen:
            raise ValueError(f"{label} contains a duplicate member name")
        seen.add(member.name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or ".git" in relative.parts
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError(f"{label} contains an unsafe member")


def _tar_json(
    archive: tarfile.TarFile,
    name: str,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[object, bytes]:
    try:
        member = archive.getmember(name)
    except KeyError as error:
        raise ValueError(f"{label} is missing") from error
    if not member.isfile() or member.size > maximum_bytes:
        raise ValueError(f"{label} is not bounded")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ValueError(f"{label} is unreadable")
    payload = extracted.read()
    return json.loads(payload), payload


def build_release_packet_receipt(
    *,
    private_tooling_revision: str,
    public_source_revision: str,
    source_archive: Path,
    transfer_archive: Path,
    inspection: dict,
) -> dict[str, object]:
    private_revision = _full_revision(
        private_tooling_revision, "private tooling revision"
    )
    public_revision = _full_revision(
        public_source_revision, "public source revision"
    )
    identity = _validated_identity(inspection, public_revision)
    digests = inspection.get("RepoDigests") or []
    registry_digest = digests[0] if len(digests) == 1 else None
    config = inspection.get("Config") or {}
    receipt = {
        "schema_version": 2,
        "private_tooling_revision": private_revision,
        "public_source_revision": public_revision,
        "source_archive": _archive_identity(source_archive),
        "transfer_archive": _archive_identity(transfer_archive),
        "image_id": identity["image_id"],
        "registry_digest": registry_digest,
        "registry_digest_status": (
            "informational_unverified_not_used_for_load"
            if registry_digest is not None
            else "absent_registry_free_or_multiple_references"
        ),
        "os": inspection.get("Os"),
        "architecture": inspection.get("Architecture"),
        "user": config.get("User"),
        "frontend_mode": identity["frontend_mode"],
    }
    return receipt


def verify_source_archive_revision(
    source_archive: Path,
    expected_public_revision: str,
) -> dict[str, object]:
    revision = _full_revision(
        expected_public_revision, "expected public source revision"
    )
    with tarfile.open(source_archive, "r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise ValueError("source archive is empty")
        _validate_tar_members(members, "source archive")
        archive_revision = archive.pax_headers.get("comment")
    if archive_revision != revision:
        raise ValueError("source archive revision differs from receipt")
    return {
        "member_count": len(members),
        "public_source_revision": revision,
    }


def validate_release_packet(
    receipt: dict,
    *,
    source_archive: Path,
    transfer_archive: Path,
    expected_private_revision: str,
    expected_public_revision: str,
    expected_source_filename: str | None = None,
    expected_transfer_filename: str | None = None,
) -> dict[str, object]:
    if receipt.get("schema_version") != 2 or "vcs_revision" in receipt:
        raise ValueError("release packet mixes legacy and current revision fields")
    private_revision = _full_revision(
        expected_private_revision, "expected private tooling revision"
    )
    public_revision = _full_revision(
        expected_public_revision, "expected public source revision"
    )
    if receipt.get("private_tooling_revision") != private_revision:
        raise ValueError("private tooling revision mismatch")
    if receipt.get("public_source_revision") != public_revision:
        raise ValueError("public source revision mismatch")
    for label, path, expected_filename in (
        (
            "source_archive",
            source_archive,
            expected_source_filename or source_archive.name,
        ),
        (
            "transfer_archive",
            transfer_archive,
            expected_transfer_filename or transfer_archive.name,
        ),
    ):
        declared = receipt.get(label)
        if not isinstance(declared, dict):
            raise ValueError(f"release packet omits {label}")
        actual = _archive_identity(path)
        actual["filename"] = expected_filename
        if (
            declared != actual
            or Path(str(declared.get("filename"))).name != expected_filename
        ):
            raise ValueError(f"{label} identity mismatch")
    if receipt.get("os") != "linux" or receipt.get("architecture") != "amd64":
        raise ValueError("release packet image must be linux/amd64")
    if receipt.get("user") in (None, "", "0", "root", "0:0"):
        raise ValueError("release packet image user must be non-root")
    if receipt.get("frontend_mode") != EXTERNAL_FRONTEND_MODE:
        raise ValueError("release packet must use the external frontend")
    image_id = receipt.get("image_id")
    if not isinstance(image_id, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image_id
    ):
        raise ValueError("release packet image ID is missing or invalid")
    _validate_registry_digest(receipt)
    source_inventory = verify_source_archive_revision(
        source_archive, public_revision
    )
    image_inventory = verify_archive_inventory(transfer_archive, {
        "image_id": image_id,
        "os": receipt["os"],
        "architecture": receipt["architecture"],
        "user": receipt["user"],
        "vcs_revision": public_revision,
        "frontend_mode": receipt.get("frontend_mode"),
    })
    return {
        "private_tooling_revision": private_revision,
        "public_source_revision": public_revision,
        "image_id": image_id,
        "source_archive": source_inventory,
        "transfer_archive": image_inventory,
    }


def verify_archive_inventory(
    archive: Path,
    receipt: dict,
) -> dict[str, object]:
    with tarfile.open(archive, "r:") as bundle:
        _validate_tar_members(bundle.getmembers(), "image archive")
        manifest, _ = _tar_json(
            bundle, "manifest.json",
            maximum_bytes=1024 * 1024,
            label="image archive manifest",
        )
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise ValueError("image archive must contain exactly one image")
        record = manifest[0]
        config_filename = record.get("Config")
        if not isinstance(config_filename, str) or not config_filename:
            raise ValueError("image archive manifest has no config reference")
        image_id = str(receipt["image_id"]).removeprefix("sha256:")
        oci_blob = re.fullmatch(
            r"blobs/sha256/([0-9a-f]{64})", config_filename
        )
        if oci_blob:
            # OCI layout, produced by a Docker with the containerd image store.
            # Here the receipt image ID is the index digest rather than the
            # config blob digest, so bind the receipt to index.json and prove
            # the config blob against its own content digest below.
            index, _ = _tar_json(
                bundle, "index.json",
                maximum_bytes=1024 * 1024,
                label="OCI image archive index",
            )
            if not isinstance(index, dict):
                raise ValueError("OCI image archive index is invalid")
            manifests = index.get("manifests")
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise ValueError(
                    "OCI image archive must reference exactly one image"
                )
            if manifests[0].get("digest") != receipt["image_id"]:
                raise ValueError(
                    "archive index does not match receipt image ID"
                )
        elif config_filename != f"{image_id}.json":
            raise ValueError("archive config does not match receipt image ID")
        config, config_bytes = _tar_json(
            bundle, config_filename,
            maximum_bytes=4 * 1024 * 1024,
            label="image archive config",
        )
        if (
            oci_blob
            and hashlib.sha256(config_bytes).hexdigest() != oci_blob.group(1)
        ):
            raise ValueError("OCI config blob does not match its content digest")
        if not isinstance(config, dict):
            raise ValueError("image archive config is invalid")
    if config.get("os") != receipt["os"]:
        raise ValueError("archive operating system differs from receipt")
    if config.get("architecture") != receipt["architecture"]:
        raise ValueError("archive architecture differs from receipt")
    image_config = config.get("config") or {}
    if image_config.get("User") != receipt["user"]:
        raise ValueError("archive user differs from receipt")
    labels = image_config.get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != receipt["vcs_revision"]:
        raise ValueError("archive revision differs from receipt")
    if labels.get(FRONTEND_MODE_LABEL) != receipt.get("frontend_mode"):
        raise ValueError("archive frontend mode differs from receipt")
    return {
        "image_count": 1,
        "config_filename": config_filename,
    }
def deploy_identity(receipt: dict, expected_revision: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise ValueError("expected revision must be a full lowercase Git SHA")
    image_id = receipt.get("image_id")
    if receipt.get("schema_version") != 2 or "vcs_revision" in receipt:
        raise ValueError("release receipt is not deployable")
    if (
        not isinstance(image_id, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        or receipt.get("os") != "linux"
        or receipt.get("architecture") != "amd64"
        or receipt.get("user") in (None, "", "0", "root", "0:0")
        or receipt.get("public_source_revision") != expected_revision
        or receipt.get("frontend_mode") != EXTERNAL_FRONTEND_MODE
    ):
        raise ValueError("release receipt is not deployable")
    return {
        "image_id": image_id,
        "vcs_revision": expected_revision,
        "frontend_mode": EXTERNAL_FRONTEND_MODE,
    }
