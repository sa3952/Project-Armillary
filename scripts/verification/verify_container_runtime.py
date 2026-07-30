#!/usr/bin/env python3
"""Build and exercise the provider-neutral Private Alpha production image."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

from scripts.verification.verify_docker_context import (
    context_file_paths,
    verify as verify_docker_context,
)
from scripts.verification.container_platform_contract import (
    platform_args as _platform_args,
    platform_contract,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DEFAULT_IMAGE = "classical-astrology-private-alpha:runtime-test"
CANARY = "PRIVATE_ALPHA_BUILD_SECRET_CANARY_8f38f0696df24f8c"
PARITY_BASELINE = PROJECT_ROOT / "deploy" / "parity-baseline-arm64.json"
CROSS_PLATFORM_FIXED_STAR_SPEED_DISTANCE_TOLERANCE = 5e-3
SAME_RUNTIME_FIXED_STAR_SPEED_DISTANCE_TOLERANCE = 1e-8
CROSS_PLATFORM_NUMERIC_ABSOLUTE_TOLERANCE = 2e-8
SAME_RUNTIME_NUMERIC_ABSOLUTE_TOLERANCE = 1e-8
ELF_MACHINE_BY_DOCKER_ARCHITECTURE = {
    "amd64": 62,
    "arm64": 183,
}
MACHINE_BY_DOCKER_ARCHITECTURE = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}
EXPECTED_PRODUCTION_PACKAGES = frozenset(
    {
        "annotated-doc",
        "annotated-types",
        "anyio",
        "click",
        "fastapi",
        "h11",
        "idna",
        "pydantic",
        "pydantic-core",
        "pyswisseph",
        "starlette",
        "typing-extensions",
        "typing-inspection",
        "uvicorn",
    }
)
EXPECTED_PRIVACY_EVENT_FIELDS = frozenset(
    {
        "event_schema_version",
        "event",
        "request_id",
        "route",
        "method",
        "status_code",
        "duration_bucket",
        "request_size_bucket",
        "outcome",
        "error_code",
    }
)
FORBIDDEN_PACKAGES = frozenset(
    {
        "httpx",
        "httpx2",
        "httptools",
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
)
RUNTIME_SPECIFIC_PARITY_PATHS = frozenset(
    {
        "$.calculation_dossier.build_identity",
        (
            "$.calculation_dossier.trace_receipt."
            "python_json_serialization_sha256"
        )
    }
)
FIXED_STAR_DISTANCE_SPEED_PATH = re.compile(
    r"^\$\.astronomical_data\.fixed_stars\[\d+\]\.speed_distance$"
)


class GateFailure(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    check: bool = True,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise GateFailure(
            f"command failed ({completed.returncode}): "
            f"{' '.join(command[:4])}: {detail[-2000:]}"
        )
    return completed


def _copy_build_context(destination: Path) -> Path:
    context = destination / "context"
    verify_docker_context(PROJECT_ROOT)
    context.mkdir()
    for relative_path in context_file_paths(PROJECT_ROOT):
        source = PROJECT_ROOT / relative_path
        target = context / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (context / "build-context-probe-8f38f069.txt").write_text(
        CANARY + "\n",
        encoding="utf-8",
    )
    return context


def _platform_contract(platform: str) -> tuple[str, str]:
    try:
        return platform_contract(platform)
    except ValueError as exc:
        raise GateFailure(str(exc)) from exc


def _build_image(
    image: str, *, require_clean: bool, platform: str | None
) -> dict[str, Any]:
    dirty_entries = _run(
        ["git", "status", "--porcelain=v1", "-z"]
    ).stdout.count("\0")
    if require_clean and dirty_entries:
        raise GateFailure(
            f"release build requires a clean checkout; found "
            f"{dirty_entries} changed path(s)"
        )
    with tempfile.TemporaryDirectory(prefix="private-alpha-build-") as raw_tmp:
        temporary = Path(raw_tmp)
        context = _copy_build_context(temporary)
        secret_file = temporary / "buildkit-secret"
        secret_file.write_text(CANARY + "\n", encoding="utf-8")
        revision = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
        completed = _run(
            [
                "docker",
                "build",
                *_platform_args(platform),
                "--file",
                "deploy/Dockerfile",
                "--tag",
                image,
                "--build-arg",
                f"VCS_REF={revision}",
                "--secret",
                f"id=private_alpha_probe,src={secret_file}",
                ".",
            ],
            cwd=context,
            timeout=1800,
        )
    if CANARY in completed.stdout or CANARY in completed.stderr:
        raise GateFailure("build output disclosed the secret canary")
    return {
        "image": image,
        "revision": revision,
        "clean_checkout": dirty_entries == 0,
        "clean_checkout_required": require_clean,
        "platform": platform,
    }


def _inspect_image(image: str, platform: str | None) -> dict[str, Any]:
    raw = _run(["docker", "image", "inspect", image]).stdout
    image_info = json.loads(raw)[0]
    config = image_info["Config"]
    if config["User"] != "10001:10001":
        raise GateFailure(f"unexpected image user: {config['User']!r}")
    environment = config.get("Env") or []
    environment_keys = {item.split("=", maxsplit=1)[0] for item in environment}
    forbidden_keys = [
        key
        for key in environment_keys
        if any(fragment in key.upper() for fragment in ("SECRET", "TOKEN", "PASSWORD"))
    ]
    if forbidden_keys:
        raise GateFailure(f"secret-like image environment keys: {forbidden_keys}")
    if "CLASSICAL_ASTROLOGY_PROFILE=private_alpha" not in environment:
        raise GateFailure("hosted profile is not fixed in the image")
    healthcheck = config.get("Healthcheck") or {}
    if "container_healthcheck.py" not in " ".join(healthcheck.get("Test") or []):
        raise GateFailure("image healthcheck does not use the bounded readiness probe")
    if CANARY in json.dumps(image_info, sort_keys=True):
        raise GateFailure("image metadata contains the secret canary")
    if platform:
        expected_os, expected_architecture = _platform_contract(platform)
        if image_info["Os"] != expected_os:
            raise GateFailure(
                f"image operating system is {image_info['Os']}, "
                f"not {expected_os}"
            )
        if image_info["Architecture"] != expected_architecture:
            raise GateFailure(
                f"image architecture is {image_info['Architecture']}, "
                f"not {expected_architecture}"
            )
    return {
        "id": image_info["Id"],
        "os": image_info["Os"],
        "architecture": image_info["Architecture"],
        "user": config["User"],
        "environment_keys": sorted(environment_keys),
        "healthcheck": healthcheck,
        "revision": config.get("Labels", {}).get(
            "org.opencontainers.image.revision"
        ),
    }


def _inventory_image(image: str, platform: str | None) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:12]
    container = f"private-alpha-inventory-{suffix}"
    _run(
        [
            "docker",
            "create",
            *_platform_args(platform),
            "--name",
            container,
            image,
        ]
    )
    try:
        with tempfile.TemporaryDirectory(prefix="private-alpha-inventory-") as raw_tmp:
            archive_path = Path(raw_tmp) / "rootfs.tar"
            _run(
                ["docker", "export", "--output", str(archive_path), container],
                timeout=600,
            )
            names: list[str] = []
            canary_found = False
            with tarfile.open(archive_path, mode="r") as archive:
                for member in archive:
                    normalized = "/" + member.name.lstrip("./")
                    names.append(normalized)
                    lowered = normalized.casefold()
                    forbidden_path = (
                        "/.git/" in lowered
                        or "/.build/" in lowered
                        or "/backend/.venv/" in lowered
                        or "/backend/tests/" in lowered
                        or "/tests/" in lowered
                        or "/app/frontend/tests/" in lowered
                        or lowered.endswith("/.ds_store")
                        or "/docs/red_team/" in lowered
                        or "/docs/archive/" in lowered
                        or normalized in {
                            "/build",
                            "/source",
                            "/wheels",
                            "/usr/bin/gcc",
                            "/usr/bin/cc",
                        }
                        or (
                            lowered.startswith("/app/")
                            and lowered.endswith((".pem", ".key", "/.env"))
                        )
                        or "/.env." in lowered
                        or "build-context-probe-8f38f069" in lowered
                    )
                    if forbidden_path:
                        raise GateFailure(
                            f"forbidden path included in image: {normalized}"
                        )
                    if member.isfile() and member.size <= 8 * 1024 * 1024:
                        extracted = archive.extractfile(member)
                        if extracted is not None and CANARY.encode() in extracted.read():
                            canary_found = True
            if canary_found:
                raise GateFailure("image filesystem contains the secret canary")
    finally:
        _run(["docker", "rm", "--force", container], check=False)

    package_output = _run(
        [
            "docker",
            "run",
            "--rm",
            *_platform_args(platform),
            "--entrypoint",
            "/opt/venv/bin/python",
            image,
            "-c",
            (
                "import importlib.metadata,json,re;"
                "print(json.dumps(sorted({re.sub(r'[-_.]+','-',"
                "d.metadata['Name']).lower() "
                "for d in importlib.metadata.distributions()})))"
            ),
        ]
    ).stdout
    packages = frozenset(json.loads(package_output.strip().splitlines()[-1]))
    missing = EXPECTED_PRODUCTION_PACKAGES - packages
    forbidden = FORBIDDEN_PACKAGES & packages
    if missing or forbidden:
        raise GateFailure(
            f"production package inventory mismatch: "
            f"missing={sorted(missing)}, forbidden={sorted(forbidden)}"
        )
    runtime_output = _run(
        [
            "docker",
            "run",
            "--rm",
            *_platform_args(platform),
            "--entrypoint",
            "/opt/venv/bin/python",
            image,
            "-c",
            (
                "import importlib.metadata,json,platform;"
                "r=json.load(open('/app/release/"
                "pyswisseph-linux-source-build.json'));"
                "p=json.load(open('/app/release/"
                "buildkit-probe-consumed.json'));"
                "print(json.dumps({'python':platform.python_version(),"
                "'machine':platform.machine(),"
                "'pyswisseph':importlib.metadata.version('pyswisseph'),"
                "'source_build':r,'buildkit_probe':p}))"
            ),
        ]
    ).stdout
    runtime = json.loads(runtime_output.strip().splitlines()[-1])
    source_build = runtime["source_build"]
    image_architecture = _inspect_image(image, platform)["architecture"]
    expected_machine = MACHINE_BY_DOCKER_ARCHITECTURE.get(
        image_architecture
    )
    expected_elf_machine = ELF_MACHINE_BY_DOCKER_ARCHITECTURE.get(
        image_architecture
    )
    if (
        runtime["python"] != "3.13.14"
        or runtime["pyswisseph"] != "2.10.3.2"
        or runtime.get("machine") != expected_machine
        or source_build.get("schema_version")
        != "pyswisseph-linux-source-build-v1"
        or source_build.get("machine") != expected_machine
        or source_build.get("source", {}).get("sha256")
        != "c54c305e83dbd5d2b71e58d8a69d8ee41de24c4d3328ce09e2af860a3537624d"
        or source_build.get("wheel", {}).get("extension_format") != "ELF"
        or source_build.get("wheel", {}).get("elf_e_machine")
        != expected_elf_machine
        or runtime.get("buildkit_probe") != {"consumed": True}
    ):
        raise GateFailure(
            "Linux source-build, architecture, BuildKit probe, or runtime "
            "version mismatch"
        )
    return {
        "rootfs_entries": len(names),
        "production_packages": sorted(packages),
        "forbidden_packages_present": sorted(forbidden),
        "secret_canary_present": False,
        "runtime": runtime,
    }


def _payloads() -> list[dict[str, Any]]:
    base: dict[str, Any] = {
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
        "options": {
            "include_fixed_stars": True,
            "include_lots": False,
            "include_antiscia": False,
            "include_void_of_course": False,
            "include_declination_aspects": False,
            "include_outer_planets": False,
            "include_lunar_phases": False,
            "include_eclipses": False,
            "include_rise_set_transits": False,
        },
    }
    modes = [
        {
            "center": "geocentric",
            "zodiac": "tropical",
            "position_mode": "apparent",
            "ecliptic_frame": "of_date",
            "nutation": True,
        },
        {
            "center": "topocentric",
            "zodiac": "sidereal",
            "ayanamsa": "fagan_bradley",
            "position_mode": "true",
            "ecliptic_frame": "j2000",
            "nutation": False,
        },
        {
            "center": "heliocentric",
            "zodiac": "tropical",
            "position_mode": "true",
            "ecliptic_frame": "j2000",
            "nutation": False,
        },
        {
            "center": "barycentric",
            "zodiac": "sidereal",
            "ayanamsa": "hipparchos",
            "position_mode": "apparent",
            "ecliptic_frame": "of_date",
            "nutation": True,
        },
    ]
    payloads = []
    for index, mode in enumerate(modes):
        payload = copy.deepcopy(base)
        payload["datetime"]["minute"] += index
        payload["computation_mode"] = mode
        payloads.append(payload)
    return payloads


def _local_baselines(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # The backend package is not installed in the repository development environment.
    # This local parity probe therefore exposes that one package root explicitly.
    sys.path.insert(0, str(BACKEND_ROOT))
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.settings import AppProfile, AppSettings

    with TestClient(create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))) as client:
        results = []
        for payload in payloads:
            response = client.post("/api/chart", json=payload)
            if response.status_code != 200:
                raise GateFailure(
                    f"local baseline failed with HTTP {response.status_code}"
                )
            results.append(response.json())
    return results


def _load_committed_cross_platform_baseline(
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        baseline = json.loads(PARITY_BASELINE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateFailure(
            f"cannot read committed cross-platform baseline: {exc}"
        ) from exc
    if (
        baseline.get("schema_version")
        != "private-alpha-four-mode-parity-baseline-v1"
        or baseline.get("source", {}).get("architecture") != "arm64"
        or baseline.get("producer", {}).get("module")
        != "scripts.verification.generate_parity_baseline"
        or baseline.get("payloads") != payloads
        or len(baseline.get("responses", [])) != len(payloads)
    ):
        raise GateFailure("committed cross-platform baseline contract mismatch")
    return baseline["responses"]


def _assert_response_shape(expected: Any, actual: Any, path: str = "$") -> None:
    normalized_path = re.sub(r"^\$\[\d+\]", "$", path)
    if normalized_path in RUNTIME_SPECIFIC_PARITY_PATHS:
        return
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise GateFailure(f"response type mismatch at {path}")
        if expected.keys() != actual.keys():
            raise GateFailure(
                f"response key mismatch at {path}: "
                f"expected={sorted(expected)} actual={sorted(actual)}"
            )
        for key in expected:
            _assert_response_shape(
                expected[key],
                actual[key],
                f"{path}.{key}",
            )
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise GateFailure(f"response list shape mismatch at {path}")
        for index, (expected_item, actual_item) in enumerate(
            zip(expected, actual, strict=True)
        ):
            _assert_response_shape(
                expected_item,
                actual_item,
                f"{path}[{index}]",
            )
        return
    if expected is None:
        if actual is not None:
            raise GateFailure(f"response nullability mismatch at {path}")
        return
    if isinstance(expected, bool):
        if not isinstance(actual, bool):
            raise GateFailure(f"response boolean type mismatch at {path}")
        return
    if isinstance(expected, (int, float)):
        if (
            not isinstance(actual, (int, float))
            or isinstance(actual, bool)
        ):
            raise GateFailure(f"response numeric type mismatch at {path}")
        return
    if not isinstance(actual, type(expected)):
        raise GateFailure(f"response scalar type mismatch at {path}")


def verify_committed_parity_baseline() -> dict[str, Any]:
    """Compare committed/current response structure before a Docker build."""
    payloads = _payloads()
    expected = _load_committed_cross_platform_baseline(payloads)
    actual = _local_baselines(payloads)
    for index, (expected_response, actual_response) in enumerate(
        zip(expected, actual, strict=True)
    ):
        _assert_response_shape(
            expected_response,
            actual_response,
            f"$[{index}]",
        )
    schema_versions = {
        response.get("schema_version")
        for response in actual
    }
    if len(schema_versions) != 1:
        raise GateFailure(
            f"current responses have mixed schema versions: "
            f"{sorted(schema_versions)}"
        )
    return {
        "status": "compatible",
        "cases": len(actual),
        "response_schema_version": schema_versions.pop(),
    }


def _assert_parity(
    expected: Any,
    actual: Any,
    path: str = "$",
    *,
    parity_scope: str = "same_runtime",
) -> None:
    # The dossier explicitly labels this Python-runtime JSON digest as
    # non-portable. The trace it receipts is still compared below, field by
    # field, including every numeric value.
    if path in RUNTIME_SPECIFIC_PARITY_PATHS:
        return
    if isinstance(expected, bool):
        if actual is not expected:
            raise GateFailure(
                f"boolean parity mismatch at {path}: "
                f"expected={expected}, actual={actual}"
            )
        return
    if expected is None or isinstance(expected, str):
        if actual != expected:
            if (
                re.fullmatch(r"^\$\.calculation_trace\[\d+\]\.title$", path)
                and isinstance(actual, str)
                and expected.startswith("恆星 ")
                and actual.startswith("恆星 ")
                and expected.endswith("位置計算")
                and actual.endswith("位置計算")
                and "".join(expected.split()).casefold()
                == "".join(actual.split()).casefold()
            ):
                return
            if re.fullmatch(
                r"^\$\.astronomical_data\.fixed_stars\[\d+\]\.catalog_name$",
                path,
            ):
                normalized_expected = "".join(expected.split()).casefold()
                normalized_actual = (
                    "".join(actual.split()).casefold()
                    if isinstance(actual, str)
                    else actual
                )
                if normalized_expected == normalized_actual:
                    return
                raise GateFailure(
                    f"catalog-name parity mismatch at {path}: "
                    f"expected={expected!r}, actual={actual!r}"
                )
            raise GateFailure(f"parity mismatch at {path}")
        return
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if FIXED_STAR_DISTANCE_SPEED_PATH.fullmatch(path):
            absolute_tolerance = (
                CROSS_PLATFORM_FIXED_STAR_SPEED_DISTANCE_TOLERANCE
                if parity_scope == "cross_platform"
                else SAME_RUNTIME_FIXED_STAR_SPEED_DISTANCE_TOLERANCE
            )
        else:
            absolute_tolerance = (
                CROSS_PLATFORM_NUMERIC_ABSOLUTE_TOLERANCE
                if parity_scope == "cross_platform"
                else SAME_RUNTIME_NUMERIC_ABSOLUTE_TOLERANCE
            )
        if not math.isclose(
            float(expected),
            float(actual),
            rel_tol=1e-12,
            abs_tol=absolute_tolerance,
        ):
            difference = abs(float(expected) - float(actual))
            raise GateFailure(
                f"numeric parity mismatch at {path}: "
                f"absolute_difference={difference:.17g}"
            )
        return
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            raise GateFailure(f"object-key parity mismatch at {path}")
        for key in expected:
            _assert_parity(
                expected[key],
                actual[key],
                f"{path}.{key}",
                parity_scope=parity_scope,
            )
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            raise GateFailure(f"list-length parity mismatch at {path}")
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            _assert_parity(
                expected_item,
                actual_item,
                f"{path}[{index}]",
                parity_scope=parity_scope,
            )
        return
    raise GateFailure(f"type parity mismatch at {path}")


def _http(
    base_url: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 60,
) -> tuple[int, bytes, dict[str, str]]:
    headers = {"Accept": "application/json"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except HTTPError as error:
        return error.code, error.read(), dict(error.headers)
    except (OSError, URLError) as error:
        raise GateFailure(f"HTTP request failed: {error.__class__.__name__}") from error


def _chart(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    status, body, _ = _http(
        base_url,
        "/api/chart",
        body=json.dumps(payload, separators=(",", ":")).encode(),
        content_type="application/json",
    )
    if status != 200:
        raise GateFailure(f"container chart request failed with HTTP {status}")
    return json.loads(body)


def _chart_after_worker_recovery(
    base_url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 10,
) -> tuple[dict[str, Any], int]:
    """Bound only the supervisor's expected post-replacement 503 window."""

    deadline = time.monotonic() + timeout
    transient_503s = 0
    while True:
        status, body, _ = _http(
            base_url,
            "/api/chart",
            body=json.dumps(payload, separators=(",", ":")).encode(),
            content_type="application/json",
        )
        if status == 200:
            return json.loads(body), transient_503s
        if status != 503 or time.monotonic() >= deadline:
            raise GateFailure(
                "post-replacement chart request failed with "
                f"HTTP {status} after {transient_503s} transient 503 responses"
            )
        transient_503s += 1
        time.sleep(0.25)


def _host_port(container: str) -> int:
    output = _run(["docker", "port", container, "8000/tcp"]).stdout.strip()
    if not output:
        raise GateFailure("Docker did not publish the test-only localhost port")
    return int(output.rsplit(":", maxsplit=1)[1])


def _wait_healthy(container: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    while time.monotonic() < deadline:
        raw = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                container,
            ],
            check=False,
        )
        if raw.returncode == 0:
            last_status = raw.stdout.strip()
            if last_status == "healthy":
                return
            if last_status == "unhealthy":
                break
        time.sleep(1)
    logs = _run(["docker", "logs", container], check=False).stdout[-2000:]
    raise GateFailure(f"container health was {last_status}: {logs}")


def _start_container(
    image: str, workers: int, platform: str | None
) -> tuple[str, str]:
    container = f"private-alpha-{workers}w-{uuid.uuid4().hex[:10]}"
    command = [
        "docker",
        "run",
        *_platform_args(platform),
        "--detach",
        "--name",
        container,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=5m",
        "--log-opt",
        "max-file=2",
        "--publish",
        "127.0.0.1::8000",
        image,
        "/opt/venv/bin/python",
        "-m",
        "uvicorn",
        "app.main:create_app",
        "--factory",
        "--app-dir",
        "/app/backend",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--workers",
        str(workers),
        "--loop",
        "asyncio",
        "--http",
        "h11",
        "--ws",
        "none",
        "--no-access-log",
        "--timeout-worker-healthcheck",
        "5",
        "--timeout-graceful-shutdown",
        "10",
        "--backlog",
        "32",
    ]
    _run(command)
    try:
        _wait_healthy(container)
        port = _host_port(container)
        return container, f"http://127.0.0.1:{port}"
    except Exception:
        _run(["docker", "rm", "--force", container], check=False)
        raise


def _runtime_controls(container: str, base_url: str) -> dict[str, Any]:
    uid = _run(["docker", "exec", container, "id", "-u"]).stdout.strip()
    if uid != "10001":
        raise GateFailure(f"runtime uid is {uid}, not 10001")
    root_write = _run(
        ["docker", "exec", container, "sh", "-c", "touch /app/write-probe"],
        check=False,
    )
    if root_write.returncode == 0:
        raise GateFailure("read-only root filesystem accepted a write")
    tmp_write = _run(
        ["docker", "exec", container, "sh", "-c", "touch /tmp/write-probe"],
        check=False,
    )
    if tmp_write.returncode != 0:
        raise GateFailure("bounded tmpfs did not accept a temporary write")

    status, body, headers = _http(base_url, "/api/health")
    if status != 200 or json.loads(body) != {"status": "ok", "ready": True}:
        raise GateFailure("hosted readiness endpoint contract failed")
    normalized_headers = {key.casefold(): value for key, value in headers.items()}
    if (
        normalized_headers.get("x-robots-tag")
        != "noindex, nofollow, noarchive"
    ):
        raise GateFailure("hosted noindex response header is absent")
    for hidden_path in ("/openapi.json", "/docs", "/api/runtime-health"):
        hidden_status, _, _ = _http(base_url, hidden_path)
        if hidden_status != 404:
            raise GateFailure(f"hosted endpoint remained visible: {hidden_path}")

    unsupported, _, _ = _http(
        base_url,
        "/api/chart",
        body=b"{}",
        content_type="text/plain",
    )
    oversized, _, _ = _http(
        base_url,
        "/api/chart",
        body=b" " * (16 * 1024 + 1),
        content_type="application/json",
    )
    malformed, _, _ = _http(
        base_url,
        "/api/chart",
        body=b'{"datetime":',
        content_type="application/json",
    )
    if (unsupported, oversized) != (415, 413) or malformed >= 500:
        raise GateFailure(
            f"request boundary mismatch: {(unsupported, oversized, malformed)}"
        )
    return {
        "uid": int(uid),
        "rootfs_write_rejected": True,
        "tmpfs_write_accepted": True,
        "health": "ready",
        "hidden_endpoints": [
            "/openapi.json",
            "/docs",
            "/api/runtime-health",
        ],
        "request_boundary_statuses": {
            "unsupported_media_type": unsupported,
            "oversized": oversized,
            "malformed": malformed,
        },
    }


def _verify_container_logs(logs: str) -> dict[str, Any]:
    for forbidden in (
        '"POST /api/chart HTTP/',
        "1997",
        "24.1477",
        "120.6736",
        CANARY,
    ):
        if forbidden in logs:
            raise GateFailure("container log disclosed request or canary data")

    privacy_events: list[dict[str, Any]] = []
    marker = "PRIVACY_EVENT "
    for line in logs.splitlines():
        if marker not in line:
            continue
        try:
            event = json.loads(line.split(marker, 1)[1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise GateFailure("container emitted malformed privacy telemetry") from exc
        if set(event) != EXPECTED_PRIVACY_EVENT_FIELDS:
            raise GateFailure("container privacy telemetry escaped its closed schema")
        privacy_events.append(event)
    if not any(event["route"] == "/api/chart" for event in privacy_events):
        raise GateFailure("container emitted no status-only chart telemetry")
    return {
        "privacy_event_count": len(privacy_events),
        "chart_status_only_event_present": True,
        "raw_chart_access_log_absent": True,
    }


def _single_worker_parity(
    image: str,
    payloads: list[dict[str, Any]],
    baselines: list[dict[str, Any]] | None,
    platform: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    container, base_url = _start_container(image, 1, platform)
    try:
        controls = _runtime_controls(container, base_url)
        container_results = []
        for case_index, payload in enumerate(payloads):
            try:
                result = _chart(base_url, payload)
                if baselines is not None:
                    _assert_parity(
                        baselines[case_index],
                        result,
                        parity_scope="cross_platform",
                    )
                container_results.append(result)
            except GateFailure as exc:
                raise GateFailure(
                    f"single-worker case {case_index}: {exc}"
                ) from exc
        logs = (
            _run(["docker", "logs", container], check=False).stdout
            + _run(["docker", "logs", container], check=False).stderr
        )
        log_controls = _verify_container_logs(logs)
        return {
            "calculation_cases": len(payloads),
            "parity_result": (
                "bounded_with_declared_cross_platform_exceptions"
                if baselines is not None
                else "Linux_container_baseline_established"
            ),
            "cross_platform_numeric_absolute_tolerance": (
                CROSS_PLATFORM_NUMERIC_ABSOLUTE_TOLERANCE
            ),
            "same_runtime_numeric_absolute_tolerance": (
                SAME_RUNTIME_NUMERIC_ABSOLUTE_TOLERANCE
            ),
            "numeric_relative_tolerance": 1e-12,
            "fixed_star_distance_speed_absolute_tolerance_au_per_day": 5e-3,
            "fixed_star_distance_speed_tolerance_scope": "cross_platform_only",
            "fixed_star_catalog_name_comparison": (
                "catalog name and its fixed-star trace title are exact after "
                "casefolding and removing whitespace only"
            ),
            "declared_runtime_specific_paths_excluded": sorted(
                RUNTIME_SPECIFIC_PARITY_PATHS
            ),
            "controls": controls,
            "log_controls": log_controls,
        }, container_results
    finally:
        _run(["docker", "rm", "--force", container], check=False)


def _assert_container_build_identity(
    image_revision: str | None,
    container_results: list[dict[str, Any]],
) -> None:
    if image_revision is None or not re.fullmatch(r"[0-9a-f]{40}", image_revision):
        raise GateFailure("image revision label is not a full lowercase Git revision")
    for case_index, result in enumerate(container_results):
        identity = result.get("calculation_dossier", {}).get("build_identity")
        if identity != {
            "status": "available",
            "source_revision": image_revision,
            "revision_source": (
                "build_environment:"
                "CLASSICAL_ASTROLOGY_SOURCE_REVISION"
            ),
        }:
            raise GateFailure(
                f"single-worker case {case_index}: Dossier build identity "
                "does not match the OCI image revision"
            )


def _two_worker_isolation(
    image: str,
    payloads: list[dict[str, Any]],
    baselines: list[dict[str, Any]],
    platform: str | None,
) -> dict[str, Any]:
    container, base_url = _start_container(image, 2, platform)
    try:
        process_table = _run(
            ["docker", "top", container, "-eo", "pid,ppid,args"]
        ).stdout
        worker_markers = sum(
            1 for line in process_table.splitlines() if "spawn_main" in line
        )
        if worker_markers < 2:
            raise GateFailure(
                f"did not observe two spawned worker processes: {process_table}"
            )

        jobs = [(index % len(payloads), payloads[index % len(payloads)]) for index in range(16)]
        # Stay within the application-owned per-worker capacity boundary; this
        # gate is proving mode isolation for accepted requests.
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_chart, base_url, payload): payload_index
                for payload_index, payload in jobs
            }
            completed = []
            for future in as_completed(futures):
                payload_index = futures[future]
                result = future.result()
                try:
                    _assert_parity(baselines[payload_index], result)
                except GateFailure as exc:
                    raise GateFailure(
                        f"two-worker case {payload_index}: {exc}"
                    ) from exc
                completed.append(payload_index)
        if len(completed) != len(jobs):
            raise GateFailure("not all concurrent requests completed")
        return {
            "observed_worker_processes": worker_markers,
            "concurrent_requests": len(completed),
            "request_concurrency": 4,
            "mode_variants": len(set(completed)),
            "parity_reference": "single-worker Linux container",
            "bounded_response_parity": True,
        }
    finally:
        _run(["docker", "rm", "--force", container], check=False)


def _worker_pids(container: str) -> list[int]:
    program = (
        "import json,os,pathlib;"
        "marker=bytes.fromhex('737061776e5f6d61696e');"
        "out=[];"
        "\nfor p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):"
        "\n pid=int(p.parent.name);"
        "\n if pid in (1,os.getpid()): continue"
        "\n try:"
        "\n  cmd=p.read_bytes();"
        "\n  state=(p.parent/'status').read_text().split('State:',1)[1]"
        ".lstrip()[:1]"
        "\n except (FileNotFoundError,PermissionError): continue"
        "\n if marker in cmd and state in 'RS': out.append(pid)"
        "\nprint(json.dumps(sorted(out)))"
    )
    output = _run(
        [
            "docker",
            "exec",
            container,
            "/opt/venv/bin/python",
            "-c",
            program,
        ]
    ).stdout
    return json.loads(output.strip())


def _wait_for_replacement(
    container: str, old_pid: int, *, timeout: float = 25
) -> tuple[list[int], float]:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        pids = _worker_pids(container)
        if len(pids) == 2 and old_pid not in pids:
            return pids, round(time.monotonic() - started, 3)
        time.sleep(0.25)
    raise GateFailure(
        f"worker {old_pid} was not replaced within {timeout} seconds"
    )


def _wait_for_stable_workers(
    container: str, *, stable_seconds: float = 2, timeout: float = 20
) -> list[int]:
    deadline = time.monotonic() + timeout
    candidate: list[int] | None = None
    candidate_since = time.monotonic()
    while time.monotonic() < deadline:
        current = _worker_pids(container)
        if len(current) == 2 and current == candidate:
            if time.monotonic() - candidate_since >= stable_seconds:
                return current
        else:
            candidate = current
            candidate_since = time.monotonic()
        time.sleep(0.25)
    raise GateFailure("two worker PIDs did not stabilize after replacement")


def _signal_worker(container: str, pid: int, signal_name: str) -> None:
    program = (
        "import os,pathlib,signal;"
        "marker=bytes.fromhex('737061776e5f6d61696e');"
        f"pid={pid};"
        "cmdline=pathlib.Path(f'/proc/{pid}/cmdline').read_bytes();"
        "assert pid != 1 and marker in cmdline;"
        f"os.kill(pid,signal.{signal_name})"
    )
    _run(
        [
            "docker",
            "exec",
            container,
            "/opt/venv/bin/python",
            "-c",
            program,
        ]
    )


def _process_resources(container: str, pids: list[int]) -> dict[str, Any]:
    program = (
        "import json,pathlib;"
        "pids=" + repr(pids) + ";"
        "out={};"
        "\nfor pid in pids:"
        "\n s=pathlib.Path(f'/proc/{pid}/status').read_text();"
        "\n fields={line.split(':',1)[0]:line.split(':',1)[1].strip() "
        "for line in s.splitlines() if ':' in line};"
        "\n out[str(pid)]={'rss_kib':int(fields['VmRSS'].split()[0]),"
        "'hwm_kib':int(fields['VmHWM'].split()[0]),"
        "'state':fields['State'].split()[0],"
        "'threads':int(fields['Threads']),"
        "'fds':len(list(pathlib.Path(f'/proc/{pid}/fd').iterdir()))};"
        "\nprint(json.dumps(out))"
    )
    output = _run(
        [
            "docker",
            "exec",
            container,
            "/opt/venv/bin/python",
            "-c",
            program,
        ]
    ).stdout
    return json.loads(output.strip())


def _warm_every_worker(
    container: str,
    base_url: str,
    pids: list[int],
    payloads: list[dict[str, Any]],
    baselines: list[dict[str, Any]],
) -> dict[str, Any]:
    requests = 0
    transient_503s = 0
    previous_shape: dict[str, tuple[int, int]] | None = None
    for round_index in range(10):
        jobs = [
            (index % len(payloads), payloads[index % len(payloads)])
            for index in range(8)
        ]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(
                    _chart_after_worker_recovery,
                    base_url,
                    payload,
                ): case
                for case, payload in jobs
            }
            for future in as_completed(futures):
                case = futures[future]
                result, request_503s = future.result()
                _assert_parity(baselines[case], result)
                transient_503s += request_503s
                requests += 1
        resources = _process_resources(container, pids)
        current_shape = {
            str(pid): (
                resources[str(pid)]["threads"],
                resources[str(pid)]["fds"],
            )
            for pid in pids
        }
        if (
            all(resources[str(pid)]["threads"] >= 2 for pid in pids)
            and current_shape == previous_shape
        ):
            return {
                "rounds": round_index + 1,
                "requests": requests,
                "each_worker_created_request_thread": True,
                "thread_and_fd_shape_stable_for_two_rounds": True,
                "stable_shape": current_shape,
                "bounded_transient_503s": transient_503s,
                "transient_503_retry_window_seconds": 10,
            }
        previous_shape = current_shape
    raise GateFailure(
        "could not prove that every replacement worker served a chart request "
        "and reached a stable thread/fd shape"
    )


def _resilience_and_soak(
    image: str,
    platform: str | None,
    payloads: list[dict[str, Any]],
    baselines: list[dict[str, Any]],
    soak_requests: int,
) -> dict[str, Any]:
    container, base_url = _start_container(image, 2, platform)
    try:
        original = _worker_pids(container)
        if len(original) < 2:
            raise GateFailure(f"expected two workers, observed {original}")

        killed = original[0]
        _signal_worker(container, killed, "SIGKILL")
        kill_replacement, kill_seconds = _wait_for_replacement(
            container, killed
        )
        kill_stable_pids = _wait_for_stable_workers(container)
        for index, payload in enumerate(payloads):
            try:
                _assert_parity(baselines[index], _chart(base_url, payload))
            except GateFailure as exc:
                raise GateFailure(
                    f"post-SIGKILL parity case {index}: {exc}"
                ) from exc

        stopped = kill_replacement[0]
        _signal_worker(container, stopped, "SIGSTOP")
        stop_replacement, stop_seconds = _wait_for_replacement(
            container, stopped
        )
        if stop_seconds < 3.5:
            raise GateFailure(
                "stopped worker was replaced before the configured "
                "worker-health timeout could be exercised"
            )
        status, body, _ = _http(base_url, "/api/health")
        if status != 200 or json.loads(body).get("ready") is not True:
            raise GateFailure("readiness did not recover after worker timeout")

        stable_pids = _wait_for_stable_workers(container)
        replacement_warmup = _warm_every_worker(
            container,
            base_url,
            stable_pids,
            payloads,
            baselines,
        )
        before = _process_resources(container, stable_pids)
        sampled_peak_rss = {
            str(pid): before[str(pid)]["rss_kib"] for pid in stable_pids
        }
        completed_requests = 0
        while completed_requests < soak_requests:
            batch_size = min(4, soak_requests - completed_requests)
            jobs = [
                (
                    request_index,
                    request_index % len(payloads),
                )
                for request_index in range(
                    completed_requests,
                    completed_requests + batch_size,
                )
            ]
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = {
                    executor.submit(_chart, base_url, payloads[case]): (
                        request_index,
                        case,
                    )
                    for request_index, case in jobs
                }
                for future in as_completed(futures):
                    request_index, case = futures[future]
                    try:
                        _assert_parity(baselines[case], future.result())
                    except GateFailure as exc:
                        raise GateFailure(
                            f"soak request {request_index}, case {case}: {exc}"
                        ) from exc
            completed_requests += batch_size
            if completed_requests % 25 == 0 or completed_requests == soak_requests:
                sample = _process_resources(container, stable_pids)
                for pid in stable_pids:
                    key = str(pid)
                    sampled_peak_rss[key] = max(
                        sampled_peak_rss[key],
                        sample[key]["rss_kib"],
                    )
        final_pids = _worker_pids(container)
        if final_pids != stable_pids:
            raise GateFailure(
                f"worker churned during soak: {stable_pids} -> {final_pids}"
            )
        after = _process_resources(container, final_pids)
        deltas = {}
        for pid in final_pids:
            key = str(pid)
            deltas[key] = {
                field: after[key][field] - before[key][field]
                for field in ("rss_kib", "hwm_kib", "threads", "fds")
            }
            deltas[key]["peak_rss_kib"] = sampled_peak_rss[key]
            if deltas[key]["rss_kib"] > 65536:
                raise GateFailure(
                    f"worker {pid} RSS grew by more than 64 MiB"
                )
            if deltas[key]["hwm_kib"] > 98304:
                raise GateFailure(
                    f"worker {pid} high-water RSS grew by more than 96 MiB"
                )
            if sampled_peak_rss[key] > 393216:
                raise GateFailure(
                    f"worker {pid} sampled RSS exceeded 384 MiB"
                )
            if deltas[key]["threads"] > 2 or deltas[key]["fds"] > 4:
                raise GateFailure(
                    f"worker {pid} resource growth exceeded bounded "
                    f"thresholds: {deltas[key]}"
                )
        return {
            "SIGKILL": {
                "target_pid": killed,
                "replacement_pids": kill_replacement,
                "stable_pids": kill_stable_pids,
                "replacement_seconds": kill_seconds,
                "post_recovery_parity": True,
            },
            "SIGSTOP": {
                "target_pid": stopped,
                "replacement_pids": stop_replacement,
                "stable_pids": stable_pids,
                "replacement_seconds": stop_seconds,
                "timeout_worker_healthcheck_seconds": 5,
                "post_recovery_readiness": True,
                "replacement_warmup": replacement_warmup,
            },
            "soak": {
                "requests": soak_requests,
                "worker_pids_stable": True,
                "before": before,
                "after": after,
                "deltas": deltas,
                "thresholds": {
                    "rss_growth_kib_per_worker": 65536,
                    "hwm_growth_kib_per_worker": 98304,
                    "peak_rss_kib_per_worker": 393216,
                    "threads_growth_per_worker": 2,
                    "fds_growth_per_worker": 4,
                },
                "request_concurrency": 4,
                "sample_every_requests": 25,
            },
            "scope_limit": (
                "Uvicorn supervisor health timeout only; this does not "
                "cancel an in-flight native calculation or replace proxy "
                "request timeouts."
            ),
        }
    finally:
        _run(["docker", "rm", "--force", container], check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--build", action="store_true")
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="fail before a release rehearsal build if Git has any changes",
    )
    parser.add_argument(
        "--platform",
        help="Docker target platform, for example linux/amd64",
    )
    parser.add_argument(
        "--container-only",
        action="store_true",
        help="establish Linux parity without importing the local backend",
    )
    parser.add_argument(
        "--worker-resilience",
        action="store_true",
        help="exercise worker kill, health timeout, recovery and soak",
    )
    parser.add_argument(
        "--soak-requests",
        type=int,
        default=1000,
        help="sequential mixed-mode requests for the bounded soak",
    )
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="emit only a closed summary; retain full JSON in --receipt",
    )
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="check committed response parity without Docker",
    )
    args = parser.parse_args()
    if args.require_clean and not args.build:
        parser.error("--require-clean requires --build")
    if args.baseline_only:
        try:
            result = verify_committed_parity_baseline()
        except (GateFailure, OSError, ValueError) as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        print(
            "PARITY BASELINE COMPATIBLE "
            f"cases={result['cases']} "
            f"schema={result['response_schema_version']}"
        )
        return 0

    started = time.monotonic()
    try:
        build = (
            _build_image(
                args.image,
                require_clean=args.require_clean,
                platform=args.platform,
            )
            if args.build
            else {
                "image": args.image,
                "revision": "prebuilt",
                "clean_checkout": None,
                "clean_checkout_required": False,
                "platform": args.platform,
            }
        )
        image = _inspect_image(args.image, args.platform)
        inventory = _inventory_image(args.image, args.platform)
        payloads = _payloads()
        baselines = (
            _load_committed_cross_platform_baseline(payloads)
            if args.container_only
            else _local_baselines(payloads)
        )
        single_worker, container_baselines = _single_worker_parity(
            args.image,
            payloads,
            baselines,
            args.platform,
        )
        _assert_container_build_identity(
            image["revision"],
            container_baselines,
        )
        two_worker = _two_worker_isolation(
            args.image,
            payloads,
            container_baselines,
            args.platform,
        )
        resilience = (
            _resilience_and_soak(
                args.image,
                args.platform,
                payloads,
                container_baselines,
                args.soak_requests,
            )
            if args.worker_resilience
            else None
        )
        receipt = {
            "schema_version": "private-alpha-container-gate-v2",
            "build": build,
            "image": image,
            "inventory": inventory,
            "single_worker": single_worker,
            "two_worker": two_worker,
            "worker_resilience": resilience,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        if args.receipt is not None:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if args.quiet:
            print(
                "OK: production container gate passed "
                f"({len(payloads)} modes, architecture="
                f"{image['architecture']})"
            )
        else:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except (GateFailure, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
