#!/usr/bin/env python3
"""Build and exercise the provider-neutral Private Alpha production image."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import platform as platform_module
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
    materialize_context,
)
from scripts.verification.container_platform_contract import (
    platform_args as _platform_args,
    platform_contract,
)
from scripts.deployment.frontend_release import (
    build_release as build_frontend_release,
    combined_release_id,
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
        ),
        # The same IANA release is discovered through OS-specific filesystem
        # layouts.  Compare the version and availability receipt, but not the
        # resolver label (for example macOS +VERSION vs Linux tzdata.zi).
        "$.library_info.tz_database.source",
        "$.calculation_dossier.engine.tz_database.source",
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
    materialize_context(
        PROJECT_ROOT,
        context,
        control_files={
            Path("build-context-probe-8f38f069.txt"): (CANARY + "\n").encode(
                "utf-8"
            )
        },
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
        # Without these, "reproducible" is a claim nobody can falsify later.
        # This is also where the receipt says out loud that it came from a
        # developer workstation, so a reader does not assume a hermetic CI
        # build produced it.
        "builder": _builder_environment(),
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


# Docker writes these per container, so they differ between two runs of the
# same image and must not enter the content identity.
DOCKER_INJECTED_PATHS = frozenset({"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"})

APP_TREE_MANIFEST = PROJECT_ROOT / "deploy" / "image-app-tree.json"
COMPILED_ARTIFACTS_MANIFEST = PROJECT_ROOT / "deploy" / "image-compiled-artifacts.json"


# Only the extensions installed into our own virtualenv. Sweeping every `.so`
# in the image would pull in the whole Debian base — hundreds of libraries we
# do not build, whose hashes move with the pinned base rather than with our
# toolchain, which is the thing this pin is meant to watch.
COMPILED_ARTIFACT_ROOT = "/opt/venv/"


def _is_compiled_artifact(path: str) -> bool:
    return path.startswith(COMPILED_ARTIFACT_ROOT) and (
        path.endswith(".so") or ".so." in path.rsplit("/", 1)[-1]
    )


def _review_required(manifest: Path, observed: Any, what: str) -> None:
    """Fail closed when there is nothing committed to compare against.

    Writing the observed value automatically would make the check agree with
    whatever the build produced, which is the failure mode this whole gate
    exists to prevent.  So the first build after a change fails, prints what
    it saw, and asks a human to review and commit it.
    """

    raise GateFailure(
        f"no committed {what} to compare against: {manifest}.\n"
        "Review the observed value below, and commit it to that path only if "
        "every entry is intended:\n"
        + json.dumps(observed, indent=2, sort_keys=True)
    )


def _assert_app_tree_is_expected(observed: list[str]) -> None:
    """Compare the whole `/app` tree against a committed allowlist.

    The previous control was a denylist of forbidden path fragments, and it
    has already failed once: a stray `/app/frontend/README.md` shipped in a
    ratified image because the list caught `tests/*` and not `README.md`
    (`DEP-ART-E-003`).  A denylist can only exclude what someone thought of.
    The `/app` tree is small and fully determined by the Dockerfile, so it can
    be stated positively; the rest of the filesystem keeps the denylist.
    """

    if not APP_TREE_MANIFEST.is_file():
        _review_required(APP_TREE_MANIFEST, observed, "/app tree manifest")
    expected = json.loads(APP_TREE_MANIFEST.read_text(encoding="utf-8"))
    expected_paths = sorted(expected["paths"])
    if observed != expected_paths:
        unexpected = sorted(set(observed) - set(expected_paths))
        missing = sorted(set(expected_paths) - set(observed))
        raise GateFailure(
            "image /app tree does not match the committed manifest: "
            f"unexpected={unexpected}, missing={missing}"
        )


def _assert_compiled_artifacts_are_expected(
    observed: dict[str, str], architecture: str
) -> None:
    """Pin the hashes of the natively compiled extensions, per architecture.

    The 2026-07-30 rebuild investigation refuted the concern that the unpinned
    `build-essential` in the builder stage could change numeric output, by
    showing `swisseph...so` was byte-identical across two builders.  That
    refutation expires the moment the toolchain moves, and nobody would think
    to repeat it.  Recording the hashes turns a future drift into a red light
    instead of a future investigation.

    Keyed by architecture because these are compiled objects: the amd64 and
    arm64 builds of the same source produce different bytes by definition, and
    a single flat mapping would make one platform permanently red.
    """

    if not COMPILED_ARTIFACTS_MANIFEST.is_file():
        _review_required(
            COMPILED_ARTIFACTS_MANIFEST,
            {"artifacts": {architecture: observed}},
            "compiled artifact manifest",
        )
    expected = json.loads(COMPILED_ARTIFACTS_MANIFEST.read_text(encoding="utf-8"))
    by_architecture = expected["artifacts"]
    if architecture not in by_architecture:
        _review_required(
            COMPILED_ARTIFACTS_MANIFEST,
            {"artifacts": {**by_architecture, architecture: observed}},
            f"compiled artifact manifest entry for {architecture}",
        )
    recorded = by_architecture[architecture]
    drifted = {
        path: {"expected": recorded.get(path), "observed": digest}
        for path, digest in observed.items()
        if recorded.get(path) != digest
    }
    absent = sorted(set(recorded) - set(observed))
    if drifted or absent:
        raise GateFailure(
            "compiled artifact drift: the native extensions differ from the "
            f"recorded build. drifted={drifted}, absent={absent}"
        )


def _builder_environment() -> dict[str, Any]:
    def _first_line(command: list[str]) -> str | None:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=60
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else None

    return {
        "trust_level": "developer_workstation_not_hermetic_ci",
        "docker_version": _first_line(
            ["docker", "version", "--format", "{{.Server.Version}}"]
        ),
        "buildx_version": _first_line(["docker", "buildx", "version"]),
        "host_platform": f"{platform_module.system()}/{platform_module.machine()}",
        "base_image_reference": _dockerfile_base_reference(),
    }


def _dockerfile_base_reference() -> str | None:
    """The pinned base as written in the Dockerfile, digest included."""

    dockerfile = PROJECT_ROOT / "deploy" / "Dockerfile"
    if not dockerfile.is_file():
        return None
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FROM "):
            return stripped[5:].split(" AS ")[0].strip()
    return None


def _image_architecture(image: str, platform: str | None) -> str:
    inspected = json.loads(
        _run(
            ["docker", "image", "inspect", *_platform_args(platform), image]
        ).stdout
    )
    return str(inspected[0]["Architecture"])


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
            app_entries: list[str] = []
            content_identity_rows: list[str] = []
            compiled_artifacts: dict[str, str] = {}
            canary_found = False
            with tarfile.open(archive_path, mode="r") as archive:
                for member in archive:
                    normalized = "/" + member.name.lstrip("./")
                    names.append(normalized)
                    if normalized.startswith("/app/"):
                        app_entries.append(normalized)
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
                        or lowered == "/app/frontend"
                        or lowered.startswith("/app/frontend/zh-TW/")
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
                    payload_digest = ""
                    if member.isfile():
                        extracted = archive.extractfile(member)
                        if extracted is not None:
                            digest = hashlib.sha256()
                            scanned = b""
                            while True:
                                chunk = extracted.read(1024 * 1024)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                if len(scanned) < 8 * 1024 * 1024:
                                    scanned += chunk
                            payload_digest = digest.hexdigest()
                            if CANARY.encode() in scanned:
                                canary_found = True
                            if _is_compiled_artifact(normalized):
                                compiled_artifacts[normalized] = payload_digest
                    if normalized not in DOCKER_INJECTED_PATHS:
                        content_identity_rows.append(
                            f"{normalized}\t{member.mode:04o}\t{member.uid}"
                            f"\t{member.gid}\t{member.type.decode('ascii')}"
                            f"\t{payload_digest}"
                        )
            if canary_found:
                raise GateFailure("image filesystem contains the secret canary")
            content_identity = hashlib.sha256(
                "\n".join(sorted(content_identity_rows)).encode("utf-8")
            ).hexdigest()
            _assert_app_tree_is_expected(sorted(app_entries))
            _assert_compiled_artifacts_are_expected(
                compiled_artifacts, _image_architecture(image, platform)
            )
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
        "app_tree_entries": len(app_entries),
        # A digest over (path, mode, uid, gid, type, content) for every entry
        # except the ones Docker injects per container.  The image digest is
        # not a stable identity — a 2026-07-30 rebuild of the same source
        # produced a different digest whose only real difference was
        # filesystem metadata from `useradd` and `chmod`, which forced an
        # amendment to a ratified digest.  This value does not move for that
        # reason, so it is the thing worth ratifying; the digest stays as the
        # deployment pointer.
        "content_identity": content_identity,
        "compiled_artifacts": dict(sorted(compiled_artifacts.items())),
        "production_packages": sorted(packages),
        "forbidden_packages_present": sorted(forbidden),
        "secret_canary_present": False,
        "runtime": runtime,
    }


# The birth date, time, timezone, altitude and coordinates below are fixture
# values composed arbitrarily when this parity fixture was created.  They were
# not taken from anyone's records.  An independent audit
# (`IMG-2026-08-08-E-005`) could not tell that from the repository, because
# nothing said so anywhere the file travels — and the baseline is published as
# part of the Corresponding Source, so it travels alone.  Hence
# PAYLOAD_PROVENANCE, which is serialized into the baseline itself rather than
# stated only here or in a document.
#
# Note what the statement does not say.  It does not claim these values fail to
# match some real person; nobody can show that, and a claim that cannot be
# supported is worse than none.  It claims only that no record was consulted.
PAYLOAD_PROVENANCE: dict[str, Any] = {
    "synthetic": True,
    "statement": (
        "The birth datetime, timezone, altitude and coordinates in these "
        "payloads are fixture values composed arbitrarily when the parity "
        "fixture was created. They were not derived from, copied from, or "
        "used to describe any real person's records, and no such record was "
        "consulted. No claim is made that the values differ from every real "
        "individual's birth data; the claim is only about their origin."
    ),
    "attested_by": "Sebastian",
    "attested_on": "2026-08-07",
    "finding": "IMG-2026-08-08-E-005",
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
            f"{sorted(schema_versions, key=str)}"
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
                and isinstance(expected, str)
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
                if not isinstance(expected, str) or not isinstance(actual, str):
                    raise GateFailure(
                        f"catalog name at {path} is not a string on both sides"
                    )
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
) -> tuple[str, str, Path]:
    container = f"private-alpha-{workers}w-{uuid.uuid4().hex[:10]}"
    image_identity = _inspect_image(image, platform)
    revision = image_identity.get("revision")
    image_id = image_identity.get("id")
    if not isinstance(revision, str) or not isinstance(image_id, str):
        raise GateFailure("image identity is unavailable for frontend release")
    temporary = Path(tempfile.mkdtemp(prefix="container-frontend-release-"))
    built = build_frontend_release(
        source_root=PROJECT_ROOT,
        output_parent=temporary,
        public_source_revision=revision,
        require_clean_revision=False,
    )
    frontend_revision = str(built["frontend_public_source_revision"])
    frontend_digest = str(built["artifact_digest"])
    combined = combined_release_id(
        backend_image_id=image_id,
        backend_public_source_revision=revision,
        frontend_artifact_digest=frontend_digest,
        frontend_public_source_revision=frontend_revision,
    )
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
        "--mount",
        (
            "type=bind,src=" + str(built["release_directory"])
            + ",dst=/app/frontend,readonly"
        ),
        "--env",
        "CLASSICAL_ASTROLOGY_REQUIRE_FRONTEND_RELEASE=1",
        "--env",
        "CLASSICAL_ASTROLOGY_FRONTEND_ROOT=/app/frontend",
        "--env",
        f"CLASSICAL_ASTROLOGY_FRONTEND_RELEASE_DIGEST={frontend_digest}",
        "--env",
        f"CLASSICAL_ASTROLOGY_BACKEND_IMAGE_ID={image_id}",
        "--env",
        f"CLASSICAL_ASTROLOGY_COMBINED_RELEASE_ID={combined}",
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
        return container, f"http://127.0.0.1:{port}", temporary
    except Exception:
        _run(["docker", "rm", "--force", container], check=False)
        shutil.rmtree(temporary, ignore_errors=True)
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
    container, base_url, frontend_temporary = _start_container(
        image, 1, platform
    )
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
        shutil.rmtree(frontend_temporary, ignore_errors=True)


def _assert_container_build_identity(
    image_revision: str | None,
    container_results: list[dict[str, Any]],
) -> None:
    if image_revision is None or not re.fullmatch(r"[0-9a-f]{40}", image_revision):
        raise GateFailure("image revision label is not a full lowercase Git revision")
    for case_index, result in enumerate(container_results):
        identity = result.get("calculation_dossier", {}).get("build_identity")
        release_identity = (
            identity.get("release_identity")
            if isinstance(identity, dict)
            else None
        )
        if (
            not isinstance(identity, dict)
            or {
                key: identity.get(key)
                for key in ("status", "source_revision", "revision_source")
            }
            != {
                "status": "available",
                "source_revision": image_revision,
                "revision_source": (
                    "build_environment:"
                    "CLASSICAL_ASTROLOGY_SOURCE_REVISION"
                ),
            }
            or not isinstance(release_identity, dict)
            or release_identity.get("status") != "available"
            or release_identity.get("backend", {}).get(
                "public_source_revision"
            ) != image_revision
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(release_identity.get("combined_release_id", "")),
            )
        ):
            raise GateFailure(
                f"single-worker case {case_index}: Dossier build identity "
                "does not match the OCI and mounted frontend release"
            )


def _two_worker_isolation(
    image: str,
    payloads: list[dict[str, Any]],
    baselines: list[dict[str, Any]],
    platform: str | None,
) -> dict[str, Any]:
    container, base_url, frontend_temporary = _start_container(
        image, 2, platform
    )
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
        shutil.rmtree(frontend_temporary, ignore_errors=True)


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
    # `fds` counts every entry in /proc/<pid>/fd, and most of them are the
    # client sockets the server is serving with at that instant, so comparing
    # two such counts measures when the snapshots were taken. Splitting sockets
    # out is still right; what was written here before was the conclusion drawn
    # next, and it was wrong.
    #
    # This comment used to say that a direct probe held steady at 22 fds across
    # 240 requests while the gate reported a delta of three, and therefore that
    # the delta was noise. The gate was correct. Swiss Ephemeris opens three
    # files per worker thread and does not return them when the thread exits,
    # so the count tracks how many threads have ever been created — and three
    # was not measurement error, it was the three ephemeris files. A 240-request
    # probe almost never contains a thread creation, so it could not have
    # contained the phenomenon it was used to rule out.
    #
    # The non-socket count is therefore a real leak signal, not a settled one:
    # it does not return to steady state after warm-up. See
    # IMG-2026-08-08-E-001, scripts/verification/probe_fd_saturation.py and
    # POSTMORTEM_6A section 4.6.
    program = "\n".join(
        (
            "import json, os, pathlib",
            f"pids = {pids!r}",
            "out = {}",
            "for pid in pids:",
            "    status = pathlib.Path(f'/proc/{pid}/status').read_text()",
            "    fields = {",
            "        line.split(':', 1)[0]: line.split(':', 1)[1].strip()",
            "        for line in status.splitlines() if ':' in line",
            "    }",
            "    targets = []",
            "    for entry in pathlib.Path(f'/proc/{pid}/fd').iterdir():",
            "        try:",
            "            targets.append(os.readlink(entry))",
            "        except OSError:",
            "            pass",
            "    sockets = sum(1 for t in targets if t.startswith('socket:'))",
            "    out[str(pid)] = {",
            "        'rss_kib': int(fields['VmRSS'].split()[0]),",
            "        'hwm_kib': int(fields['VmHWM'].split()[0]),",
            "        'state': fields['State'].split()[0],",
            "        'threads': int(fields['Threads']),",
            "        'fds': len(targets),",
            "        'fds_sockets': sockets,",
            "        'fds_files': len(targets) - sockets,",
            "    }",
            "print(json.dumps(out))",
        )
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


def _soak_pass(
    *,
    container: str,
    base_url: str,
    payloads: list[dict[str, Any]],
    baselines: list[dict[str, Any]],
    stable_pids: list[int],
    soak_requests: int,
    strict_handle_growth: bool,
) -> dict[str, Any]:
    """Run one soak and measure it.

    `strict_handle_growth` separates the two roles. The warm-up pass still has
    to hold the memory bounds — a runaway allocation is a defect whichever pass
    it happens in — but only the measured pass may be judged on descriptors and
    threads, because only it starts from a process that has already opened
    everything it opens lazily.
    """

    before = _process_resources(container, stable_pids)
    sampled_peak_rss = {
        str(pid): before[str(pid)]["rss_kib"] for pid in stable_pids
    }
    completed_requests = 0
    while completed_requests < soak_requests:
        batch_size = min(4, soak_requests - completed_requests)
        jobs = [
            (request_index, request_index % len(payloads))
            for request_index in range(
                completed_requests, completed_requests + batch_size
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
                    sampled_peak_rss[key], sample[key]["rss_kib"]
                )
    final_pids = _worker_pids(container)
    if final_pids != stable_pids:
        raise GateFailure(
            f"worker churned during soak: {stable_pids} -> {final_pids}"
        )
    after = _process_resources(container, final_pids)
    deltas: dict[str, Any] = {}
    for pid in final_pids:
        key = str(pid)
        deltas[key] = {
            field: after[key][field] - before[key][field]
            for field in (
                "rss_kib",
                "hwm_kib",
                "threads",
                "fds",
                "fds_sockets",
                "fds_files",
            )
        }
        deltas[key]["peak_rss_kib"] = sampled_peak_rss[key]
        if deltas[key]["rss_kib"] > 65536:
            raise GateFailure(f"worker {pid} RSS grew by more than 64 MiB")
        if deltas[key]["hwm_kib"] > 98304:
            raise GateFailure(
                f"worker {pid} high-water RSS grew by more than 96 MiB"
            )
        if sampled_peak_rss[key] > 393216:
            raise GateFailure(f"worker {pid} sampled RSS exceeded 384 MiB")
        if strict_handle_growth and (
            deltas[key]["threads"] > 1 or deltas[key]["fds_files"] > 1
        ):
            raise GateFailure(
                f"worker {pid} leaked handles on the warm soak pass: "
                f"{deltas[key]}. The first pass already opened every lazily "
                "opened file, so non-socket growth here is leakage, not "
                "warm-up. Sockets are excluded deliberately: they are the "
                "connections being served at the instant of the snapshot."
            )
    return {
        "before": before,
        "after": after,
        "deltas": deltas,
        "final_pids": final_pids,
        "strict_handle_growth": strict_handle_growth,
    }


def _resilience_and_soak(
    image: str,
    platform: str | None,
    payloads: list[dict[str, Any]],
    baselines: list[dict[str, Any]],
    soak_requests: int,
) -> dict[str, Any]:
    container, base_url, frontend_temporary = _start_container(
        image, 2, platform
    )
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
        first_pass = _soak_pass(
            container=container,
            base_url=base_url,
            payloads=payloads,
            baselines=baselines,
            stable_pids=stable_pids,
            soak_requests=soak_requests,
            strict_handle_growth=False,
        )
        # Sebastian ruled option C on 2026-08-07. One soak measured file
        # descriptors that had never been opened rather than descriptors that
        # leaked: Swiss Ephemeris opens its data files lazily, and which worker
        # opens which file between two snapshots is decided by the four
        # rotating modes, not by load. Measured, it did not scale — 250
        # requests gave +6, 500 gave +3 and +3, 750 gave +6, on native arm64 as
        # well as emulated amd64.
        #
        # Raising the threshold would have bought silence at the cost of
        # resolution: a genuine leak of five per soak would then pass forever,
        # and this repository has already watched one gate become ignorable.
        # A second pass costs a minute and removes the ambiguity instead of
        # tolerating it — it runs against an already-warm process, so its
        # growth is leakage and can be held to nearly zero.
        second_pass = _soak_pass(
            container=container,
            base_url=base_url,
            payloads=payloads,
            baselines=baselines,
            stable_pids=first_pass["final_pids"],
            soak_requests=soak_requests,
            strict_handle_growth=True,
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
                "requests_per_pass": soak_requests,
                "passes": 2,
                "semantics": (
                    "pass one absorbs lazily opened files; pass two runs warm, "
                    "so its handle growth is leakage and is held near zero"
                ),
                "worker_pids_stable": True,
                "warm_up_pass": first_pass,
                "measured_pass": second_pass,
                "thresholds": {
                    "rss_growth_kib_per_worker": 65536,
                    "hwm_growth_kib_per_worker": 98304,
                    "peak_rss_kib_per_worker": 393216,
                    "threads_growth_per_worker_measured_pass": 1,
                    "fds_files_growth_per_worker_measured_pass": 1,
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
        shutil.rmtree(frontend_temporary, ignore_errors=True)


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
