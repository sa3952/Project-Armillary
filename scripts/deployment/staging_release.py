#!/usr/bin/env python3
"""Registry-free staging image export, import, deploy, and rollback."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import stat
import sys
import tarfile
import tempfile

from scripts.deployment.authorization_markers import claim, sha256_file
import time
from typing import Callable

try:
    from app.frontend_release import (
        combined_release_id,
        verify_release as verify_frontend_release,
    )
except ModuleNotFoundError:
    # The backend package is not installed when this governed deployment CLI is
    # invoked from a source checkout, so expose its canonical app package root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    from app.frontend_release import (
        combined_release_id,
        verify_release as verify_frontend_release,
    )


ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "deploy" / "compose.yaml"
STAGING_COMPOSE = ROOT / "deploy" / "staging" / "compose.staging.yaml"
FRONTEND_COMPOSE = (
    ROOT / "deploy" / "compose.frontend-release.yaml"
)
HOST_MARKER = Path("/var/lib/private-alpha/.release-apply-authorized")
HOST_LOCK = Path("/var/lib/private-alpha/deployment.lock")
DEFAULT_STATE = Path("/var/lib/private-alpha/deployment-state.json")
DEFAULT_FRONTEND_RELEASE_ROOT = Path(
    "/var/lib/private-alpha/frontend-releases"
)
IMAGE_REPOSITORY = "classical-astrology-private-alpha"
FRONTEND_MODE_LABEL = "org.classical-astrology.frontend.mode"
EXTERNAL_FRONTEND_MODE = "external-release-v1"
PUBLISHED_FRONTEND_SOURCE_STATUS = (
    "published_and_expected_anonymously_reachable"
)
# The application container stays on an `internal: true` network, which Docker
# cannot publish ports from, so probes address it directly at the address
# pinned in deploy/staging/compose.staging.yaml rather than on host loopback.
# Changing either side requires changing the other in the same commit.
APP_PROBE_HOST = "172.31.240.2"
APP_PROBE_PORT = 8000
PRIVACY_RECEIPT_MAX_AGE_SECONDS = 60 * 60


def _require_published_frontend(frontend: dict) -> None:
    if frontend.get("source_url_status") != PUBLISHED_FRONTEND_SOURCE_STATUS:
        raise ValueError(
            "frontend release must be published and anonymously reachable"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ValueError(f"release path contains a symlink: {candidate}")
    return absolute


@contextmanager
def _host_lock():
    lock = safe_path(HOST_LOCK)
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        lock,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o077
        ):
            raise RuntimeError("deployment lock must be root-owned mode 0600")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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
    identity = {
        "image_id": image_id,
        "vcs_revision": revision,
    }
    frontend_mode = labels.get(FRONTEND_MODE_LABEL)
    if frontend_mode is not None:
        if frontend_mode != EXTERNAL_FRONTEND_MODE:
            raise ValueError("image declares an unsupported frontend mode")
        identity["frontend_mode"] = frontend_mode
    return identity


def build_receipt(
    *,
    archive: Path,
    archive_sha256: str,
    archive_size: int,
    inspection: dict,
) -> dict[str, object]:
    config = inspection.get("Config") or {}
    labels = config.get("Labels") or {}
    digests = inspection.get("RepoDigests") or []
    receipt = {
        "schema_version": 1,
        "archive_filename": archive.name,
        "archive_sha256": archive_sha256,
        "archive_size_bytes": archive_size,
        "image_id": inspection.get("Id"),
        "image_digest": digests[0] if len(digests) == 1 else None,
        "os": inspection.get("Os"),
        "architecture": inspection.get("Architecture"),
        "user": config.get("User"),
        "vcs_revision": labels.get("org.opencontainers.image.revision"),
    }
    frontend_mode = labels.get(FRONTEND_MODE_LABEL)
    if frontend_mode is not None:
        receipt["frontend_mode"] = frontend_mode
    return receipt


def validate_receipt(
    receipt: dict, archive: Path, expected_revision: str
) -> dict:
    if type(receipt.get("schema_version")) is not int or receipt.get(
        "schema_version"
    ) != 1:
        raise ValueError("unsupported receipt schema")
    filename = receipt.get("archive_filename")
    if filename != archive.name or Path(str(filename)).name != filename:
        raise ValueError("receipt archive filename is unsafe or mismatched")
    if receipt.get("archive_sha256") != _sha256(archive):
        raise ValueError("archive SHA-256 mismatch")
    if receipt.get("archive_size_bytes") != archive.stat().st_size:
        raise ValueError("archive size mismatch")
    if receipt.get("os") != "linux" or receipt.get("architecture") != "amd64":
        raise ValueError("receipt image must be linux/amd64")
    if receipt.get("user") in (None, "", "0", "root", "0:0"):
        raise ValueError("receipt image user must be non-root")
    if receipt.get("vcs_revision") != expected_revision:
        raise ValueError("receipt VCS revision mismatch")
    if receipt.get("frontend_mode") not in (None, EXTERNAL_FRONTEND_MODE):
        raise ValueError("receipt frontend mode is unsupported")
    image_id = receipt.get("image_id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ValueError("receipt image ID is missing")
    return receipt


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


def _require_schema_version(receipt: dict, expected: int) -> None:
    value = receipt.get("schema_version")
    if type(value) is not int or value != expected:
        raise ValueError("unsupported release packet schema")


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
    }
    frontend_mode = (config.get("Labels") or {}).get(FRONTEND_MODE_LABEL)
    if frontend_mode is not None:
        receipt["frontend_mode"] = frontend_mode
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
    _require_schema_version(receipt, 2)
    if "vcs_revision" in receipt:
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
    if receipt.get("frontend_mode") not in (None, EXTERNAL_FRONTEND_MODE):
        raise ValueError("release packet frontend mode is unsupported")
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
        try:
            manifest_member = bundle.getmember("manifest.json")
        except KeyError as exc:
            raise ValueError("image archive has no manifest") from exc
        if not manifest_member.isfile() or manifest_member.size > 1024 * 1024:
            raise ValueError("image archive manifest is not bounded")
        manifest_file = bundle.extractfile(manifest_member)
        if manifest_file is None:
            raise ValueError("image archive manifest is unreadable")
        manifest = json.load(manifest_file)
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
            try:
                index_member = bundle.getmember("index.json")
            except KeyError as exc:
                raise ValueError("OCI image archive has no index") from exc
            if not index_member.isfile() or index_member.size > 1024 * 1024:
                raise ValueError("OCI image archive index is not bounded")
            index_file = bundle.extractfile(index_member)
            if index_file is None:
                raise ValueError("OCI image archive index is unreadable")
            index = json.load(index_file)
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
        try:
            config_member = bundle.getmember(config_filename)
        except KeyError as exc:
            raise ValueError("image archive config is missing") from exc
        if not config_member.isfile() or config_member.size > 4 * 1024 * 1024:
            raise ValueError("image archive config is not bounded")
        config_file = bundle.extractfile(config_member)
        if config_file is None:
            raise ValueError("image archive config is unreadable")
        config_bytes = config_file.read()
        if (
            oci_blob
            and hashlib.sha256(config_bytes).hexdigest() != oci_blob.group(1)
        ):
            raise ValueError("OCI config blob does not match its content digest")
        config = json.loads(config_bytes)
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


def rollback_readiness(
    previous: dict | None,
    *,
    image_present: Callable[[str], bool],
    release_present: Callable[[str], bool],
) -> dict:
    """State whether the previous pair could actually be rolled back to.

    `deploy/README.md` says to keep the previous backend/frontend pair until
    rollback evidence is complete, and nothing checked it.  A rule with no
    check degrades quietly: the image gets pruned, the release directory gets
    cleaned up, and the first time anyone needs the rollback it is not there.

    This does not perform a rollback — that needs a real host — it records
    whether the artifacts the rollback would need are still resolvable, so the
    absence is visible before it matters rather than during an incident.
    """

    if not previous:
        return {
            "status": "no_previous_deployment",
            "image_present": None,
            "frontend_release_present": None,
        }
    image = str(previous.get("image_id") or "")
    release = str(previous.get("frontend_release_dir") or "")
    image_ok = image_present(image) if image else False
    release_ok = release_present(release) if release else None
    ready = image_ok and release_ok is not False
    return {
        "status": "rollback_artifacts_present" if ready else "rollback_artifacts_missing",
        "image_present": image_ok,
        "frontend_release_present": release_ok,
    }


def next_state(old: dict, candidate: dict, readiness: dict | None = None) -> dict:
    return {
        "schema_version": (
            2 if candidate.get("schema_version") == 2 else 1
        ),
        "current": candidate,
        "previous": old.get("current"),
        "rollback_readiness": readiness
        or {
            "status": "not_evaluated",
            "image_present": None,
            "frontend_release_present": None,
        },
    }


def deploy_identity(receipt: dict, expected_revision: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise ValueError("expected revision must be a full lowercase Git SHA")
    image_id = receipt.get("image_id")
    schema_version = receipt.get("schema_version")
    if type(schema_version) is not int or schema_version not in (1, 2):
        raise ValueError("release receipt is not deployable")
    if schema_version == 2 and "vcs_revision" in receipt:
        raise ValueError("release receipt mixes revision schemas")
    revision_field = (
        receipt.get("public_source_revision")
        if schema_version == 2
        else receipt.get("vcs_revision")
    )
    if (
        not isinstance(image_id, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        or receipt.get("os") != "linux"
        or receipt.get("architecture") != "amd64"
        or receipt.get("user") in (None, "", "0", "root", "0:0")
        or revision_field != expected_revision
    ):
        raise ValueError("release receipt is not deployable")
    identity = {
        "image_id": image_id,
        "vcs_revision": expected_revision,
    }
    frontend_mode = receipt.get("frontend_mode")
    if frontend_mode is not None:
        if frontend_mode != EXTERNAL_FRONTEND_MODE:
            raise ValueError("release receipt frontend mode is unsupported")
        identity["frontend_mode"] = frontend_mode
    return identity


def validate_supply_chain_receipt(
    receipt: dict,
    candidate: dict,
) -> dict[str, str]:
    """Reject an exact release candidate with an adverse scanner verdict."""
    schema = receipt.get("schema_version")
    if schema not in {
        "private-alpha-supply-chain-summary-v1",
        "private-alpha-supply-chain-summary-v2",
    }:
        raise ValueError("unsupported supply-chain receipt schema")
    observed = receipt.get("candidate")
    if not isinstance(observed, dict):
        raise ValueError("supply-chain receipt candidate is missing")
    backend = candidate.get("backend") or candidate
    expected = {
        "image_id": backend.get("image_id"),
        "revision": backend.get("vcs_revision"),
        "os": "linux",
        "architecture": "amd64",
    }
    for key, value in expected.items():
        if observed.get(key) != value:
            raise ValueError(
                f"supply-chain receipt {key} differs from release candidate"
            )
    if schema == "private-alpha-supply-chain-summary-v1":
        if receipt.get("decision") != "no_high_or_critical_matches":
            raise ValueError("supply-chain receipt requires adverse manual triage")
    else:
        if (
            receipt.get("decision")
            != "high_critical_matches_triaged_with_open_finding"
            or receipt.get("open_finding") != "DEP-SEC-E-001"
        ):
            raise ValueError(
                "triaged supply-chain receipt requires the exact open finding"
            )
        counts = receipt.get("severity_counts")
        if (
            not isinstance(counts, dict)
            or set(counts) != {"grype", "trivy"}
            or any(
                not isinstance(item, dict)
                or set(item) != {"critical", "high"}
                or any(type(value) is not int or value < 0 for value in item.values())
                for item in counts.values()
            )
            or not any(value > 0 for item in counts.values() for value in item.values())
        ):
            raise ValueError("triaged supply-chain severity counts are invalid")
    positive = receipt.get("scanner_positive_control")
    if (
        not isinstance(positive, dict)
        or type(positive.get("matches")) is not int
        or positive["matches"] < 1
    ):
        raise ValueError("supply-chain scanner positive control did not pass")
    artifacts = receipt.get("artifact_sha256")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("supply-chain raw artifact identities are missing")
    if not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        for value in artifacts.values()
    ):
        raise ValueError("supply-chain raw artifact identity is invalid")
    return {
        "image_id": str(expected["image_id"]),
        "revision": str(expected["revision"]),
    }


def validate_privacy_receipt(
    receipt: dict,
    candidate: dict,
    *,
    now: int | None = None,
) -> dict[str, str]:
    """Bind the real HTTPS sensitive-request/sink canary to this candidate."""
    if (
        receipt.get("schema_version") != 2
        or receipt.get("verification_scope")
        != "network_check_external_privacy_v1"
        or receipt.get("result") != "pass"
    ):
        raise ValueError("privacy receipt is not a passing v2 receipt")
    created = receipt.get("created_at_epoch")
    current = int(time.time()) if now is None else now
    if (
        type(created) is not int
        or created > current + 60
        or current - created > PRIVACY_RECEIPT_MAX_AGE_SECONDS
    ):
        raise ValueError("privacy receipt is stale or has an invalid timestamp")
    backend = candidate.get("backend") or candidate
    observed = receipt.get("candidate")
    if not isinstance(observed, dict) or observed != {
        "revision": backend.get("vcs_revision"),
        "image_id": backend.get("image_id"),
    }:
        raise ValueError("privacy receipt candidate identity mismatch")
    if (
        receipt.get("total_match_count") != 0
        or not (
            receipt.get("all_sinks_observed") is True
            or receipt.get("quiet_sink_authority_verified") is True
        )
        or receipt.get("scanner_self_test_passed") is not True
    ):
        raise ValueError("privacy receipt sink evidence is incomplete")
    checks = receipt.get("request_case_checks")
    required = {"success", "422", "malformed", "413", "415", "429"}
    if not isinstance(checks, dict) or set(checks) != required or not all(
        value is True for value in checks.values()
    ):
        raise ValueError("privacy receipt request controls are incomplete")
    if receipt.get("deferred_request_cases") != {
        "503": "requires_concurrency_drill",
        "slow_body": "requires_raw_tls_drill",
        "timeout": "requires_bounded_worker_drill",
        "unexpected_error": "disposable_runtime_only",
        "worker_restart": "requires_bounded_worker_drill",
    }:
        raise ValueError("privacy receipt deferred controls are misclassified")
    return {
        "image_id": str(backend["image_id"]),
        "revision": str(backend["vcs_revision"]),
    }


def bind_frontend_release(
    backend_identity: dict[str, str],
    release_directory: Path,
) -> dict[str, object]:
    if backend_identity.get("frontend_mode") != EXTERNAL_FRONTEND_MODE:
        raise ValueError("backend image does not require an external frontend")
    release = safe_path(release_directory)
    frontend = verify_frontend_release(release, authority_root=ROOT)
    _require_published_frontend(frontend)
    artifact_digest = str(frontend["artifact_digest"])
    frontend_revision = str(frontend["frontend_public_source_revision"])
    backend_image_id = backend_identity["image_id"]
    backend_revision = backend_identity["vcs_revision"]
    combined = combined_release_id(
        backend_image_id=backend_image_id,
        backend_public_source_revision=backend_revision,
        frontend_artifact_digest=artifact_digest,
        frontend_public_source_revision=frontend_revision,
    )
    return {
        "schema_version": 2,
        "combined_release_id": combined,
        "backend": dict(backend_identity),
        "frontend": {
            "release_directory": str(release),
            "artifact_digest": artifact_digest,
            "public_source_revision": frontend_revision,
            "source_url": frontend["source_url"],
            "source_url_status": frontend["source_url_status"],
            "contracts": frontend["required_contracts"],
        },
    }


def verify_bound_frontend_identity(identity: dict) -> None:
    if identity.get("schema_version") != 2:
        return
    frontend = identity.get("frontend")
    backend = identity.get("backend")
    combined = identity.get("combined_release_id")
    if (
        not isinstance(frontend, dict)
        or not isinstance(backend, dict)
        or not isinstance(combined, str)
    ):
        raise ValueError("combined release identity is incomplete")
    verified = verify_frontend_release(
        safe_path(Path(str(frontend.get("release_directory")))),
        authority_root=ROOT,
    )
    _require_published_frontend(verified)
    if (
        frontend.get("artifact_digest") != verified["artifact_digest"]
        or frontend.get("public_source_revision")
        != verified["frontend_public_source_revision"]
        or frontend.get("source_url") != verified["source_url"]
        or frontend.get("source_url_status")
        != verified["source_url_status"]
        or frontend.get("contracts") != verified["required_contracts"]
    ):
        raise ValueError("persisted frontend identity differs from release")
    expected_combined = combined_release_id(
        backend_image_id=str(backend.get("image_id")),
        backend_public_source_revision=str(backend.get("vcs_revision")),
        frontend_artifact_digest=str(frontend.get("artifact_digest")),
        frontend_public_source_revision=str(
            frontend.get("public_source_revision")
        ),
    )
    if combined != expected_combined:
        raise ValueError("persisted combined release ID is invalid")


def install_frontend_release(
    incoming_release: Path,
    release_root: Path,
    *,
    expected_artifact_digest: str,
) -> dict[str, object]:
    incoming = safe_path(incoming_release)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_artifact_digest):
        raise ValueError("approved frontend digest must be 64 lowercase hex")
    digest = expected_artifact_digest
    incoming_verified = verify_frontend_release(
        incoming,
        authority_root=ROOT,
    )
    _require_published_frontend(incoming_verified)
    if incoming_verified.get("artifact_digest") != digest:
        raise ValueError("incoming release differs from approved frontend digest")
    root = safe_path(release_root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / digest
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            f"frontend release already installed: {destination}"
        )
    temporary = Path(tempfile.mkdtemp(prefix=".frontend-install-", dir=root))
    published = False
    try:
        for source in sorted(incoming.rglob("*")):
            relative = source.relative_to(incoming)
            target = temporary / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(source, flags)
            try:
                before = os.fstat(descriptor)
                with os.fdopen(descriptor, "rb", closefd=False) as reader, target.open(
                    "xb"
                ) as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                after = os.fstat(descriptor)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
                ):
                    raise RuntimeError("frontend input changed during descriptor copy")
            finally:
                os.close(descriptor)
            os.chmod(target, 0o444)
        for directory in sorted(
            (path for path in temporary.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            os.chmod(directory, 0o555)
        os.chmod(temporary, 0o555)
        verified = verify_frontend_release(
            temporary,
            authority_root=ROOT,
            require_digest_directory_name=False,
        )
        _require_published_frontend(verified)
        if verified.get("artifact_digest") != expected_artifact_digest:
            raise ValueError("frontend bundle differs from approved frontend digest")
        os.replace(temporary, destination)
        published = True
        directory_descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return {
            **verified,
            "release_directory": str(destination),
        }
    finally:
        if not published and temporary.exists():
            for directory in sorted(
                (path for path in temporary.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
            ):
                directory.chmod(0o755)
            temporary.chmod(0o755)
            shutil.rmtree(temporary)


def _assert_host_frontend_release_permissions(candidate: dict) -> None:
    frontend = candidate.get("frontend")
    if not isinstance(frontend, dict):
        return
    release = safe_path(Path(str(frontend["release_directory"])))
    for path in (release, *release.rglob("*")):
        metadata = path.lstat()
        if metadata.st_uid != 0:
            raise RuntimeError(
                f"frontend release path must be root-owned: {path.name}"
            )
        if metadata.st_mode & 0o222:
            raise RuntimeError(
                f"frontend release path must be immutable: {path.name}"
            )


def deploy_transaction(
    old: dict,
    candidate: dict,
    *,
    activate: Callable[[dict], None],
    deactivate: Callable[[dict], None],
    healthy: Callable[[dict], bool],
    privacy_probe: Callable[[dict], bool],
    readiness: Callable[[dict | None], dict] | None = None,
) -> dict:
    if old.get("current") is not None and readiness is None:
        raise RuntimeError("rollback readiness verifier is required")
    evaluated = readiness(old.get("current")) if readiness else None
    if (
        old.get("current") is not None
        and evaluated is not None
        and evaluated.get("status") != "rollback_artifacts_present"
    ):
        raise RuntimeError("required rollback artifacts are not ready")
    activate(candidate)
    if healthy(candidate) and privacy_probe(candidate):
        return next_state(old, candidate, evaluated)
    previous = old.get("current")
    if previous:
        activate(previous)
        if not healthy(previous) or not privacy_probe(previous):
            raise RuntimeError("candidate failed and previous did not recover")
        raise RuntimeError("candidate failed; previous was restored")
    deactivate(candidate)
    raise RuntimeError("candidate failed; candidate was deactivated")


def network_capability_transaction(
    candidate: dict,
    *,
    activate: Callable[[dict], None],
    healthy: Callable[[dict], bool],
    privacy_probe: Callable[[dict], bool],
    deactivate: Callable[[dict], None],
) -> bool:
    try:
        activate(candidate)
        return healthy(candidate) and privacy_probe(candidate)
    finally:
        deactivate(candidate)


def rollback_transaction(
    state: dict,
    *,
    activate: Callable[[dict], None],
    healthy: Callable[[dict], bool],
    privacy_probe: Callable[[dict], bool],
) -> dict:
    previous = state.get("previous")
    current = state.get("current")
    if not previous or not current:
        raise ValueError("both current and previous are required for rollback")
    activate(previous)
    if not healthy(previous) or not privacy_probe(previous):
        activate(current)
        if not healthy(current) or not privacy_probe(current):
            raise RuntimeError("rollback failed and current did not recover")
        raise RuntimeError("rollback failed; current was restored")
    return {
        "schema_version": (
            2
            if previous.get("schema_version") == 2
            or current.get("schema_version") == 2
            else 1
        ),
        "current": previous,
        "previous": current,
    }


def _load_json(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"required regular file missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _path_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    if (current.st_dev, current.st_ino) != identity:
        return False
    path.unlink()
    return True


def _reserve_output(path: Path) -> tuple[int, int]:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _require_path_identity(path: Path, identity: tuple[int, int]) -> None:
    if _path_identity(path) != identity:
        raise RuntimeError(f"release packet output object changed: {path.name}")


def _expected_image_id(receipt: dict, expected_image_id: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id):
        raise ValueError("expected image ID must be sha256 plus 64 lowercase hex")
    if receipt.get("image_id") != expected_image_id:
        raise ValueError("release packet differs from approved image ID")
    return expected_image_id


def _ensure_apply_host(purpose: str, bindings: dict[str, str]) -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("--apply requires Linux amd64")
    if os.geteuid() != 0:
        raise RuntimeError("--apply requires root")
    claim(
        HOST_MARKER,
        expected_purpose=purpose,
        expected_bindings={
            **bindings,
            "script_sha256": sha256_file(Path(__file__)),
        },
    )


def _clean_git_revision(
    root: Path,
    expected_revision: str,
    label: str,
) -> str:
    expected = _full_revision(expected_revision, label)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if status:
        raise RuntimeError(f"{label} checkout must be clean")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if actual != expected:
        raise RuntimeError(f"{label} checkout HEAD mismatch")
    return actual


def _packet_export(args: argparse.Namespace) -> None:
    private_revision = _clean_git_revision(
        ROOT, args.private_tooling_revision, "private tooling revision"
    )
    public_root = safe_path(args.public_source_root)
    public_revision = _clean_git_revision(
        public_root, args.public_source_revision, "public source revision"
    )
    inspection = _inspect(args.image)
    _expected_image_id({"image_id": inspection["Id"]}, args.expected_image_id)
    _validated_identity(inspection, public_revision)
    source_archive = safe_path(args.source_archive)
    transfer_archive = safe_path(args.transfer_archive)
    receipt_path = safe_path(args.receipt)
    outputs = (source_archive, transfer_archive, receipt_path)
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise RuntimeError("refusing to overwrite release packet artifact")
    if any(ROOT == path or ROOT in path.parents for path in outputs):
        raise RuntimeError("release packet artifacts must be outside the repository")
    if not args.apply:
        print(json.dumps({
            "mode": "plan",
            "private_tooling_revision": private_revision,
            "public_source_revision": public_revision,
            "image_id": inspection["Id"],
            "source_archive_filename": source_archive.name,
            "transfer_archive_filename": transfer_archive.name,
        }, indent=2))
        return
    source_archive.parent.mkdir(parents=True, exist_ok=True)
    transfer_archive.parent.mkdir(parents=True, exist_ok=True)
    source_partial = source_archive.with_name(f".{source_archive.name}.partial")
    transfer_partial = transfer_archive.with_name(
        f".{transfer_archive.name}.partial"
    )
    if source_partial.exists() or transfer_partial.exists():
        raise RuntimeError("stale release packet partial artifact exists")
    published: list[tuple[Path, tuple[int, int]]] = []
    partials: list[tuple[Path, tuple[int, int]]] = []
    try:
        partials.append((source_partial, _reserve_output(source_partial)))
        partials.append((transfer_partial, _reserve_output(transfer_partial)))
        subprocess.run(
            [
                "git",
                "archive",
                "--format=tar.gz",
                f"--output={source_partial}",
                public_revision,
            ],
            cwd=public_root,
            check=True,
        )
        _require_path_identity(source_partial, partials[0][1])
        verify_source_archive_revision(source_partial, public_revision)
        subprocess.run(
            ["docker", "save", "--output", str(transfer_partial), args.image],
            check=True,
        )
        _require_path_identity(transfer_partial, partials[1][1])
        receipt = build_release_packet_receipt(
            private_tooling_revision=private_revision,
            public_source_revision=public_revision,
            source_archive=source_partial,
            transfer_archive=transfer_partial,
            inspection=inspection,
        )
        source_identity = receipt.get("source_archive")
        transfer_identity = receipt.get("transfer_archive")
        if not isinstance(source_identity, dict) or not isinstance(
            transfer_identity, dict
        ):
            raise RuntimeError("release packet archive identities are malformed")
        source_identity["filename"] = source_archive.name
        transfer_identity["filename"] = transfer_archive.name
        validate_release_packet(
            receipt,
            source_archive=source_partial,
            transfer_archive=transfer_partial,
            expected_private_revision=private_revision,
            expected_public_revision=public_revision,
            expected_source_filename=source_archive.name,
            expected_transfer_filename=transfer_archive.name,
        )
        os.replace(source_partial, source_archive)
        published.append((source_archive, _path_identity(source_archive)))
        os.replace(transfer_partial, transfer_archive)
        published.append((transfer_archive, _path_identity(transfer_archive)))
        _atomic_json(receipt_path, receipt)
    except Exception:
        for path, identity in reversed(published):
            _unlink_if_identity(path, identity)
        raise
    finally:
        for path, identity in reversed(partials):
            _unlink_if_identity(path, identity)
    print(json.dumps({"mode": "exported_release_packet", **receipt}, indent=2))


def _packet_identity(args: argparse.Namespace) -> tuple[dict, dict]:
    source_archive = safe_path(args.source_archive)
    transfer_archive = safe_path(args.transfer_archive)
    receipt = _load_json(safe_path(args.receipt))
    _expected_image_id(receipt, args.expected_image_id)
    identity = validate_release_packet(
        receipt,
        source_archive=source_archive,
        transfer_archive=transfer_archive,
        expected_private_revision=args.private_tooling_revision,
        expected_public_revision=args.public_source_revision,
    )
    return receipt, identity


def _packet_verify(args: argparse.Namespace) -> None:
    _receipt, identity = _packet_identity(args)
    print(json.dumps({"mode": "verified_release_packet", **identity}, indent=2))


def _packet_load(args: argparse.Namespace) -> None:
    receipt, identity = _packet_identity(args)
    if not args.apply:
        print(json.dumps({"mode": "plan_local_load", **identity}, indent=2))
        return
    with _host_lock():
        _ensure_apply_host("release-packet-load", {
            "image_id": str(receipt["image_id"]),
            "transfer_archive_sha256": _sha256(
                safe_path(args.transfer_archive)
            ),
        })
        subprocess.run(
            ["docker", "load", "--input", str(safe_path(args.transfer_archive))],
            check=True,
        )
        inspection = _inspect(str(receipt["image_id"]))
        loaded = _validated_identity(
            inspection, str(receipt["public_source_revision"])
        )
        if loaded["image_id"] != receipt["image_id"]:
            raise RuntimeError("loaded image ID does not match release packet")
        tag = (
            f"{IMAGE_REPOSITORY}:release-"
            f"{str(receipt['public_source_revision'])[:12]}"
        )
        subprocess.run(
            ["docker", "image", "tag", loaded["image_id"], tag],
            check=True,
        )
    print(json.dumps({
        "mode": "loaded_release_packet",
        "tag": tag,
        **identity,
    }, indent=2))


def _export(args: argparse.Namespace) -> None:
    if subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout:
        raise RuntimeError("image export requires a clean source checkout")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if revision != args.expected_revision:
        raise RuntimeError("source HEAD does not match expected revision")
    inspection = _inspect(args.image)
    _validated_identity(inspection, revision)
    _expected_image_id({"image_id": inspection["Id"]}, args.expected_image_id)
    archive = safe_path(args.archive)
    receipt_path = safe_path(args.receipt)
    if archive.exists() or archive.is_symlink() or receipt_path.exists() or receipt_path.is_symlink():
        raise RuntimeError("refusing to overwrite export artifact")
    if ROOT == archive or ROOT in archive.parents:
        raise RuntimeError("image archive must be outside the repository")
    if not args.apply:
        print(json.dumps({
            "mode": "plan",
            "image_id": inspection["Id"],
            "vcs_revision": revision,
            "archive_filename": archive.name,
        }, indent=2))
        return
    archive.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["docker", "save", "--output", str(archive), args.image],
        check=True,
    )
    receipt = build_receipt(
        archive=archive,
        archive_sha256=_sha256(archive),
        archive_size=archive.stat().st_size,
        inspection=inspection,
    )
    _atomic_json(receipt_path, receipt)


def _import(args: argparse.Namespace) -> None:
    archive = safe_path(args.archive)
    receipt_path = safe_path(args.receipt)
    receipt = validate_receipt(
        _load_json(receipt_path),
        archive,
        args.expected_revision,
    )
    _expected_image_id(receipt, args.expected_image_id)
    inventory = verify_archive_inventory(archive, receipt)
    if not args.apply:
        print(json.dumps({
            "mode": "verified_archive",
            "image_id": receipt["image_id"],
            "vcs_revision": receipt["vcs_revision"],
            **inventory,
        }, indent=2))
        return
    with _host_lock():
        _ensure_apply_host("release-import", {
            "archive_sha256": _sha256(archive),
            "image_id": str(receipt["image_id"]),
        })
        subprocess.run(["docker", "load", "--input", str(archive)], check=True)
        inspection = _inspect(str(receipt["image_id"]))
        identity = _validated_identity(inspection, args.expected_revision)
        if identity["image_id"] != receipt["image_id"]:
            raise RuntimeError("loaded image ID does not match receipt")
        tag = f"{IMAGE_REPOSITORY}:release-{args.expected_revision[:12]}"
        subprocess.run(
            ["docker", "image", "tag", identity["image_id"], tag],
            check=True,
        )
    print(json.dumps({"mode": "imported", "tag": tag, **identity}, indent=2))


def _state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "current": None, "previous": None}
    return _load_json(path)


def _activate(identity: dict) -> None:
    verify_bound_frontend_identity(identity)
    backend = identity.get("backend") or identity
    revision = backend["vcs_revision"]
    tag = f"release-{revision[:12]}"
    expected_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", f"{IMAGE_REPOSITORY}:{tag}"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if expected_id != backend["image_id"]:
        raise RuntimeError("release tag resolves to the wrong image ID")
    environment = {**os.environ, "IMAGE_TAG": tag, "VCS_REF": revision}
    compose_files = [
        "--file", str(BASE_COMPOSE),
        "--file", str(STAGING_COMPOSE),
    ]
    if identity.get("schema_version") == 2:
        frontend = identity.get("frontend")
        combined = identity.get("combined_release_id")
        if not isinstance(frontend, dict) or not isinstance(combined, str):
            raise RuntimeError("combined release identity is incomplete")
        environment.update({
            "FRONTEND_RELEASE_DIR": str(frontend["release_directory"]),
            "FRONTEND_RELEASE_DIGEST": str(frontend["artifact_digest"]),
            "BACKEND_IMAGE_ID": str(backend["image_id"]),
            "COMBINED_RELEASE_ID": combined,
        })
        compose_files.extend(["--file", str(FRONTEND_COMPOSE)])
    subprocess.run(
        [
            "docker", "compose",
            *compose_files,
            "up", "--detach", "--no-build", "--force-recreate",
            "private-alpha-app",
        ],
        check=True,
        env=environment,
    )


def compose_ps_health(stdout: str) -> tuple[bool, str]:
    """Judge one compose `ps --format json` output, and say why.

    `FPI-2026-08-06-E-015`. This used to be
    ``'"Health":"healthy"' in stdout.replace(" ", "")`` — an OR over the whole
    output that never looked at `State`, never counted records, and never
    checked which image was running. Two of its misreadings are dangerous
    rather than merely noisy: two records where a stale container is healthy
    and the new one is not, and a container `restarting` with a leftover
    `Health: healthy`. Both are reported healthy. Two more are the safe
    direction but still wrong: a tab between key and value defeats the
    space-stripping, and an image with no HEALTHCHECK reports an empty
    `Health` — each looks unhealthy and can trigger an unnecessary rollback.

    It has not misfired yet only because it is imprecise enough to match both
    the pre-2.21 array form and the current NDJSON form. That is not a safety
    property.

    Parsing is per line, and a parse failure is a distinct outcome from an
    unhealthy service: collapsing them is what makes "the deploy check is
    broken" indistinguishable from "the deploy is broken".
    """

    records: list[dict] = []
    text = stdout.strip()
    if not text:
        return False, "no_compose_records"
    # Three shapes are in the wild: a JSON array (compose <= 2.20), one JSON
    # object per line (2.21+), and a single pretty-printed object. Try the
    # whole document first, which covers the array and the pretty object, then
    # fall back to per-line parsing. Guessing from the first character is what
    # made the pretty form unparsable.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    return False, "unparsable_compose_output"
                records.append(item)
        except json.JSONDecodeError:
            return False, "unparsable_compose_output"
    else:
        if isinstance(parsed, dict):
            records = [parsed]
        elif isinstance(parsed, list):
            if any(not isinstance(item, dict) for item in parsed):
                return False, "unparsable_compose_output"
            records = list(parsed)
        else:
            return False, "unparsable_compose_output"

    if len(records) != 1:
        return False, (
            "no_compose_records" if not records else "multiple_compose_records"
        )
    record = records[0]
    if record.get("State") != "running":
        return False, "container_not_running"
    health = record.get("Health")
    if health == "":
        return False, "image_declares_no_healthcheck"
    if health != "healthy":
        return False, "container_not_healthy"
    return True, "healthy"


def _healthy(_identity: dict) -> bool:
    reason = "never_polled"
    for _ in range(60):
        result = subprocess.run(
            [
                "docker", "compose",
                "--file", str(BASE_COMPOSE),
                "--file", str(STAGING_COMPOSE),
                "ps", "--format", "json", "private-alpha-app",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            reason = "compose_ps_failed"
        else:
            healthy, reason = compose_ps_health(result.stdout)
            if healthy:
                container = subprocess.run(
                    [
                        "docker", "compose",
                        "--file", str(BASE_COMPOSE),
                        "--file", str(STAGING_COMPOSE),
                        "ps", "--quiet", "private-alpha-app",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                container_id = container.stdout.strip()
                if container.returncode != 0 or not container_id:
                    reason = "container_identity_unavailable"
                    continue
                actual_image = subprocess.run(
                    [
                        "docker", "inspect", "--format", "{{.Image}}",
                        container_id,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                backend = _identity.get("backend") or _identity
                if (
                    actual_image.returncode == 0
                    and actual_image.stdout.strip() == backend.get("image_id")
                ):
                    return True
                reason = "running_image_identity_mismatch"
        time.sleep(2)
    # Say which of the failure modes ended the wait. Without this the caller
    # cannot tell a broken service from a broken health check.
    print(f"health wait ended: {reason}", file=sys.stderr)
    return False


def _deactivate(_identity: dict) -> None:
    subprocess.run(
        [
            "docker", "compose",
            "--file", str(BASE_COMPOSE),
            "--file", str(STAGING_COMPOSE),
            "down", "--remove-orphans",
        ],
        check=True,
    )


def _privacy_probe(identity: dict) -> bool:
    def local_get(path: str) -> tuple[int, bytes, dict[str, str]] | None:
        connection = HTTPConnection(
            APP_PROBE_HOST, APP_PROBE_PORT, timeout=5
        )
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read(4097)
            if len(body) > 4096:
                return None
            return response.status, body, dict(response.headers)
        except (HTTPException, OSError, TimeoutError):
            return None
        finally:
            connection.close()

    try:
        health = local_get("/api/health")
        if health is None or health[0] != 200:
            return False
        expected_health = {"status": "ok", "ready": True}
        if identity.get("schema_version") == 2:
            expected_health["readiness_scope"] = "process_liveness_only"
        if json.loads(health[1]) != expected_health:
            return False
        headers = {key.casefold(): value for key, value in health[2].items()}
        if "noindex" not in headers.get("x-robots-tag", "").casefold():
            return False
        for path in ("/api/runtime-health", "/openapi.json"):
            hidden = local_get(path)
            if hidden is None or hidden[0] != 404:
                return False
        if identity.get("schema_version") == 2:
            configuration = local_get("/api/client-config")
            if configuration is None or configuration[0] != 200:
                return False
            release_identity = json.loads(configuration[1]).get(
                "release_identity"
            )
            frontend = identity["frontend"]
            backend = identity["backend"]
            if (
                not isinstance(release_identity, dict)
                or release_identity.get("combined_release_id")
                != identity["combined_release_id"]
                or release_identity.get("backend", {}).get("image_id")
                != backend["image_id"]
                or release_identity.get("backend", {}).get(
                    "public_source_revision"
                )
                != backend["vcs_revision"]
                or release_identity.get("frontend", {}).get(
                    "artifact_digest"
                )
                != frontend["artifact_digest"]
                or release_identity.get("frontend", {}).get(
                    "public_source_revision"
                )
                != frontend["public_source_revision"]
                or release_identity.get("contracts")
                != frontend["contracts"]
            ):
                return False
        return True
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def _external_privacy_probe(
    args: argparse.Namespace,
    identity: dict,
) -> bool:
    """Run the maintained real-host/sink canary while candidate is active."""
    if not _privacy_probe(identity):
        return False
    privacy_base_url = getattr(args, "privacy_base_url", None)
    privacy_credential_file = getattr(
        args, "privacy_credential_file", None
    )
    approved_hostname_file = getattr(
        args, "approved_hostname_file", None
    )
    privacy_receipt = getattr(args, "privacy_receipt", None)
    sinks = getattr(args, "privacy_sink_file", None) or []
    if (
        not isinstance(privacy_base_url, str)
        or not privacy_base_url
        or not isinstance(privacy_credential_file, Path)
        or not isinstance(approved_hostname_file, Path)
        or not isinstance(privacy_receipt, Path)
        or not sinks
    ):
        raise ValueError(
            "network-check --apply requires privacy URL, credential, "
            "approved-hostname, receipt, and at least one sink"
        )
    output = safe_path(privacy_receipt)
    command = [
        sys.executable,
        "-m", "scripts.deployment.run_staging_privacy_canary",
        "--base-url", privacy_base_url,
        "--credential-file", str(privacy_credential_file),
        "--approved-hostname-file", str(approved_hostname_file),
        "--output", str(output),
        "--expected-revision", str(
            (identity.get("backend") or identity)["vcs_revision"]
        ),
        "--expected-image-id", str(
            (identity.get("backend") or identity)["image_id"]
        ),
        "--apply",
    ]
    for sink in sinks:
        command.extend(["--sink-file", str(sink)])
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        return False
    validate_privacy_receipt(_load_json(output), identity)
    return True


def _rollback_readiness(previous: dict | None) -> dict:
    def image_present(image_id: str) -> bool:
        return subprocess.run(
            ["docker", "image", "inspect", image_id],
            check=False, capture_output=True,
        ).returncode == 0

    def release_present(path: str) -> bool:
        try:
            candidate = safe_path(Path(path))
            verified = verify_frontend_release(candidate, authority_root=ROOT)
        except (OSError, RuntimeError, ValueError):
            return False
        return candidate.name == verified.get("artifact_digest")

    return rollback_readiness(
        previous, image_present=image_present, release_present=release_present
    )


def _deploy(args: argparse.Namespace) -> None:
    receipt = _load_json(safe_path(args.receipt))
    backend_candidate = deploy_identity(receipt, args.expected_revision)
    candidate: dict[str, object] = dict(backend_candidate)
    if candidate.get("frontend_mode") == EXTERNAL_FRONTEND_MODE:
        frontend_release_dir = getattr(args, "frontend_release_dir", None)
        if frontend_release_dir is None:
            raise ValueError(
                "external frontend image requires --frontend-release-dir"
            )
        candidate = bind_frontend_release(
            backend_candidate,
            frontend_release_dir,
        )
    supply_chain_path = safe_path(args.supply_chain_receipt)
    validate_supply_chain_receipt(
        _load_json(supply_chain_path), candidate
    )
    privacy_receipt_path = safe_path(args.privacy_receipt)
    validate_privacy_receipt(
        _load_json(privacy_receipt_path), candidate
    )
    if not args.apply:
        print(json.dumps({"mode": "plan", "candidate": candidate}, indent=2))
        return
    state_path = safe_path(args.state)
    with _host_lock():
        _ensure_apply_host("release-deploy", {
            "image_id": str(backend_candidate["image_id"]),
            "receipt_sha256": _sha256(safe_path(args.receipt)),
            "supply_chain_receipt_sha256": _sha256(supply_chain_path),
            "privacy_receipt_sha256": _sha256(privacy_receipt_path),
        })
        _assert_host_frontend_release_permissions(candidate)
        old = _state(state_path)
        if old.get("deployment_transaction"):
            raise RuntimeError("an earlier deployment transaction is unresolved")
        _atomic_json(state_path, {
            **old,
            "deployment_transaction": "pending_activation",
            "pending_candidate": candidate,
        })
        try:
            updated = deploy_transaction(
                old,
                candidate,
                activate=_activate,
                deactivate=_deactivate,
                healthy=_healthy,
                privacy_probe=_privacy_probe,
                readiness=_rollback_readiness,
            )
        except BaseException:
            _atomic_json(state_path, old)
            raise
        _atomic_json(state_path, updated)
    print(json.dumps({"mode": "deployed", **updated}, indent=2))


def _network_check(args: argparse.Namespace) -> None:
    receipt = _load_json(safe_path(args.receipt))
    backend_candidate = deploy_identity(receipt, args.expected_revision)
    candidate: dict[str, object] = dict(backend_candidate)
    if candidate.get("frontend_mode") == EXTERNAL_FRONTEND_MODE:
        frontend_release_dir = getattr(args, "frontend_release_dir", None)
        if frontend_release_dir is None:
            raise ValueError(
                "external frontend image requires --frontend-release-dir"
            )
        candidate = bind_frontend_release(
            backend_candidate,
            frontend_release_dir,
        )
    if not args.apply:
        print(json.dumps({
            "mode": "plan",
            "candidate": candidate,
            "target_runtime_required": "linux-amd64-docker-ce",
        }, indent=2))
        return
    with _host_lock():
        _ensure_apply_host("release-network-check", {
            "image_id": str(backend_candidate["image_id"]),
            "receipt_sha256": _sha256(safe_path(args.receipt)),
        })
        _assert_host_frontend_release_permissions(candidate)
        if _state(safe_path(args.state)).get("current"):
            raise RuntimeError(
                "network capability check requires no active release"
            )
        passed = network_capability_transaction(
            candidate,
            activate=_activate,
            healthy=_healthy,
            privacy_probe=lambda identity: _external_privacy_probe(
                args, identity
            ),
            deactivate=_deactivate,
        )
    if not passed:
        raise RuntimeError(
            "target Docker runtime cannot prove internal-network "
            "application reachability"
        )
    print(json.dumps({
        "mode": "network_capability_pass",
        "target_runtime": "linux-amd64-docker-ce",
        **candidate,
    }, indent=2))


def _frontend_deploy(args: argparse.Namespace) -> None:
    state_path = safe_path(args.state)
    old = _state(state_path)
    current = old.get("current")
    if not isinstance(current, dict):
        raise RuntimeError(
            "frontend-only deployment requires an active backend release"
        )
    backend = current.get("backend") or current
    if not isinstance(backend, dict):
        raise RuntimeError("active backend identity is malformed")
    candidate = bind_frontend_release(
        backend,
        args.frontend_release_dir,
    )
    if not args.apply:
        print(json.dumps({
            "mode": "plan_frontend_only_recreate",
            "candidate": candidate,
        }, indent=2))
        return
    with _host_lock():
        _ensure_apply_host("release-frontend-deploy", {
            "combined_release_id": str(candidate["combined_release_id"]),
            "state_sha256": _sha256(state_path),
        })
        _assert_host_frontend_release_permissions(candidate)
        latest = _state(state_path)
        if latest != old:
            raise RuntimeError("deployment state changed after frontend plan")
        if latest.get("deployment_transaction"):
            raise RuntimeError("an earlier deployment transaction is unresolved")
        _atomic_json(state_path, {
            **latest,
            "deployment_transaction": "pending_activation",
            "pending_candidate": candidate,
        })
        try:
            updated = deploy_transaction(
                latest,
                candidate,
                activate=_activate,
                deactivate=_deactivate,
                healthy=_healthy,
                privacy_probe=_privacy_probe,
                readiness=_rollback_readiness,
            )
        except BaseException:
            _atomic_json(state_path, latest)
            raise
        _atomic_json(state_path, updated)
    print(json.dumps({"mode": "frontend_deployed", **updated}, indent=2))


def _frontend_install(args: argparse.Namespace) -> None:
    incoming = safe_path(args.incoming_release_dir)
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_artifact_digest):
        raise ValueError("approved frontend digest must be 64 lowercase hex")
    verified = verify_frontend_release(incoming, authority_root=ROOT)
    _require_published_frontend(verified)
    if verified.get("artifact_digest") != args.expected_artifact_digest:
        raise ValueError("incoming release differs from approved frontend digest")
    release_root = safe_path(args.release_root)
    destination = release_root / str(verified["artifact_digest"])
    if not args.apply:
        print(json.dumps({
            "mode": "plan_frontend_install",
            "incoming_release_directory": str(incoming),
            "destination": str(destination),
            "artifact_digest": verified["artifact_digest"],
            "public_source_revision": verified[
                "frontend_public_source_revision"
            ],
        }, indent=2))
        return
    with _host_lock():
        _ensure_apply_host("release-frontend-install", {
            "frontend_artifact_digest": args.expected_artifact_digest,
            "incoming_manifest_sha256": _sha256(
                incoming / "frontend-release.json"
            ),
        })
        installed = install_frontend_release(
            incoming,
            release_root,
            expected_artifact_digest=args.expected_artifact_digest,
        )
        root_metadata = release_root.lstat()
        if (
            root_metadata.st_uid != 0
            or root_metadata.st_mode & 0o022
        ):
            raise RuntimeError(
                "frontend release root ownership or permissions are unsafe"
            )
        _assert_host_frontend_release_permissions({
            "frontend": installed,
        })
    print(json.dumps({"mode": "frontend_installed", **installed}, indent=2))


def _rollback(args: argparse.Namespace) -> None:
    state_path = safe_path(args.state)
    if not args.apply:
        state = _state(state_path)
        print(json.dumps({"mode": "plan", "state": state}, indent=2))
        return
    with _host_lock():
        _ensure_apply_host("release-rollback", {
            "state_sha256": _sha256(state_path),
        })
        state = _state(state_path)
        updated = rollback_transaction(
            state,
            activate=_activate,
            healthy=_healthy,
            privacy_probe=_privacy_probe,
        )
        _atomic_json(state_path, updated)
    print(json.dumps({"mode": "rolled_back", **updated}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--image", required=True)
    export.add_argument("--archive", type=Path, required=True)
    export.add_argument("--receipt", type=Path, required=True)
    export.add_argument("--expected-revision", required=True)
    export.add_argument("--expected-image-id", required=True)
    export.add_argument("--apply", action="store_true")
    export.set_defaults(handler=_export)

    packet_export = commands.add_parser("packet-export")
    packet_export.add_argument("--image", required=True)
    packet_export.add_argument("--public-source-root", type=Path, required=True)
    packet_export.add_argument("--source-archive", type=Path, required=True)
    packet_export.add_argument("--transfer-archive", type=Path, required=True)
    packet_export.add_argument("--receipt", type=Path, required=True)
    packet_export.add_argument("--private-tooling-revision", required=True)
    packet_export.add_argument("--public-source-revision", required=True)
    packet_export.add_argument("--expected-image-id", required=True)
    packet_export.add_argument("--apply", action="store_true")
    packet_export.set_defaults(handler=_packet_export)

    for command_name, handler in (
        ("packet-verify", _packet_verify),
        ("packet-load", _packet_load),
    ):
        packet = commands.add_parser(command_name)
        packet.add_argument("--source-archive", type=Path, required=True)
        packet.add_argument("--transfer-archive", type=Path, required=True)
        packet.add_argument("--receipt", type=Path, required=True)
        packet.add_argument("--private-tooling-revision", required=True)
        packet.add_argument("--public-source-revision", required=True)
        packet.add_argument("--expected-image-id", required=True)
        if command_name == "packet-load":
            packet.add_argument("--apply", action="store_true")
        packet.set_defaults(handler=handler)

    import_image = commands.add_parser("import")
    import_image.add_argument("--archive", type=Path, required=True)
    import_image.add_argument("--receipt", type=Path, required=True)
    import_image.add_argument("--expected-revision", required=True)
    import_image.add_argument("--expected-image-id", required=True)
    import_image.add_argument("--apply", action="store_true")
    import_image.set_defaults(handler=_import)

    deploy = commands.add_parser("deploy")
    deploy.add_argument("--receipt", type=Path, required=True)
    deploy.add_argument(
        "--supply-chain-receipt", type=Path, required=True
    )
    deploy.add_argument("--privacy-receipt", type=Path, required=True)
    deploy.add_argument("--expected-revision", required=True)
    deploy.add_argument("--frontend-release-dir", type=Path)
    deploy.add_argument("--state", type=Path, default=DEFAULT_STATE)
    deploy.add_argument("--apply", action="store_true")
    deploy.set_defaults(handler=_deploy)

    network_check = commands.add_parser("network-check")
    network_check.add_argument("--receipt", type=Path, required=True)
    network_check.add_argument("--expected-revision", required=True)
    network_check.add_argument("--frontend-release-dir", type=Path)
    network_check.add_argument(
        "--state", type=Path, default=DEFAULT_STATE
    )
    network_check.add_argument("--privacy-base-url")
    network_check.add_argument("--privacy-credential-file", type=Path)
    network_check.add_argument("--approved-hostname-file", type=Path)
    network_check.add_argument(
        "--privacy-sink-file", type=Path, action="append", default=[]
    )
    network_check.add_argument("--privacy-receipt", type=Path)
    network_check.add_argument("--apply", action="store_true")
    network_check.set_defaults(handler=_network_check)

    frontend_deploy = commands.add_parser("frontend-deploy")
    frontend_deploy.add_argument(
        "--frontend-release-dir", type=Path, required=True
    )
    frontend_deploy.add_argument(
        "--state", type=Path, default=DEFAULT_STATE
    )
    frontend_deploy.add_argument("--apply", action="store_true")
    frontend_deploy.set_defaults(handler=_frontend_deploy)

    frontend_install = commands.add_parser("frontend-install")
    frontend_install.add_argument(
        "--incoming-release-dir", type=Path, required=True
    )
    frontend_install.add_argument(
        "--expected-artifact-digest", required=True
    )
    frontend_install.add_argument(
        "--release-root",
        type=Path,
        default=DEFAULT_FRONTEND_RELEASE_ROOT,
    )
    frontend_install.add_argument("--apply", action="store_true")
    frontend_install.set_defaults(handler=_frontend_install)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--state", type=Path, default=DEFAULT_STATE)
    rollback.add_argument("--apply", action="store_true")
    rollback.set_defaults(handler=_rollback)

    args = parser.parse_args()
    try:
        args.handler(args)
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        ValueError,
    ) as error:
        raise SystemExit(
            f"staging release failed: {type(error).__name__}: {error}"
        ) from None


if __name__ == "__main__":
    main()
