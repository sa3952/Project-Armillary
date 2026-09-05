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


EXPECTED_PYTHON = "3.14.7"
EXPECTED_PACKAGE_VERSION = "2.10.3.2"
EXPECTED_SOURCE_SHA256 = (
    "c54c305e83dbd5d2b71e58d8a69d8ee41de24c4d3328ce09e2af860a3537624d"
)
EXPECTED_SOURCE_REQUIREMENT = (
    "file:///source/pyswisseph-2.10.3.2.tar.gz#sha256="
    + EXPECTED_SOURCE_SHA256
)
ELF_MACHINE_BY_PLATFORM = {
    "aarch64": 183,
    "x86_64": 62,
}
# The source contains C++ translation units; the resulting ELF must record its
# C++ runtime rather than relying on setuptools language inference.
REQUIRED_SHARED_LIBRARY = "libstdc++.so.6"

_PT_LOAD = 1
_PT_DYNAMIC = 2
_DT_NULL = 0
_DT_NEEDED = 1
_DT_STRTAB = 5
_DT_STRSZ = 10


def elf_dynamic_needed(blob: bytes) -> tuple[str, ...]:
    """Return the shared libraries an ELF object records as needed.

    Every path that cannot be read is a refusal rather than an empty tuple: a
    reader that silently returned nothing would let a missing dependency read
    as a satisfied one.
    """

    if blob[:4] != b"\x7fELF":
        raise RuntimeError("not an ELF object")
    if len(blob) < 64:
        raise RuntimeError("ELF header is truncated")
    wide = blob[4] == 2
    if blob[5] == 1:
        order: Any = "little"
    elif blob[5] == 2:
        order = "big"
    else:
        raise RuntimeError("ELF header has unknown byte order")

    def number(offset: int, size: int) -> int:
        chunk = blob[offset : offset + size]
        if len(chunk) != size:
            raise RuntimeError("ELF structure runs past the end of the object")
        return int.from_bytes(chunk, order)

    program_offset = number(0x20, 8) if wide else number(0x1C, 4)
    entry_size = number(0x36, 2) if wide else number(0x2A, 2)
    entry_count = number(0x38, 2) if wide else number(0x2C, 2)
    if not entry_count:
        raise RuntimeError("ELF object has no program headers")

    def segment(index: int) -> tuple[int, int, int, int]:
        header = program_offset + index * entry_size
        kind = number(header, 4)
        if wide:
            return (
                kind,
                number(header + 0x08, 8),
                number(header + 0x10, 8),
                number(header + 0x20, 8),
            )
        return (
            kind,
            number(header + 0x04, 4),
            number(header + 0x08, 4),
            number(header + 0x10, 4),
        )

    dynamic = next(
        (
            (offset, size)
            for kind, offset, _address, size in map(segment, range(entry_count))
            if kind == _PT_DYNAMIC
        ),
        None,
    )
    if dynamic is None:
        raise RuntimeError("ELF object has no dynamic segment")

    dynamic_offset, dynamic_size = dynamic
    stride = 16 if wide else 8
    needed_offsets: list[int] = []
    string_address: int | None = None
    string_size = 0
    for position in range(dynamic_offset, dynamic_offset + dynamic_size, stride):
        tag = number(position, 8) if wide else number(position, 4)
        value = number(position + 8, 8) if wide else number(position + 4, 4)
        if tag == _DT_NULL:
            break
        if tag == _DT_NEEDED:
            needed_offsets.append(value)
        elif tag == _DT_STRTAB:
            string_address = value
        elif tag == _DT_STRSZ:
            string_size = value
    if string_address is None or not string_size:
        raise RuntimeError("ELF dynamic segment names no string table")

    string_offset = next(
        (
            offset + (string_address - address)
            for kind, offset, address, size in map(segment, range(entry_count))
            if kind == _PT_LOAD and address <= string_address < address + size
        ),
        None,
    )
    if string_offset is None:
        raise RuntimeError("ELF string table is not inside a loaded segment")
    table = blob[string_offset : string_offset + string_size]
    if len(table) != string_size:
        raise RuntimeError("ELF string table runs past the end of the object")

    names: list[str] = []
    for offset in needed_offsets:
        end = table.find(b"\0", offset)
        if offset >= len(table) or end < 0:
            raise RuntimeError("ELF needed entry points outside the string table")
        names.append(table[offset:end].decode("ascii"))
    return tuple(sorted(names))


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


def verify(
    source_dir: Path,
    wheel_dir: Path,
    source_requirement: str,
) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise RuntimeError("source-build proof must run on Linux")
    if platform.python_version() != EXPECTED_PYTHON:
        raise RuntimeError(
            f"expected Python {EXPECTED_PYTHON}, got {platform.python_version()}"
        )
    if source_requirement != EXPECTED_SOURCE_REQUIREMENT:
        raise RuntimeError(
            "pyswisseph build did not use the exact source file URL and digest"
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
    needed = elf_dynamic_needed(extension_binary)
    if REQUIRED_SHARED_LIBRARY not in needed:
        raise RuntimeError(
            f"swisseph extension does not link {REQUIRED_SHARED_LIBRARY}; "
            f"it records {needed or '()'}"
        )

    return {
        "schema_version": "pyswisseph-linux-source-build-v1",
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "machine": machine,
        "source": {
            "filename": source.name,
            "sha256": source_digest,
        },
        "build_binding": {
            "method": "direct_file_url_with_sha256_fragment",
            "source_requirement": source_requirement,
        },
        "wheel": {
            "filename": wheel.name,
            "sha256": _digest(wheel),
            "extension_member": extension_member,
            "extension_format": "ELF",
            "elf_e_machine": elf_machine,
            "dynamic_needed": list(needed),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--source-requirement", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = verify(
            args.source_dir,
            args.wheel_dir,
            args.source_requirement,
        )
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
