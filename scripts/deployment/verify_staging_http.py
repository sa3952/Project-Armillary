#!/usr/bin/env python3
"""Authenticated HTTPS staging checks with credentials read from a file."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    build_opener,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
)

from scripts.tools.staging_secure_io import (
    emit_json_receipt,
    read_owner_only,
    safe_absolute_path as safe_path,
    validate_server_name,
)


ALIAS = re.compile(r"^[A-Za-z0-9_-]{12,32}$")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


_DIRECT_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def read_credential(path: Path) -> tuple[str, str]:
    lines = read_owner_only(
        path, role="credential", minimum_bytes=3, maximum_bytes=4096
    ).decode("utf-8").splitlines()
    if len(lines) != 2 or not lines[0] or not lines[1]:
        raise ValueError("credential file must contain alias and password")
    if not ALIAS.fullmatch(lines[0]):
        raise ValueError("credential alias syntax is invalid")
    return lines[0], lines[1]


def read_approved_hostname(path: Path) -> str:
    text = read_owner_only(
        path, role="approved hostname", minimum_bytes=3, maximum_bytes=254
    ).decode("utf-8")
    lines = text.splitlines()
    if len(lines) != 1 or lines[0] != lines[0].strip():
        raise ValueError("approved hostname file must contain one clean line")
    return validate_server_name(lines[0])


def _authorization(alias: str, password: str) -> str:
    encoded = base64.b64encode(f"{alias}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


def _request(
    url: str,
    *,
    authorization: str | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 35,
    maximum_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[int, bytes, dict[str, str]]:
    headers = dict(extra_headers or {})
    if authorization:
        headers["Authorization"] = authorization
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=body, headers=headers)
    try:
        with _DIRECT_OPENER.open(request, timeout=timeout) as response:
            return response.status, _bounded_response_body(response, maximum_bytes), dict(response.headers)
    except HTTPError as error:
        return error.code, _bounded_response_body(error, maximum_bytes), dict(error.headers)
    except (URLError, OSError, TimeoutError):
        return 0, b"", {}


def _bounded_response_body(response, maximum_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    length = response.headers.get("Content-Length")
    if length is not None and int(length) > maximum_bytes:
        raise ValueError("staging response exceeds bounded byte limit")
    body = response.read(maximum_bytes + 1)
    if len(body) > maximum_bytes:
        raise ValueError("staging response exceeds bounded byte limit")
    return body


def principal_budget_isolated(
    unauthenticated_statuses: list[int],
    valid_after_burst_status: int,
) -> bool:
    """Judge the real edge sequence without treating one status as the proof."""

    return (
        bool(unauthenticated_statuses)
        and all(value in {401, 429} for value in unauthenticated_statuses)
        and valid_after_burst_status == 200
    )


def validate_base_url(
    base_url: str,
    *,
    fixture_mode: bool = False,
    approved_hostname: str | None = None,
) -> str:
    parsed = urlparse(base_url)
    fixture_origin = (
        fixture_mode
        and parsed.scheme == "http"
        and parsed.hostname in ("127.0.0.1", "localhost")
    )
    if not fixture_origin and (
        parsed.scheme != "https" or parsed.port not in (None, 443)
    ):
        raise ValueError("real staging verification requires HTTPS port 443")
    if (
        not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("base URL must be a bare origin without credentials")
    expected_netloc = parsed.hostname
    if parsed.port is not None:
        expected_netloc += f":{parsed.port}"
    if parsed.netloc != expected_netloc:
        raise ValueError("base URL authority is malformed")
    if not fixture_mode:
        if approved_hostname is None:
            raise ValueError("real staging verification requires approved hostname")
        if parsed.hostname != validate_server_name(approved_hostname):
            raise ValueError("base URL does not match approved staging hostname")
    return base_url.rstrip("/")


def verify(
    base_url: str,
    credential: tuple[str, str],
    *,
    fixture_mode: bool = False,
    approved_hostname: str | None = None,
) -> dict[str, object]:
    base_url = validate_base_url(
        base_url,
        fixture_mode=fixture_mode,
        approved_hostname=approved_hostname,
    )
    alias, password = credential
    auth = _authorization(alias, password)
    wrong = _authorization(alias, secrets.token_urlsafe(24))
    cases: dict[str, bool] = {}
    if not fixture_mode:
        hostname = urlparse(base_url).hostname
        assert hostname is not None
        http_origin = f"http://{hostname}"
        boundary = "/http-boundary"
        status, _, headers = _request(
            http_origin + boundary + "?discard=sensitive"
        )
        cases["http_redirect_308"] = (
            status == 308
            and headers.get("Location")
            == base_url + boundary + "?discard=sensitive"
        )
        status, _, _ = _request(
            http_origin
            + "/.well-known/acme-challenge/private-alpha-missing"
        )
        cases["http_acme_missing_404"] = status == 404
    status, _, _ = _request(base_url + "/")
    cases["no_credential_401"] = status == 401
    status, _, _ = _request(base_url + "/", authorization=wrong)
    cases["wrong_credential_401"] = status == 401
    status, _, headers = _request(base_url + "/", authorization=auth)
    if status == 308 and headers.get("location") == "/zh-TW/":
        # Do not follow arbitrary redirects with Basic auth.  The public
        # frontend owns exactly this same-origin entrypoint, so issue a new
        # separately bounded request only to that approved path.
        status, _, headers = _request(
            base_url + "/zh-TW/", authorization=auth
        )
        cases["valid_credential_entrypoint"] = status == 200
    else:
        cases["valid_credential_entrypoint"] = status == 200
    cases["valid_credential_200"] = status == 200
    cases["noindex"] = "noindex" in headers.get("X-Robots-Tag", "").lower()
    hsts = headers.get("Strict-Transport-Security", "").lower()
    match = re.search(r"(?:^|;)\s*max-age=(\d+)(?:;|$)", hsts)
    cases["hsts"] = bool(match and int(match.group(1)) > 0)
    for path in ("/openapi.json", "/api/runtime-health"):
        status, _, _ = _request(base_url + path, authorization=auth)
        cases[path] = status == 404
    unauthenticated_burst = [
        _request(base_url + "/", authorization=wrong)[0]
        for _ in range(12)
    ]
    valid_after_burst, _, _ = _request(
        base_url + "/zh-TW/",
        authorization=auth,
    )
    cases["principal_budget_isolation"] = principal_budget_isolated(
        unauthenticated_burst,
        valid_after_burst,
    )
    return {
        "schema_version": 1,
        "result": "pass" if all(cases.values()) else "fail",
        "cases": cases,
        "credential_material_recorded": False,
        "base_url_recorded": False,
        "principal_budget_observation": {
            "unauthenticated_401": unauthenticated_burst.count(401),
            "unauthenticated_429": unauthenticated_burst.count(429),
            "valid_after_burst_status": valid_after_burst,
        },
        "deferred_active_cases": [
            "valid_chart_200",
            "422",
            "malformed",
            "413",
            "415",
            "429",
            "503",
            "slow_body",
            "timeout",
            "revoked_credential",
            "worker_restart",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--approved-hostname-file", type=Path)
    parser.add_argument("--fixture-mode", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({
            "mode": "plan",
            "network_requests_sent": False,
            "credential_file_opened": False,
        }, indent=2))
        return
    if not args.fixture_mode and not args.approved_hostname_file:
        raise SystemExit(
            "real staging verification requires --approved-hostname-file"
        )
    approved_hostname = (
        read_approved_hostname(safe_path(args.approved_hostname_file))
        if args.approved_hostname_file
        else None
    )
    receipt = verify(
        args.base_url,
        read_credential(safe_path(args.credential_file)),
        fixture_mode=args.fixture_mode,
        approved_hostname=approved_hostname,
    )
    emit_json_receipt(receipt, args.output)


if __name__ == "__main__":
    main()
