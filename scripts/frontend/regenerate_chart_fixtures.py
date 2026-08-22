#!/usr/bin/env python3
"""Check or explicitly regenerate frontend chart fixtures from the current backend."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FIXTURES = ROOT / "frontend/tests/fixtures"
REQUESTS = FIXTURES / "chart-requests.json"
MANIFEST = FIXTURES / "chart-fixture-manifest.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object, *, indent: int = 1) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True)
        + "\n"
    ).encode()


def _fixture_bytes(filename: str, value: object) -> bytes:
    # The full all-modules fixture deliberately uses wider indentation for
    # human review; preserve its established bytes instead of reformatting the
    # entire 14k-line evidence object on every regeneration.
    return _json_bytes(value, indent=4 if filename == "chart-all-modules.json" else 1)


def _backend_source_sha256() -> str:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", "backend/app"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = sorted(raw.decode() for raw in completed.stdout.split(b"\0") if raw)
    digest = hashlib.sha256()
    for relative in paths:
        body = (ROOT / relative).read_bytes()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(body).digest())
    return digest.hexdigest()


def _responses(requests: dict[str, dict]) -> dict[str, dict]:
    # The backend package is not installed into the repository-tool import
    # root; this bounded producer temporarily exposes backend/ only while it
    # calls the real app entry, then removes the path in finally.
    sys.path.insert(0, str(BACKEND))
    try:
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        responses = {}
        for filename, payload in requests.items():
            response = client.post("/api/chart", json=payload)
            if response.status_code != 200:
                raise RuntimeError(
                    f"fixture request failed for {filename}: "
                    f"HTTP {response.status_code} {response.text[:400]}"
                )
            responses[filename] = response.json()
        return responses
    finally:
        sys.path.remove(str(BACKEND))


def _manifest(requests: dict[str, dict], responses: dict[str, dict]) -> dict:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "schema_version": "frontend-chart-fixture-manifest-v1",
        "producer": "scripts.frontend.regenerate_chart_fixtures",
        "source_revision_at_generation": revision,
        "backend_source_sha256": _backend_source_sha256(),
        "fixtures": {
            filename: {
                "request_sha256": _sha256(_json_bytes(requests[filename])),
                "response_sha256": _sha256(_fixture_bytes(filename, response)),
                "schema_version": response.get("schema_version"),
                "dossier_version": response.get(
                    "calculation_dossier", {}
                ).get("dossier_version"),
                "generation_status": "current_backend_response",
            }
            for filename, response in responses.items()
        },
    }


def _check(requests: dict[str, dict], responses: dict[str, dict]) -> None:
    from scripts.tools.semantic_currentness import protected_semantic_mismatches

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "frontend-chart-fixture-manifest-v1"
        or manifest.get("producer") != "scripts.frontend.regenerate_chart_fixtures"
        or manifest.get("backend_source_sha256") != _backend_source_sha256()
        or set(manifest.get("fixtures", {})) != set(requests)
    ):
        raise RuntimeError("frontend fixture manifest is stale or incomplete")
    for filename, response in responses.items():
        fixture = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
        entry = manifest["fixtures"][filename]
        if entry.get("request_sha256") != _sha256(_json_bytes(requests[filename])):
            raise RuntimeError(f"fixture request identity is stale: {filename}")
        if entry.get("response_sha256") != _sha256(_fixture_bytes(filename, fixture)):
            raise RuntimeError(f"fixture response identity is stale: {filename}")
        mismatches = protected_semantic_mismatches(fixture, response)
        if mismatches:
            raise RuntimeError(
                f"fixture protected semantics differ from current backend: "
                f"{filename}: {mismatches[:5]}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    requests = json.loads(REQUESTS.read_text(encoding="utf-8"))
    if not isinstance(requests, dict) or not requests:
        raise RuntimeError("frontend fixture request universe is empty")
    responses = _responses(requests)
    if args.check:
        _check(requests, responses)
        print(f"FRONTEND FIXTURES CURRENT: {len(responses)}")
        return 0
    for filename, response in responses.items():
        (FIXTURES / filename).write_bytes(_fixture_bytes(filename, response))
    MANIFEST.write_bytes(_json_bytes(_manifest(requests, responses)))
    print(f"FRONTEND FIXTURES WRITTEN: {len(responses)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
