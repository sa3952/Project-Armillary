"""Atomically claim one-time, purpose-bound host authorization files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
from collections.abc import Mapping

from scripts.tools.source_tree_identity import sha256_file


MARKER_SCHEMA_VERSION = 1
MAX_GRANT_LIFETIME_SECONDS = 15 * 60
MAX_MARKER_BYTES = 16 * 1024


class AuthorizationNotClaimed(RuntimeError):
    """No valid grant was atomically acquired; mutation must not begin."""


def sanitize_privileged_environment() -> None:
    """Replace command lookup and dynamic-loader inputs with a closed set."""
    os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    for name in tuple(os.environ):
        if name == "PYTHONPATH" or name == "LD_PRELOAD" or name.startswith(
            "DYLD_"
        ):
            os.environ.pop(name, None)


def _validate_parent(marker: Path) -> os.stat_result:
    try:
        parent = marker.parent.lstat()
    except FileNotFoundError as error:
        raise AuthorizationNotClaimed(
            f"authorization parent missing: {marker.parent}"
        ) from error
    if not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700:
        raise AuthorizationNotClaimed(
            f"authorization parent must be a trusted 0700 directory: {marker.parent}"
        )
    if parent.st_uid != os.geteuid():
        raise AuthorizationNotClaimed("authorization parent has the wrong owner")
    return parent


def _validate_trust(marker: Path) -> os.stat_result:
    _validate_parent(marker)
    try:
        metadata = marker.lstat()
    except FileNotFoundError as error:
        raise AuthorizationNotClaimed(
            f"authorization marker missing: {marker}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise AuthorizationNotClaimed("authorization marker must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise AuthorizationNotClaimed("authorization marker must have mode 0600")
    if metadata.st_uid != os.geteuid():
        raise AuthorizationNotClaimed("authorization marker has the wrong owner")
    return metadata


def sha256_descriptor(descriptor: int) -> str:
    current = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.lseek(descriptor, current, os.SEEK_SET)


def issue(
    marker: Path,
    *,
    purpose: str,
    bindings: Mapping[str, str],
    lifetime_seconds: int = 5 * 60,
    now: int | None = None,
) -> dict[str, object]:
    """Create one fresh grant without overwriting an existing marker."""

    _validate_parent(marker)
    if not purpose or not bindings:
        raise AuthorizationNotClaimed(
            "authorization purpose and at least one binding are required"
        )
    if not 1 <= lifetime_seconds <= MAX_GRANT_LIFETIME_SECONDS:
        raise AuthorizationNotClaimed("authorization lifetime is out of range")
    normalized = dict(sorted(bindings.items()))
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or not value
        for key, value in normalized.items()
    ):
        raise AuthorizationNotClaimed("authorization bindings are invalid")
    issued_at = int(time.time()) if now is None else now
    grant: dict[str, object] = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "purpose": purpose,
        "issued_at": issued_at,
        "expires_at": issued_at + lifetime_seconds,
        "bindings": normalized,
    }
    payload = (json.dumps(grant, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return grant


def _parse_grant(payload: bytes) -> dict[str, object]:
    if len(payload) > MAX_MARKER_BYTES:
        raise AuthorizationNotClaimed("authorization marker is too large")
    try:
        grant = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorizationNotClaimed(
            "authorization marker is not valid JSON"
        ) from error
    if not isinstance(grant, dict) or grant.get("schema_version") != 1:
        raise AuthorizationNotClaimed("authorization marker schema is unsupported")
    return grant


def claim(
    marker: Path,
    *,
    expected_purpose: str,
    expected_bindings: Mapping[str, str],
    now: int | None = None,
) -> str:
    """Take one grant; restore the same inode if its purpose is wrong.

    Restoration uses a no-overwrite hard link inside the trusted directory.
    A concurrently created marker is never replaced. Any restore failure stays
    fail-closed and must be resolved by the operator before mutation.
    """

    before = _validate_trust(marker)
    sanitize_privileged_environment()
    spent = marker.with_name(
        f"{marker.name}.spent.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        os.rename(marker, spent)
    except OSError as error:
        raise AuthorizationNotClaimed(
            f"authorization could not be claimed atomically: {marker}"
        ) from error
    try:
        after = spent.lstat()
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise AuthorizationNotClaimed("claimed authorization identity changed")
        grant = _parse_grant(spent.read_bytes())
        purpose = grant.get("purpose")
        bindings = grant.get("bindings")
        issued_at = grant.get("issued_at")
        expires_at = grant.get("expires_at")
        current = int(time.time()) if now is None else now
        mismatch = None
        if not isinstance(purpose, str) or purpose != expected_purpose:
            mismatch = (
                f"authorization purpose {purpose!r} does not match "
                f"{expected_purpose!r}"
            )
        elif bindings != dict(sorted(expected_bindings.items())):
            mismatch = "authorization object/content bindings do not match"
        elif (
            type(issued_at) is not int
            or type(expires_at) is not int
            or expires_at <= issued_at
            or expires_at - issued_at > MAX_GRANT_LIFETIME_SECONDS
            or issued_at > current + 30
            or current >= expires_at
        ):
            mismatch = "authorization grant is expired or has invalid freshness"
        if mismatch is not None:
            try:
                os.link(spent, marker, follow_symlinks=False)
            except OSError as error:
                raise AuthorizationNotClaimed(
                    f"{mismatch}; the grant could not be restored "
                    "without overwriting the marker path"
                ) from error
            raise AuthorizationNotClaimed(
                f"{mismatch}; the original grant was restored"
            )
        return str(purpose)
    finally:
        spent.unlink(missing_ok=True)


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("marker", type=Path)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--binding", action="append", default=[])
    parser.add_argument("--lifetime-seconds", type=int, default=5 * 60)
    args = parser.parse_args()
    bindings: dict[str, str] = {}
    for item in args.binding:
        key, separator, value = item.partition("=")
        if not separator or key in bindings:
            raise SystemExit("each --binding must be a unique KEY=VALUE")
        bindings[key] = value
    try:
        issue(
            args.marker,
            purpose=args.purpose,
            bindings=bindings,
            lifetime_seconds=args.lifetime_seconds,
        )
    except (AuthorizationNotClaimed, OSError) as error:
        raise SystemExit(f"authorization issue failed: {error}") from None


if __name__ == "__main__":
    _main()
