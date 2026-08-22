#!/usr/bin/env python3
"""Capture deterministic BuildKit-context and ephemeral builder evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def context_manifest(root: Path) -> dict[str, object]:
    """Observe the filesystem BuildKit mounted, without following links."""
    root = root.resolve()
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        common: dict[str, object] = {
            "path": relative,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "size_bytes": None,
            "sha256": None,
            "symlink_target": None,
        }
        if stat.S_ISREG(metadata.st_mode):
            common.update(
                type="file",
                size_bytes=metadata.st_size,
                sha256=_sha256(path),
            )
        elif stat.S_ISDIR(metadata.st_mode):
            common["type"] = "directory"
        elif stat.S_ISLNK(metadata.st_mode):
            common.update(type="symlink", symlink_target=os.readlink(path))
        else:
            common["type"] = "special"
        entries.append(common)
    canonical = json.dumps(
        entries, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema_version": "build-context-identity-v1",
        "producer": "builder_mount_observation",
        "entries": entries,
        "identity_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _command(*arguments: str) -> dict[str, object]:
    try:
        completed = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"argv": list(arguments), "status": "unavailable", "error": str(error)}
    return {
        "argv": list(arguments),
        "status": "evaluated",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _executable(name: str) -> dict[str, object]:
    resolved = shutil.which(name)
    if resolved is None:
        return {"name": name, "status": "unavailable"}
    path = Path(resolved).resolve()
    return {
        "name": name,
        "status": "evaluated",
        "path": path.as_posix(),
        "sha256": _sha256(path),
    }


def _os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def toolchain_receipt(*, target_platform: str) -> dict[str, object]:
    packages = _command("dpkg-query", "-W", "-f=${Package}\t${Version}\n")
    tools = {
        name: {
            "identity": _executable(name),
            "version": _command(name, "--version"),
        }
        for name in ("gcc", "g++", "cpp", "ld", "as", "make", "python")
    }
    distributions = sorted(
        {
            f"{distribution.metadata['Name']}=={distribution.version}"
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        }
    )
    return {
        "schema_version": "builder-toolchain-receipt-v1",
        "status": "observed_ephemeral_builder",
        "TARGETPLATFORM": target_platform,
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "os-release": _os_release(),
        # Exclude the ephemeral container hostname.  It is not a toolchain axis
        # and would make two otherwise identical isolated builds incomparable.
        "uname": _command("uname", "-srm"),
        "libc": _command("ldd", "--version"),
        "dpkg-query": packages,
        "tools": tools,
        "python_distributions": distributions,
        "compile_environment": {
            key: os.environ.get(key)
            for key in (
                "CFLAGS",
                "CPPFLAGS",
                "CXXFLAGS",
                "LDFLAGS",
                "SOURCE_DATE_EPOCH",
            )
        },
    }


def native_extension_receipt(venv_root: Path) -> dict[str, object]:
    artifacts = []
    for path in sorted(venv_root.rglob("*.so"), key=lambda item: item.as_posix()):
        relative = path.relative_to(venv_root).as_posix()
        if path.name.startswith("swisseph."):
            origin = "source_built_native"
        elif "pydantic_core" in relative:
            origin = "acquired_native_wheel"
        else:
            origin = "other_venv_native"
        artifacts.append(
            {
                "path": "/opt/venv/" + relative,
                "origin_class": origin,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    if not any(item["origin_class"] == "source_built_native" for item in artifacts):
        raise RuntimeError("source-built swisseph extension is absent")
    return {
        "schema_version": "builder-native-extensions-v1",
        "scope": "native extensions installed under /opt/venv",
        "base_image_elf_scope": "not_in_this_receipt_bound_by_pinned_base_digest",
        "artifacts": artifacts,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--target-platform", required=True)
    parser.add_argument("--venv-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.source_revision or len(args.source_revision) != 40:
        print("BUILD EVIDENCE FAILED: invalid source revision", file=sys.stderr)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    context = context_manifest(args.context_root)
    toolchain = toolchain_receipt(target_platform=args.target_platform)
    native_extensions = native_extension_receipt(args.venv_root)
    contract = {
        "schema_version": "build-contract-v1",
        "source_revision": args.source_revision,
        "target_platform": args.target_platform,
        "build_context_identity_sha256": context["identity_sha256"],
        "toolchain_identity_sha256": hashlib.sha256(
            json.dumps(toolchain, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    _write_json(args.output_dir / "build-context-received.json", context)
    _write_json(args.output_dir / "builder-toolchain.json", toolchain)
    _write_json(args.output_dir / "build-contract.json", contract)
    _write_json(args.output_dir / "native-extensions.json", native_extensions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
