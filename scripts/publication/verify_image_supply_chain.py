#!/usr/bin/env python3
"""Create auditable SBOM and vulnerability receipts for a local image.

Raw scanner matches are evidence for manual triage, not proof that a finding is
reachable or exploitable.  This gate fails on missing/forbidden runtime
dependencies or malformed/stale evidence, while preserving every raw match.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRODUCTION_LOCK = PROJECT_ROOT / "deploy" / "requirements.lock"
MAX_GRYPE_DB_AGE_HOURS = 72
REQUIRED = {"fastapi", "pydantic", "pyswisseph", "starlette", "uvicorn"}
FORBIDDEN = {
    "httpx",
    "httpx2",
    "pip-audit",
    "pip-tools",
    "pip",
    "pytest",
    "python-dotenv",
    "pyyaml",
    "uvloop",
    "watchfiles",
    "websockets",
}


class AuditFailure(RuntimeError):
    pass


def _run(command: list[str], *, timeout: int = 1800) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise AuditFailure(
            f"command failed ({result.returncode}): {command[0]}: {detail[-2000:]}"
        )
    return result.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _parse_utc_timestamp(value: str) -> datetime:
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(rendered)
    if parsed.tzinfo is None:
        raise AuditFailure("Grype database build time omitted timezone")
    return parsed.astimezone(timezone.utc)


def _database_age_hours(built: str, checked_at: datetime) -> float:
    built_at = _parse_utc_timestamp(built)
    age_hours = (checked_at - built_at).total_seconds() / 3600
    if age_hours < -1:
        raise AuditFailure("Grype database build time is in the future")
    if age_hours > MAX_GRYPE_DB_AGE_HOURS:
        raise AuditFailure(
            f"Grype database is stale: {age_hours:.1f}h exceeds "
            f"{MAX_GRYPE_DB_AGE_HOURS}h"
        )
    return max(age_hours, 0)


def _db_status(grype: str) -> dict[str, Any]:
    raw = _run([grype, "db", "status", "-o", "json"])
    status = json.loads(raw)
    built = (
        status.get("built")
        or status.get("buildDate")
        or status.get("database", {}).get("built")
    )
    if not built:
        raise AuditFailure("Grype database status omitted database_built")
    database_source = status.get("from", "")
    checksum = status.get("checksum")
    if not checksum and database_source:
        checksum = parse_qs(urlparse(database_source).query).get(
            "checksum", [None]
        )[0]
    if not checksum:
        raise AuditFailure("Grype database status omitted checksum evidence")
    checked_at = datetime.now(timezone.utc)
    age_hours = _database_age_hours(str(built), checked_at)
    return {
        "database_built": built,
        "checked_at": checked_at.isoformat(),
        "age_hours": round(age_hours, 3),
        "max_allowed_age_hours": MAX_GRYPE_DB_AGE_HOURS,
        "schema_version": status.get("schemaVersion")
        or status.get("schema_version"),
        "checksum": checksum,
    }


def _python_inventory(sbom: dict[str, Any]) -> set[str]:
    packages = set()
    for artifact in sbom.get("artifacts", []):
        if artifact.get("type") == "python":
            packages.add(_normalized(artifact.get("name", "")))
    return packages


def _matches(grype: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = []
    for match in grype.get("matches", []):
        vulnerability = match.get("vulnerability", {})
        artifact = match.get("artifact", {})
        fix_versions = vulnerability.get("fix", {}).get("versions") or []
        normalized.append(
            {
                "id": vulnerability.get("id"),
                "severity": vulnerability.get("severity", "Unknown"),
                "package": artifact.get("name"),
                "installed_version": artifact.get("version"),
                "package_type": artifact.get("type"),
                "fix_available": bool(fix_versions),
                "fix_versions": fix_versions,
                "data_source": vulnerability.get("dataSource"),
            }
        )
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--syft", default="syft")
    parser.add_argument("--grype", default="grype")
    parser.add_argument("--pip-audit", default="pip-audit")
    parser.add_argument(
        "--production-lock",
        type=Path,
        default=DEFAULT_PRODUCTION_LOCK,
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    try:
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=True)
        sbom_path = output / "sbom.syft.json"
        spdx_path = output / "sbom.spdx.json"
        grype_path = output / "grype.json"
        pip_audit_path = output / "pip-audit.json"
        _run([args.grype, "db", "update"])
        _run(
            [
                args.syft,
                args.image,
                "-o",
                f"syft-json={sbom_path}",
                "-o",
                f"spdx-json={spdx_path}",
            ]
        )
        _run(
            [
                args.grype,
                f"sbom:{sbom_path}",
                "-o",
                "json",
                "--file",
                str(grype_path),
            ]
        )
        pip_audit_command = [
            args.pip_audit,
            "--require-hashes",
            "--disable-pip",
            "--no-deps",
            "--format",
            "json",
            "--output",
            str(pip_audit_path),
            "--requirement",
            str(args.production_lock.resolve()),
        ]
        _run(pip_audit_command)
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        grype = json.loads(grype_path.read_text(encoding="utf-8"))
        pip_audit = json.loads(
            pip_audit_path.read_text(encoding="utf-8")
        )
        python_packages = _python_inventory(sbom)
        missing = REQUIRED - python_packages
        forbidden = FORBIDDEN & python_packages
        if missing or forbidden:
            raise AuditFailure(
                f"Python inventory mismatch: missing={sorted(missing)}, "
                f"forbidden={sorted(forbidden)}"
            )
        matches = _matches(grype)
        severity = Counter(item["severity"] for item in matches)
        fix_available = sum(item["fix_available"] for item in matches)
        image = json.loads(
            _run(["docker", "image", "inspect", args.image])
        )[0]
        database = _db_status(args.grype)
        high_or_critical = severity["High"] + severity["Critical"]
        dependencies = (
            pip_audit
            if isinstance(pip_audit, list)
            else pip_audit.get("dependencies", [])
        )
        vulnerabilities = sum(
            len(item.get("vulns", [])) for item in dependencies
        )
        normalized_pip_audit_command = list(pip_audit_command)
        normalized_pip_audit_command[0] = Path(
            normalized_pip_audit_command[0]
        ).name
        normalized_pip_audit_command[
            normalized_pip_audit_command.index("--output") + 1
        ] = "<audit-output>/pip-audit.json"
        normalized_pip_audit_command[
            normalized_pip_audit_command.index("--requirement") + 1
        ] = "deploy/requirements.lock"
        python_dependency_audit = {
            "artifact_sha256": _sha256(pip_audit_path),
            "dependency_count": len(dependencies),
            "known_vulnerability_count": vulnerabilities,
            "observed_argv_normalized": normalized_pip_audit_command,
            "production_lock_sha256": _sha256(
                args.production_lock.resolve()
            ),
            "exit_code": 0,
        }
        receipt = {
            "schema_version": "private-alpha-supply-chain-summary-v1",
            "candidate": {
                "image_reference": args.image,
                "image_id": image["Id"],
                "os": image["Os"],
                "architecture": image["Architecture"],
                "revision": (image.get("Config", {}).get("Labels") or {}).get(
                    "org.opencontainers.image.revision"
                ),
            },
            "tools": {
                "syft": _run([args.syft, "version"]).strip(),
                "grype": _run([args.grype, "version"]).strip(),
                "pip_audit": _run(
                    [args.pip_audit, "--version"]
                ).strip(),
            },
            "grype_database": database,
            "artifact_sha256": {
                "syft_json": _sha256(sbom_path),
                "spdx_json": _sha256(spdx_path),
                "grype_json": _sha256(grype_path),
                "pip_audit_json": _sha256(pip_audit_path),
            },
            "inventory": {
                "sbom_packages": len(sbom.get("artifacts", [])),
                "production_python_distributions": len(python_packages),
                "required_packages_present": sorted(REQUIRED),
                "forbidden_packages_present": sorted(forbidden),
                "pip_present": "pip" in python_packages,
            },
            "grype_raw_matches": {
                **dict(sorted(severity.items())),
                "total": len(matches),
                "matches_with_scanner_fix_available": fix_available,
            },
            "python_dependency_audit": python_dependency_audit,
            "decision": (
                "manual_triage_required"
                if high_or_critical
                else "no_high_or_critical_matches"
            ),
            "interpretation": (
                "Raw package matches remain in the generated Grype artifact "
                "for evidence-backed manual triage and are not automatically "
                "reachable or exploitable findings."
            ),
            "limitations": [
                (
                    "This is a local image receipt, not a registry manifest "
                    "or published artifact."
                ),
                (
                    "Raw scanner outputs remain local evidence and must be "
                    "regenerated for each release candidate."
                ),
                (
                    "This receipt is not whole-repository security approval "
                    "or deployment readiness."
                ),
            ],
        }
        receipt_path = args.receipt or output / "receipt.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except (
        AuditFailure,
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
