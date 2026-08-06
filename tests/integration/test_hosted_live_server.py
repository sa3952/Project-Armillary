from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BACKEND_ROOT = str(Path(__file__).resolve().parents[2] / "backend")
PROFILE_ENV = "CLASSICAL_ASTROLOGY_PROFILE"
CANARY = "HOSTED_PRIVACY_CANARY_842761"


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _payload() -> dict:
    return {
        "datetime": {
            "year": 1997,
            "month": 8,
            "day": 17,
            "hour": 9,
            "minute": 42,
            "second": 0,
        },
        "timezone": {"mode": "iana", "iana_name": "Asia/Taipei"},
        "location": {
            "latitude": 24.1477,
            "longitude": 120.6736,
            "altitude_m": 80,
        },
    }


def _request(
    url: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    headers = {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = Request(url, data=body, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def _wait_until_ready(process: subprocess.Popen, base_url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                "hosted server stopped early:\n" + process.stdout.read()
            )
        try:
            status, body = _request(f"{base_url}/api/health")
            if status == 200:
                health = json.loads(body)
                assert health["status"] == "ok"
                assert health["ready"] is True
                return
        except URLError:
            pass
        time.sleep(0.05)
    raise AssertionError("hosted server startup timed out")


def _stop_and_read(process: subprocess.Popen) -> str:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return process.stdout.read()


def test_real_hosted_server_success_rejections_and_process_output_are_bounded():
    port = _unused_local_port()
    environment = {**os.environ, PROFILE_ENV: "private_alpha"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        _wait_until_ready(process, base_url)
        success = _request(
            f"{base_url}/api/chart",
            body=json.dumps(_payload()).encode(),
            content_type="application/json",
        )
        wrong_type = _request(
            f"{base_url}/api/chart",
            body=CANARY.encode(),
            content_type="text/plain",
        )
        malformed = _request(
            f"{base_url}/api/chart",
            body=(b'{"datetime":"' + CANARY.encode()),
            content_type="application/json",
        )
        invalid_payload = _payload()
        invalid_payload["datetime"]["month"] = 13
        invalid_payload["location"][CANARY] = CANARY
        invalid = _request(
            f"{base_url}/api/chart",
            body=json.dumps(invalid_payload).encode(),
            content_type="application/json",
        )
        oversized = _request(
            f"{base_url}/api/chart",
            body=CANARY.encode() + b"x" * (16 * 1024),
            content_type="application/json",
        )
        trailing_slash = _request(
            f"{base_url}/api/chart/",
            body=CANARY.encode(),
            content_type="application/json",
        )
        future_child = _request(
            f"{base_url}/api/chart/future",
            body=CANARY.encode(),
            content_type="application/json",
        )
        lookalike = _request(f"{base_url}/api/chartography")
        openapi = _request(f"{base_url}/openapi.json")
        runtime = _request(f"{base_url}/api/runtime-health")
    finally:
        output = _stop_and_read(process)

    assert success[0] == 200
    assert wrong_type[0] == 415
    assert malformed[0] == 422
    assert invalid[0] == 422
    assert oversized[0] == 413
    assert trailing_slash[0] == 405
    assert future_child[0] == 405
    assert lookalike[0] == 404
    assert openapi[0] == 404
    assert runtime[0] == 404
    assert CANARY not in malformed[1].decode()
    assert CANARY not in invalid[1].decode()
    assert CANARY not in output
    assert '"POST /api/chart HTTP/' not in output
    assert output.count('"route":"/api/chart"') == 5
    assert output.count('"route":"frontend_or_unmatched"') == 3
    assert "Traceback (most recent call last)" not in output


def test_real_hosted_server_unexpected_error_is_generic_and_not_logged():
    port = _unused_local_port()
    script = "\n".join(
        (
            "import uvicorn",
            "from app import main",
            "from app.settings import AppProfile, AppSettings",
            "def fail(_request):",
            f"    raise RuntimeError({CANARY!r})",
            "main._compute_chart_locked = fail",
            (
                "main.app = main.create_app("
                "AppSettings(profile=AppProfile.PRIVATE_ALPHA))"
            ),
            (
                "uvicorn.run(main.app, host='127.0.0.1', "
                f"port={port}, access_log=False)"
            ),
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        _wait_until_ready(process, base_url)
        status, body = _request(
            f"{base_url}/api/chart",
            body=json.dumps(_payload()).encode(),
            content_type="application/json",
        )
    finally:
        output = _stop_and_read(process)

    assert status == 500
    assert json.loads(body)["detail"]["code"] == "internal_server_error"
    assert CANARY not in body.decode()
    assert CANARY not in output
    assert '"POST /api/chart HTTP/' not in output
    assert output.count('"route":"/api/chart"') == 1
    assert "Traceback (most recent call last)" not in output


def test_real_local_server_sanitizes_validation_input_and_nonfinite_numbers():
    """The response boundary is private in local mode too, not only hosted."""

    port = _unused_local_port()
    environment = {**os.environ, PROFILE_ENV: "local"}
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app", "--host",
            "127.0.0.1", "--port", str(port), "--no-access-log",
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    coordinate = "25.0337891"
    try:
        _wait_until_ready(process, base_url)
        wrong_type_payload = _payload()
        wrong_type_payload["location"]["latitude"] = coordinate
        wrong_type = _request(
            f"{base_url}/api/chart",
            body=json.dumps(wrong_type_payload).encode(),
            content_type="application/json",
        )
        nonfinite_body = json.dumps(_payload()).replace("24.1477", "NaN", 1)
        nonfinite = _request(
            f"{base_url}/api/chart",
            body=nonfinite_body.encode(),
            content_type="application/json",
        )
    finally:
        output = _stop_and_read(process)

    assert wrong_type[0] == 422
    assert nonfinite[0] == 422
    assert coordinate not in wrong_type[1].decode()
    assert "NaN" not in nonfinite[1].decode()
    assert coordinate not in output
    assert "Traceback (most recent call last)" not in output
