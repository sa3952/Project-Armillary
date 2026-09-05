#!/usr/bin/env python3
"""Build the committed CycloneDX view from exact Python locks."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.markers import Marker, default_environment


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_LOCK = PROJECT_ROOT / "deploy/requirements.lock"
DEV_LOCK = PROJECT_ROOT / "backend/requirements-dev.lock"
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def _components(lock: Path, scope: str) -> list[dict]:
    logical: list[str] = []
    buffer = ""
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("--", "-r", "-c")) and not buffer:
            raise ValueError(f"unsupported top-level lock directive: {line}")
        buffer += (" " if buffer else "") + line.removesuffix("\\").strip()
        if not line.endswith("\\"):
            logical.append(buffer)
            buffer = ""
    if buffer:
        raise ValueError("unterminated lock requirement continuation")

    components = []
    for record in logical:
        requirement_text = record.split("--hash=", 1)[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            raise ValueError(f"cannot parse lock requirement: {requirement_text}") from exc
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise ValueError(f"lock requirement is not exactly pinned: {requirement_text}")
        hashes = _HASH.findall(record)
        if not hashes:
            raise ValueError(f"lock requirement has no SHA-256: {requirement_text}")
        component = {
            "type": "library",
            "name": requirement.name,
            "version": pins[0].version,
            "purl": f"pkg:pypi/{requirement.name}@{pins[0].version}",
            "scope": scope,
            "hashes": [
                {"alg": "SHA-256", "content": digest} for digest in hashes
            ],
        }
        if requirement.marker is not None:
            component["properties"] = [{
                "name": "pep508_marker", "value": str(requirement.marker)
            }]
        components.append(component)
    return components


def _lock_digest_properties() -> list[dict]:
    return [{
        "name": f"lock_digest:{lock.name}",
        "value": hashlib.sha256(lock.read_bytes()).hexdigest(),
    } for lock in (RUNTIME_LOCK, DEV_LOCK)]


def runtime_distribution_names(architecture: str) -> frozenset[str]:
    environment = cast(dict[str, str], default_environment())
    environment.update({
        "os_name": "posix",
        "platform_machine": {"amd64": "x86_64", "arm64": "aarch64"}[architecture],
        "platform_system": "Linux",
        "python_full_version": "3.14.7",
        "python_version": "3.14",
        "sys_platform": "linux",
    })
    result = set()
    for component in _components(RUNTIME_LOCK, "required"):
        properties = component.get("properties", [])
        marker = next(
            (item["value"] for item in properties if item["name"] == "pep508_marker"),
            None,
        )
        if marker is None or Marker(marker).evaluate(environment):
            result.add(component["name"])
    return frozenset(result)


def build() -> dict:
    runtime = _components(RUNTIME_LOCK, "required")
    runtime_names = {component["name"] for component in runtime}
    development = [
        component for component in _components(DEV_LOCK, "optional")
        if component["name"] not in runtime_names
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "classical-astrology-app",
                "description": "Classical Western astrology astronomical calculation service",
            },
            "properties": [
                *_lock_digest_properties(),
                {
                    "name": "component_source",
                    "value": (
                        "deploy/requirements.lock (the file the image installs from) "
                        "and backend/requirements-dev.lock, not the installed environment"
                    ),
                },
                {
                    "name": "scope_semantics",
                    "value": (
                        "required = shipped and executed at runtime; optional = "
                        "development and test only, not part of the AGPL corresponding "
                        "source of the running program"
                    ),
                },
                {
                    "name": "limitations",
                    "value": (
                        "PyPI packages only. It does not cover the Swiss "
                        "Ephemeris data files, the vendored third-party source "
                        "under third_party/, the base image's OS packages, or "
                        "the IANA time zone database — each of which has its "
                        "own evidence path and none of which are pip packages."
                    ),
                },
            ],
        },
        "components": runtime + development,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    document = build()
    output = Path(args.output)

    if args.check:
        if not output.is_file():
            print(f"SBOM MISSING: {output}")
            return 1
        try:
            committed = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("SBOM UNREADABLE: committed file is not valid JSON")
            return 1
        if committed != document:
            print("SBOM STALE: committed document differs from current locks")
            return 1
        print(f"SBOM CURRENT: {len(document['components'])} components")
        return 0

    output.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    required = sum(c["scope"] == "required" for c in document["components"])
    print(
        f"SBOM WRITTEN: {output} — {required} runtime, "
        f"{len(document['components']) - required} development-only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
