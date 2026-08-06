from __future__ import annotations

import json


def test_release_openapi_generator_writes_hosted_schema_only(tmp_path):
    from scripts.publication.generate_static_openapi import write_static_openapi

    output = tmp_path / "private-alpha-openapi.json"
    write_static_openapi(output)
    schema = json.loads(output.read_text(encoding="utf-8"))

    assert "/api/chart" in schema["paths"]
    assert "/api/places/search" in schema["paths"]
    assert "/api/health" in schema["paths"]
    assert "/api/runtime-health" not in schema["paths"]
    assert output.read_bytes().endswith(b"\n")


def test_hosted_openapi_describes_sanitized_validation_errors_and_timezone_requirements(
    tmp_path,
):
    from scripts.publication.generate_static_openapi import write_static_openapi

    output = tmp_path / "private-alpha-openapi.json"
    write_static_openapi(output)
    schema = json.loads(output.read_text(encoding="utf-8"))

    validation_response = schema["paths"]["/api/chart"]["post"]["responses"][
        "422"
    ]["content"]["application/json"]["schema"]
    assert validation_response == {
        "anyOf": [
            {"$ref": "#/components/schemas/HostedValidationResponse"},
            {"$ref": "#/components/schemas/HostedBoundaryErrorResponse"},
        ],
        "title": "Response 422 Compute Chart Api Chart Post",
    }

    issue = schema["components"]["schemas"]["HostedValidationIssue"]
    assert set(issue["required"]) == {"loc", "type"}
    assert "msg" not in issue["properties"]

    for status_code in ("400", "413", "415", "431", "503"):
        boundary_response = schema["paths"]["/api/chart"]["post"][
            "responses"
        ][status_code]["content"]["application/json"]["schema"]
        assert boundary_response == {
            "$ref": "#/components/schemas/HostedBoundaryErrorResponse"
        }

    place_search = schema["paths"]["/api/places/search"]["post"]
    assert place_search["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/PlaceSearchRequest"}
    for status_code in ("400", "413", "415", "431", "503"):
        assert place_search["responses"][status_code]["content"][
            "application/json"
        ]["schema"] == {
            "$ref": "#/components/schemas/HostedBoundaryErrorResponse"
        }

    timezone = schema["components"]["schemas"]["TimezoneInput"]
    conditions = timezone["allOf"]
    assert {
        "if": {
            "properties": {"mode": {"const": "iana"}},
            "required": ["mode"],
        },
        "then": {
            "properties": {
                "iana_name": {"minLength": 1, "type": "string"}
            },
            "required": ["iana_name"],
        },
    } in conditions
    assert {
        "if": {
            "properties": {"mode": {"const": "fixed_offset"}},
            "required": ["mode"],
        },
        "then": {
            "properties": {
                "utc_offset_hours": {
                    "maximum": 14.0,
                    "minimum": -14.0,
                    "type": "number",
                },
                "fold": {"const": 0},
            },
            "required": ["utc_offset_hours"],
        },
    } in conditions
