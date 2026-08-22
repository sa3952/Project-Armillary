#!/usr/bin/env python3
"""Prove the Linux pyswisseph wheel was built from the expected source archive."""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any
from pathlib import Path
import platform
import sys
import zipfile


def _default_source_root() -> Path:
    script = Path(__file__).resolve()
    # Repository execution keeps the script under scripts/publication.  The
    # Dockerfile deliberately copies the same consumer to /build, where only
    # one parent exists before filesystem root.  The verifier must be portable
    # across both maintained entrypoints.
    if len(script.parents) > 2:
        return script.parents[2]
    return script.parent


PROJECT_ROOT = _default_source_root()


def _external_receipt_path(path: Path) -> Path:
    output = path.resolve()
    root = PROJECT_ROOT.resolve()
    if output == root or root in output.parents:
        raise ValueError("Linux source-build receipt must be outside source")
    return output


EXPECTED_PYTHON = "3.13.14"
EXPECTED_PACKAGE_VERSION = "2.10.3.2"
EXPECTED_SOURCE_SHA256 = (
    "c54c305e83dbd5d2b71e58d8a69d8ee41de24c4d3328ce09e2af860a3537624d"
)
ELF_MACHINE_BY_PLATFORM = {
    "aarch64": 183,
    "x86_64": 62,
}


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _one(paths: list[Path], description: str) -> Path:
    if len(paths) != 1:
        raise RuntimeError(f"expected one {description}, found {len(paths)}")
    return paths[0]


def verify(source_dir: Path, wheel_dir: Path) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise RuntimeError("source-build proof must run on Linux")
    if platform.python_version() != EXPECTED_PYTHON:
        raise RuntimeError(
            f"expected Python {EXPECTED_PYTHON}, got {platform.python_version()}"
        )

    source = _one(
        sorted(source_dir.glob(f"pyswisseph-{EXPECTED_PACKAGE_VERSION}.tar.gz")),
        "pyswisseph source archive",
    )
    source_digest = _digest(source)
    if source_digest != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("pyswisseph source archive digest mismatch")

    wheel = _one(
        sorted(wheel_dir.glob(f"pyswisseph-{EXPECTED_PACKAGE_VERSION}-*.whl")),
        "pyswisseph wheel",
    )
    with zipfile.ZipFile(wheel) as archive:
        extension_members = sorted(
            name
            for name in archive.namelist()
            if name.startswith("swisseph.") and name.endswith(".so")
        )
        extension_member = _one(
            [Path(name) for name in extension_members],
            "Linux swisseph extension",
        ).as_posix()
        extension_binary = archive.read(extension_member)
        extension_header = extension_binary[:4]
        forbidden_native_members = [
            name
            for name in archive.namelist()
            if name.endswith((".dylib", ".dll", ".pyd"))
        ]
    if extension_header != b"\x7fELF":
        raise RuntimeError("swisseph extension is not an ELF binary")
    if len(extension_binary) < 20:
        raise RuntimeError("swisseph ELF header is truncated")
    byte_order = extension_binary[5]
    if byte_order == 1:
        elf_machine = int.from_bytes(extension_binary[18:20], "little")
    elif byte_order == 2:
        elf_machine = int.from_bytes(extension_binary[18:20], "big")
    else:
        raise RuntimeError("swisseph ELF header has unknown byte order")
    machine = platform.machine()
    expected_elf_machine = ELF_MACHINE_BY_PLATFORM.get(machine)
    if expected_elf_machine is None:
        raise RuntimeError(f"unsupported Linux build machine: {machine}")
    if elf_machine != expected_elf_machine:
        raise RuntimeError(
            f"swisseph ELF e_machine {elf_machine} does not match "
            f"{machine} ({expected_elf_machine})"
        )
    if forbidden_native_members:
        raise RuntimeError("wheel includes a non-Linux native library")

    return {
        "schema_version": "pyswisseph-linux-source-build-v1",
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "machine": machine,
        "source": {
            "filename": source.name,
            "sha256": source_digest,
        },
        "wheel": {
            "filename": wheel.name,
            "sha256": _digest(wheel),
            "extension_member": extension_member,
            "extension_format": "ELF",
            "elf_e_machine": elf_machine,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = verify(args.source_dir, args.wheel_dir)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    try:
        receipt_path = _external_receipt_path(args.receipt)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "OK: verified Linux source build "
        f"{receipt['wheel']['filename']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
