#!/usr/bin/env python3
"""Generate one-time sensitive input and scan only operator-visible sinks."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from typing import Any, NamedTuple
from urllib.parse import quote_from_bytes

from scripts.deployment.verify_staging_http import (
    _authorization,
    _request,
    read_credential,
    read_approved_hostname,
    safe_path,
    validate_base_url,
)
from scripts.tools.staging_secure_io import emit_json_receipt, write_owner_only_new


class Canary(NamedTuple):
    marker: str
    payload_bytes: bytes
    sensitive_fragments: tuple[str, ...]


RECEIPT_MAX_AGE_SECONDS = 60 * 60


def validate_receipt(
    receipt: dict, candidate: dict, *, now: int | None = None
) -> dict[str, str]:
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
        or current - created > RECEIPT_MAX_AGE_SECONDS
    ):
        raise ValueError("privacy receipt is stale or has an invalid timestamp")
    backend = candidate.get("backend") or candidate
    if receipt.get("candidate") != {
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
    required = {"success", "422", "malformed", "413", "415", "429"}
    checks = receipt.get("request_case_checks")
    if not isinstance(checks, dict) or set(checks) != required or not all(checks.values()):
        raise ValueError("privacy receipt request controls are incomplete")
    if receipt.get("deferred_request_cases") != {
        "503": "requires_concurrency_drill",
        "slow_body": "requires_raw_tls_drill",
        "timeout": "requires_bounded_worker_drill",
        "unexpected_error": "disposable_runtime_only",
        "worker_restart": "requires_bounded_worker_drill",
    }:
        raise ValueError("privacy receipt deferred controls are misclassified")
    return {"image_id": str(backend["image_id"]), "revision": str(backend["vcs_revision"])}


def generate_canary() -> Canary:
    marker = "PA-" + secrets.token_hex(16)
    year = 1960 + secrets.randbelow(55)
    minute = secrets.randbelow(60)
    latitude = round(-70 + secrets.randbelow(1400000) / 10000, 4)
    longitude = round(-170 + secrets.randbelow(3400000) / 10000, 4)
    payload = {
        "datetime": {
            "year": year,
            "month": 2,
            "day": 3,
            "hour": 4,
            "minute": minute,
            "second": 17,
        },
        "timezone": {"mode": "iana", "iana_name": "Etc/UTC"},
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            "altitude_m": 37,
        },
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compact_fragments = (
        marker,
        f'"year":{year}',
        f'"minute":{minute}',
        f'"latitude":{latitude}',
        f'"longitude":{longitude}',
        "Etc/UTC",
    )
    coordinate_variants: list[str] = []
    for field, value in (
        ("latitude", latitude),
        ("longitude", longitude),
    ):
        rendered_values = {str(value), format(value, "g")}
        for rendered in rendered_values:
            coordinate_variants.extend((
                f"'{field}': {rendered}",
                f'"{field}": {rendered}',
                f"{field}={rendered}",
            ))
    formatting_variants = (
        f"'year': {year}",
        f'"year": {year}',
        f"'minute': {minute}",
        f'"minute": {minute}',
        f"{latitude},{longitude}",
        *coordinate_variants,
    )
    encoded_variants = (
        base64.b64encode(marker.encode("ascii")).decode("ascii"),
        base64.b64encode(encoded).decode("ascii"),
        quote_from_bytes(encoded),
        encoded.hex(),
    )
    return Canary(
        marker,
        encoded,
        tuple(dict.fromkeys((
            *compact_fragments,
            *formatting_variants,
            *encoded_variants,
        ))),
    )


def _matches(canary: Canary, content: str) -> int:
    return sum(
        1 for fragment in canary.sensitive_fragments if fragment in content
    )


def scanner_self_test(canary: Canary) -> bool:
    payload = json.loads(canary.payload_bytes)
    year = payload["datetime"]["year"]
    latitude = payload["location"]["latitude"]
    longitude = payload["location"]["longitude"]
    controls = (
        canary.payload_bytes.decode("utf-8"),
        json.dumps(payload, indent=2),
        repr({"year": year, "latitude": latitude}),
        f"chart computed for {latitude},{longitude}",
    )
    if not all(_matches(canary, control) > 0 for control in controls):
        return False
    with tempfile.TemporaryDirectory(
        prefix="private-alpha-canary-control-"
    ) as directory:
        control_path = Path(directory) / "positive-control.log"
        write_owner_only_new(
            control_path,
            (canary.marker + "\n").encode("utf-8"),
        )
        return _matches(
            canary,
            control_path.read_text(encoding="utf-8"),
        ) > 0


def snapshot_sinks(sinks: list[Path]) -> dict[Path, tuple[int, int]]:
    return {
        sink: (sink.stat().st_size, sink.stat().st_mtime_ns)
        for sink in sinks
    }


def scan_sinks(
    canary: Canary,
    sinks: list[Path],
    *,
    baseline: dict[Path, tuple[int, int]] | None = None,
) -> dict[str, object]:
    results: list[dict[str, Any]] = []
    for sink in sinks:
        content = sink.read_text(encoding="utf-8", errors="replace")
        matches = _matches(canary, content)
        results.append({
            "sink_name_sha256": hashlib.sha256(
                sink.name.encode("utf-8")
            ).hexdigest(),
            "match_count": matches,
            "byte_length": len(content.encode("utf-8")),
            "line_count": len(content.splitlines()),
            "changed_since_request_start": (
                baseline is not None
                and baseline.get(sink)
                != (sink.stat().st_size, sink.stat().st_mtime_ns)
            ),
        })
    return {
        "schema_version": 1,
        "marker_sha256": hashlib.sha256(
            canary.marker.encode("ascii")
        ).hexdigest(),
        "sinks": results,
        "total_match_count": sum(item["match_count"] for item in results),
        "sink_observation_present": any(
            item["byte_length"] > 0
            and item["changed_since_request_start"] is True
            for item in results
        ),
        "all_sinks_observed": bool(results) and all(
            item["byte_length"] > 0
            and item["changed_since_request_start"] is True
            for item in results
        ),
        "scanner_self_test_passed": scanner_self_test(canary),
        "provider_invisible_sinks": "not_inspectable",
        "credential_material_recorded": False,
        "request_or_response_recorded": False,
    }


def evidence_passes(
    receipt: dict[str, Any],
    request_checks: dict[str, bool],
) -> bool:
    return (
        receipt.get("total_match_count") == 0
        and (
            receipt.get("all_sinks_observed") is True
            or receipt.get("quiet_sink_authority_verified") is True
        )
        and receipt.get("scanner_self_test_passed") is True
        and bool(request_checks)
        and all(request_checks.values())
    )


def exercise_requests(
    base_url: str,
    credential: tuple[str, str],
    canary: Canary,
) -> dict[str, object]:
    authorization = _authorization(*credential)
    header = {"X-Private-Alpha-Canary": canary.marker}
    cases: dict[str, object] = {}
    cases["success"] = _request(
        base_url + "/api/chart",
        authorization=authorization,
        body=canary.payload_bytes,
        content_type="application/json",
        extra_headers=header,
    )[0]
    invalid = json.loads(canary.payload_bytes)
    invalid["datetime"]["month"] = 13
    invalid[canary.marker] = canary.marker
    cases["422"] = _request(
        base_url + "/api/chart",
        authorization=authorization,
        body=json.dumps(invalid).encode(),
        content_type="application/json",
        extra_headers=header,
    )[0]
    cases["malformed"] = _request(
        base_url + "/api/chart",
        authorization=authorization,
        body=(b'{"canary":"' + canary.marker.encode()),
        content_type="application/json",
        extra_headers=header,
    )[0]
    cases["413"] = _request(
        base_url + "/api/chart",
        authorization=authorization,
        body=canary.marker.encode() + b"x" * (17 * 1024),
        content_type="application/json",
        extra_headers=header,
    )[0]
    cases["415"] = _request(
        base_url + "/api/chart",
        authorization=authorization,
        body=canary.marker.encode(),
        content_type="text/plain",
        extra_headers=header,
    )[0]
    burst = []
    for _ in range(45):
        status = _request(
            base_url + "/api/chart",
            authorization=authorization,
            body=(b'{"canary":"' + canary.marker.encode()),
            content_type="application/json",
            extra_headers=header,
        )[0]
        burst.append(status)
        if status == 429:
            break
    cases["429"] = 429 if 429 in burst else "not_observed"
    cases["503"] = "requires_concurrency_drill"
    cases["slow_body"] = "requires_raw_tls_drill"
    cases["timeout"] = "requires_bounded_worker_drill"
    cases["unexpected_error"] = "disposable_runtime_only"
    cases["worker_restart"] = "requires_bounded_worker_drill"
    return cases


def evaluate_request_cases(cases: dict[str, Any]) -> dict[str, bool]:
    expected = {
        "success": 200,
        "422": 422,
        "malformed": 422,
        "413": 413,
        "415": 415,
        "429": 429,
    }
    return {
        name: cases.get(name) == status
        for name, status in expected.items()
    }


def deferred_request_cases(cases: dict[str, Any]) -> dict[str, str]:
    expected = {
        "503": "requires_concurrency_drill",
        "slow_body": "requires_raw_tls_drill",
        "timeout": "requires_bounded_worker_drill",
        "unexpected_error": "disposable_runtime_only",
        "worker_restart": "requires_bounded_worker_drill",
    }
    observed = {name: cases.get(name) for name in expected}
    if observed != expected:
        raise ValueError("deferred privacy request cases are misclassified")
    return expected


def quiet_nginx_sink_authority(
    sinks: list[Path], config_text: str,
) -> bool:
    """Prove that an empty sink is the effective privacy-preserving path."""
    return (
        sinks == [Path("/var/log/nginx/error.log")]
        and bool(re.search(r"(?m)^\s*access_log\s+off;\s*$", config_text))
        and bool(re.search(
            r"(?m)^\s*error_log\s+/var/log/nginx/error\.log\s+emerg;\s*$",
            config_text,
        ))
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--approved-hostname-file", type=Path)
    parser.add_argument("--sink-file", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-revision")
    parser.add_argument("--expected-image-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({
            "mode": "plan",
            "marker_generated": False,
            "network_requests_sent": False,
            "required_cases": [
                "success",
                "422",
                "malformed",
                "413",
                "415",
                "429",
                "unexpected_error_disposable_runtime",
                "worker_restart",
            ],
        }, indent=2))
        return
    if not args.sink_file:
        raise SystemExit("--apply requires at least one --sink-file")
    if (
        not args.base_url
        or not args.credential_file
        or not args.approved_hostname_file
        or not args.expected_revision
        or not args.expected_image_id
    ):
        raise SystemExit(
            "--apply requires base URL, credential file, approved hostname, "
            "expected revision, and expected image ID"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_revision):
        raise SystemExit("--expected-revision must be a full lowercase Git SHA")
    if not re.fullmatch(
        r"sha256:[0-9a-f]{64}", args.expected_image_id
    ):
        raise SystemExit("--expected-image-id must be an immutable image ID")
    sink_paths = [safe_path(path) for path in args.sink_file]
    baseline = snapshot_sinks(sink_paths)
    canary = generate_canary()
    base_url = validate_base_url(
        args.base_url,
        approved_hostname=read_approved_hostname(
            safe_path(args.approved_hostname_file)
        ),
    )
    cases = exercise_requests(
        base_url,
        read_credential(safe_path(args.credential_file)),
        canary,
    )
    receipt = scan_sinks(canary, sink_paths, baseline=baseline)
    nginx = subprocess.run(
        ["nginx", "-T"], text=True, capture_output=True, check=False,
    )
    receipt["quiet_sink_authority_verified"] = (
        nginx.returncode == 0
        and quiet_nginx_sink_authority(
            sink_paths, nginx.stdout + "\n" + nginx.stderr,
        )
    )
    receipt["schema_version"] = 2
    receipt["verification_scope"] = "network_check_external_privacy_v1"
    receipt["created_at_epoch"] = int(time.time())
    receipt["candidate"] = {
        "revision": args.expected_revision,
        "image_id": args.expected_image_id,
    }
    receipt["request_cases"] = cases
    # Bound before storing: reading it back out of the receipt loses the type,
    # and the checks are what decides pass/fail.
    request_case_checks = evaluate_request_cases(cases)
    receipt["request_case_checks"] = request_case_checks
    receipt["deferred_request_cases"] = deferred_request_cases(cases)
    receipt["result"] = (
        "pass" if evidence_passes(receipt, request_case_checks) else "fail"
    )
    emit_json_receipt(receipt, args.output)


if __name__ == "__main__":
    main()
