"""Small fail-closed file helpers for privileged staging transactions."""

from __future__ import annotations

import os
import ipaddress
import json
from pathlib import Path
import re
import stat
import tempfile


def safe_absolute_path(path: Path, *, role: str = "staging") -> Path:
    absolute = Path(os.path.abspath(path))
    if any(candidate.is_symlink() for candidate in (absolute, *absolute.parents)):
        raise ValueError(f"{role} path contains a symlink")
    return absolute


def require_trusted_parent(path: Path, *, role: str) -> None:
    """Require a parent in which only the current operator can replace names."""

    parent = path.parent
    metadata = parent.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError(
            f"{role} parent must be operator-owned and not group/world writable"
        )


def path_matches_descriptor(path: Path, descriptor: int) -> bool:
    """Compare a pathname with an open regular file whose inode stays allocated."""

    held = os.fstat(descriptor)
    if not stat.S_ISREG(held.st_mode):
        return False
    try:
        current = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(current.st_mode) and (
        current.st_dev,
        current.st_ino,
    ) == (held.st_dev, held.st_ino)


def unlink_if_descriptor(path: Path, descriptor: int) -> bool:
    """Unlink only the held object inside a non-replaceable parent namespace."""

    require_trusted_parent(path, role="cleanup target")
    if not path_matches_descriptor(path, descriptor):
        return False
    path.unlink()
    return True


def read_owner_only(
    path: Path,
    *,
    role: str,
    maximum_bytes: int,
    minimum_bytes: int = 1,
) -> bytes:
    path = safe_absolute_path(path, role=role)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{role} must be an owner-only regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or not minimum_bytes <= metadata.st_size <= maximum_bytes
        ):
            raise ValueError(f"{role} ownership, mode, or size is unsafe")
        payload = os.read(descriptor, maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise ValueError(f"{role} exceeds its bounded size")
        return payload
    finally:
        os.close(descriptor)


def read_owner_ipv4_cidr(path: Path, *, role: str) -> str:
    lines = read_owner_only(
        path, role=role, minimum_bytes=9, maximum_bytes=64
    ).decode("ascii").splitlines()
    if len(lines) != 1 or lines[0] != lines[0].strip():
        raise ValueError(f"{role} must contain one clean line")
    network = ipaddress.ip_network(lines[0], strict=True)
    if network.version != 4 or network.prefixlen != 32:
        raise ValueError(f"{role} must be one IPv4 /32")
    return str(network)


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_server_name(value: str) -> str:
    """Return one canonical FQDN and reject IP literals or DNS-invalid labels."""

    value = value.rstrip(".").lower()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("server name must be a fully qualified DNS name")
    if len(value) > 253 or len(value.split(".")) < 2:
        raise ValueError("server name must be a fully qualified DNS name")
    if not all(_DOMAIN_LABEL.fullmatch(label) for label in value.split(".")):
        raise ValueError("server name contains invalid DNS syntax")
    return value


def atomic_owner_only_replace(path: Path, payload: bytes, *, role: str) -> None:
    path = safe_absolute_path(path, role=role)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_owner_only_new(path: Path, payload: bytes) -> None:
    """Create one new owner-only regular file without a permissive interval."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def emit_json_receipt(receipt: dict, output: Path | None) -> None:
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    if output is None:
        print(payload.decode(), end="")
    else:
        target = safe_absolute_path(output, role="receipt")
        if target.exists():
            raise SystemExit(f"refusing to overwrite receipt: {target}")
        write_owner_only_new(target, payload)
    if receipt.get("result") != "pass":
        raise SystemExit(1)
