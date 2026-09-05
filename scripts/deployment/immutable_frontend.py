#!/usr/bin/env python3
"""Verify and install one immutable published frontend release."""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from scripts.deployment.release_packet import EXTERNAL_FRONTEND_MODE
from scripts.tools.staging_secure_io import safe_absolute_path as safe_path
try:
    from app.frontend_release import SOURCE_URL_PREFIX, combined_release_id, verify_release as verify_frontend_release
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    from app.frontend_release import SOURCE_URL_PREFIX, combined_release_id, verify_release as verify_frontend_release
ROOT = Path(__file__).resolve().parents[2]
PUBLISHED_FRONTEND_SOURCE_STATUS = "published_and_expected_anonymously_reachable"

def _require_published_frontend(frontend: dict) -> None:
    if frontend.get("source_url_status") != PUBLISHED_FRONTEND_SOURCE_STATUS:
        raise ValueError(
            "frontend release must be published and anonymously reachable"
        )

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
        raise ValueError("deployment identity must be a combined release")
    frontend = identity.get("frontend")
    backend = identity.get("backend")
    combined = identity.get("combined_release_id")
    if (
        not isinstance(frontend, dict)
        or not isinstance(backend, dict)
        or not isinstance(combined, str)
    ):
        raise ValueError("combined release identity is incomplete")
    rebuilt = bind_frontend_release(
        backend,
        Path(str(frontend.get("release_directory"))),
    )
    if frontend != rebuilt["frontend"]:
        raise ValueError("persisted frontend identity differs from release")
    if combined != rebuilt["combined_release_id"]:
        raise ValueError("persisted combined release ID is invalid")


def frontend_install_plan(
    incoming_release: Path,
    release_root: Path,
    *,
    expected_artifact_digest: str,
) -> tuple[Path, Path, dict[str, object]]:
    incoming = safe_path(incoming_release)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_artifact_digest):
        raise ValueError("approved frontend digest must be 64 lowercase hex")
    verified = verify_frontend_release(incoming, authority_root=ROOT)
    _require_published_frontend(verified)
    if verified.get("artifact_digest") != expected_artifact_digest:
        raise ValueError("incoming release differs from approved frontend digest")
    root = safe_path(release_root)
    return incoming, root / expected_artifact_digest, verified


def install_frontend_release(
    incoming_release: Path,
    release_root: Path,
    *,
    expected_artifact_digest: str,
) -> dict[str, object]:
    incoming, destination, _verified = frontend_install_plan(
        incoming_release,
        release_root,
        expected_artifact_digest=expected_artifact_digest,
    )
    root = destination.parent
    root.mkdir(parents=True, exist_ok=True)
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
