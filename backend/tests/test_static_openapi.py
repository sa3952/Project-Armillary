from __future__ import annotations

import json


def test_release_openapi_generator_writes_hosted_schema_only(tmp_path):
    from scripts.generate_static_openapi import write_static_openapi

    output = tmp_path / "private-alpha-openapi.json"
    write_static_openapi(output)
    schema = json.loads(output.read_text(encoding="utf-8"))

    assert "/api/chart" in schema["paths"]
    assert "/api/health" in schema["paths"]
    assert "/api/runtime-health" not in schema["paths"]
    assert output.read_bytes().endswith(b"\n")
