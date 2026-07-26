from __future__ import annotations

import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import re

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = PROJECT_ROOT / "deploy"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
COMPOSE = DEPLOY_DIR / "compose.yaml"
LOCK = DEPLOY_DIR / "requirements.lock"
LOCK_INPUT = DEPLOY_DIR / "requirements.in"
BUILD_LOCK = DEPLOY_DIR / "build-requirements.lock"
EPHEMERIS_MANIFEST = DEPLOY_DIR / "ephemeris.sha256"
ENTRYPOINT = DEPLOY_DIR / "entrypoint.sh"
HEALTHCHECK = DEPLOY_DIR / "container_healthcheck.py"
SOURCE_BUILD_VERIFIER = PROJECT_ROOT / "scripts" / "verify_linux_source_build.py"
EPHEMERIS_VERIFIER = PROJECT_ROOT / "scripts" / "verify_ephemeris_integrity.py"
CONTAINER_GATE = PROJECT_ROOT / "scripts" / "verify_container_runtime.py"
SUPPLY_CHAIN_GATE = PROJECT_ROOT / "scripts" / "verify_image_supply_chain.py"
AMD64_CI = PROJECT_ROOT / ".github" / "workflows" / "production-amd64.yml"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
PARITY_BASELINE = DEPLOY_DIR / "parity-baseline-arm64.json"
PRIVACY_DEPENDENCY_GATE = (
    PROJECT_ROOT / "scripts" / "verify_privacy_dependencies.py"
)
DELIVERY_GATE = PROJECT_ROOT / "scripts" / "verify_delivery.py"

PYTHON_IMAGE = (
    "python:3.13.14-slim-trixie"
    "@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
)


@pytest.mark.parametrize(
    "path",
    (
        DOCKERFILE,
        COMPOSE,
        LOCK,
        LOCK_INPUT,
        BUILD_LOCK,
        EPHEMERIS_MANIFEST,
        ENTRYPOINT,
        HEALTHCHECK,
        SOURCE_BUILD_VERIFIER,
        EPHEMERIS_VERIFIER,
        CONTAINER_GATE,
        SUPPLY_CHAIN_GATE,
        AMD64_CI,
        DOCKERIGNORE,
        PARITY_BASELINE,
    ),
)
def test_production_runtime_artifacts_exist(path):
    assert path.is_file(), path


def _logical_requirements(text: str) -> list[str]:
    requirements = []
    pending = ""
    for raw_line in text.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if not line:
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].strip()
            continue
        requirements.append(pending)
        pending = ""
    assert not pending
    return requirements


def test_production_lock_is_complete_hashed_and_excludes_dev_dependencies():
    requirements = _logical_requirements(LOCK.read_text(encoding="utf-8"))
    package_requirements = [
        requirement
        for requirement in requirements
        if not requirement.startswith("--")
    ]
    names = {
        re.match(r"([A-Za-z0-9_.-]+)", requirement).group(1).lower()
        for requirement in package_requirements
    }

    assert {"fastapi", "uvicorn", "pyswisseph"} <= names
    assert {
        "pytest",
        "httpx2",
        "pip-tools",
        "httptools",
        "python-dotenv",
        "pyyaml",
        "uvloop",
        "watchfiles",
        "websockets",
    }.isdisjoint(names)
    assert any(
        requirement.startswith("pyswisseph==2.10.3.2 ")
        for requirement in package_requirements
    )
    for requirement in package_requirements:
        assert "==" in requirement, requirement
        assert "--hash=sha256:" in requirement, requirement


def test_production_lock_input_is_minimal_and_auditable():
    requirements = {
        line.strip()
        for line in LOCK_INPUT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert requirements == {
        "fastapi==0.139.2",
        "uvicorn==0.51.0",
        "pyswisseph==2.10.3.2",
    }


def test_dockerfile_pins_python_builds_pyswisseph_from_source_and_is_non_root():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert dockerfile.count(f"FROM {PYTHON_IMAGE}") == 2
    assert f"FROM {PYTHON_IMAGE} AS builder" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--no-build-isolation" in dockerfile
    assert "build-requirements.lock" in dockerfile
    assert "--no-binary=pyswisseph" in dockerfile
    assert "mount=type=secret,id=private_alpha_probe,required=true" in dockerfile
    assert "buildkit-probe-consumed.json" in dockerfile
    assert "verify_linux_source_build.py" in dockerfile
    assert "pip uninstall --yes pip" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'CLASSICAL_ASTROLOGY_PROFILE="private_alpha"' in dockerfile
    assert "--no-access-log" in dockerfile
    assert '"--workers", "2"' in dockerfile
    assert '"--loop", "asyncio"' in dockerfile
    assert '"--http", "h11"' in dockerfile
    assert '"--ws", "none"' in dockerfile
    assert '"--timeout-worker-healthcheck", "5"' in dockerfile
    assert "slim-bookworm" not in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "container_healthcheck.py" in dockerfile


def test_compose_enforces_read_only_tmpfs_and_no_direct_host_publication():
    compose = COMPOSE.read_text(encoding="utf-8")

    for required in (
        "read_only: true",
        "tmpfs:",
        "/tmp:",
        "cap_drop:",
        '- "ALL"',
        "no-new-privileges:true",
        "pids_limit:",
        "mem_limit:",
        "memswap_limit: 512m",
        "cpus:",
        'driver: "local"',
        'max-size: "5m"',
        'max-file: "2"',
        "internal: true",
    ):
        assert required in compose
    assert re.search(r"^\s+ports\s*:", compose, re.MULTILINE) is None
    assert re.search(r"^\s+expose\s*:", compose, re.MULTILINE) is None


def test_ephemeris_manifest_matches_every_runtime_input():
    entries = {}
    for line in EPHEMERIS_MANIFEST.read_text(encoding="utf-8").splitlines():
        digest, relative_path = line.split(maxsplit=1)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        entries[relative_path] = digest

    expected_paths = {
        "backend/ephe/sepl_18.se1",
        "backend/ephe/semo_18.se1",
        "backend/ephe/seas_18.se1",
        "backend/ephe/sefstars.txt",
        "backend/ephe/seorbel.txt",
    }
    assert set(entries) == expected_paths
    for relative_path, expected_digest in entries.items():
        actual_digest = hashlib.sha256(
            (PROJECT_ROOT / relative_path).read_bytes()
        ).hexdigest()
        assert actual_digest == expected_digest


def test_ephemeris_verifier_rejects_one_byte_equivalent_digest_mismatch(
    tmp_path,
):
    spec = importlib.util.spec_from_file_location(
        "verify_ephemeris_integrity",
        EPHEMERIS_VERIFIER,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    lines = EPHEMERIS_MANIFEST.read_text(encoding="utf-8").splitlines()
    original_digest, relative_path = lines[0].split(maxsplit=1)
    replacement_digest = (
        ("0" if original_digest[0] != "0" else "1") + original_digest[1:]
    )
    lines[0] = f"{replacement_digest}  {relative_path}"
    tampered_manifest = tmp_path / "ephemeris.sha256"
    tampered_manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    errors = module.verify(tampered_manifest)

    assert errors == [f"digest mismatch: {relative_path}"]


def test_ephemeris_verifier_rejects_an_extra_runtime_input(
    tmp_path,
    monkeypatch,
):
    spec = importlib.util.spec_from_file_location(
        "verify_ephemeris_integrity_extra",
        EPHEMERIS_VERIFIER,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)

    for relative_path in module.EXPECTED_PATHS:
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"known ephemeris input")
    extra = tmp_path / "backend/ephe/unexpected.se1"
    extra.write_bytes(b"unexpected")
    manifest = tmp_path / "ephemeris.sha256"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256(b'known ephemeris input').hexdigest()}  {path}\n"
            for path in sorted(module.EXPECTED_PATHS)
        ),
        encoding="utf-8",
    )

    assert module.verify(manifest) == [
        "unexpected ephemeris input: backend/ephe/unexpected.se1"
    ]


def test_entrypoint_verifies_ephemeris_before_exec_without_echoing_environment():
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")

    assert "verify_ephemeris_integrity.py" in entrypoint
    assert "--check" in entrypoint
    assert 'exec "$@"' in entrypoint
    assert re.search(
        r"(^|\s)(env|printenv)(\s|$)",
        entrypoint,
        re.MULTILINE,
    ) is None
    assert "set -x" not in entrypoint


def test_dockerignore_excludes_local_build_review_and_secret_material():
    ignored = DOCKERIGNORE.read_text(encoding="utf-8")

    for pattern in (
        ".git",
        "backend/.venv",
        ".build",
        "dist",
        "docs/red_team",
        "docs/archive",
        ".env",
        "*.pem",
        "*secret*",
        "*token*",
        "**/.DS_Store",
        "frontend/tests",
    ):
        assert pattern in ignored


def test_build_context_canary_reaches_dockerfile_boundary_before_copy_filter():
    gate = CONTAINER_GATE.read_text(encoding="utf-8")

    assert "build-context-probe-8f38f069.txt" in gate
    assert ".env.private-alpha-canary" not in gate
    assert "runtime-secret-canary.txt" not in gate


def test_container_release_gate_can_fail_closed_on_a_dirty_checkout():
    gate = CONTAINER_GATE.read_text(encoding="utf-8")

    assert "--require-clean" in gate
    assert "status" in gate
    assert "--porcelain" in gate


def test_container_gate_covers_native_amd64_and_worker_resilience():
    gate = CONTAINER_GATE.read_text(encoding="utf-8")

    for required in (
        "--platform",
        "linux/amd64",
        "--container-only",
        "--worker-resilience",
        "--soak-requests",
        "SIGKILL",
        "SIGSTOP",
        "timeout-worker-healthcheck",
        "platform.machine()",
        "e_machine",
        "parity-baseline-arm64.json",
        "VmHWM",
        "peak_rss_kib",
        "thread_and_fd_shape_stable_for_two_rounds",
        "bounded_transient_503s",
        '"request_concurrency": 4',
    ):
        assert required in gate
    assert "CROSS_PLATFORM_FIXED_STAR_SPEED_DISTANCE_TOLERANCE = 5e-3" in gate
    assert "SAME_RUNTIME_FIXED_STAR_SPEED_DISTANCE_TOLERANCE = 1e-8" in gate
    assert "CROSS_PLATFORM_NUMERIC_ABSOLUTE_TOLERANCE = 2e-8" in gate
    assert "SAME_RUNTIME_NUMERIC_ABSOLUTE_TOLERANCE = 1e-8" in gate


def test_container_platform_parser_preserves_arm64_variant():
    spec = importlib.util.spec_from_file_location(
        "verify_container_runtime_platform",
        CONTAINER_GATE,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module._platform_contract("linux/amd64") == ("linux", "amd64")
    assert module._platform_contract("linux/arm64/v8") == ("linux", "arm64")
    with pytest.raises(module.GateFailure):
        module._platform_contract("linux/riscv64")


def test_supply_chain_gate_records_sbom_scanner_db_and_digest_evidence():
    gate = SUPPLY_CHAIN_GATE.read_text(encoding="utf-8")

    for required in (
        "syft-json",
        "spdx-json",
        "grype",
        "database_built",
        "sha256",
        "manual_triage_required",
        "fix_available",
        "MAX_GRYPE_DB_AGE_HOURS = 72",
        "private-alpha-supply-chain-summary-v1",
        "--require-hashes",
        "--disable-pip",
        "--no-deps",
        '"db", "update"',
    ):
        assert required in gate
    assert "--pip-audit-json" not in gate


def test_supply_chain_gate_rejects_database_older_than_72_hours():
    spec = importlib.util.spec_from_file_location(
        "verify_image_supply_chain_age",
        SUPPLY_CHAIN_GATE,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    checked_at = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)
    assert module._database_age_hours(
        "2026-07-23T09:00:00Z",
        checked_at,
    ) == 71
    with pytest.raises(module.AuditFailure, match="database is stale"):
        module._database_age_hours(
            "2026-07-23T07:59:59Z",
            checked_at,
        )


def test_amd64_ci_is_native_read_only_and_does_not_upload_artifacts():
    workflow = AMD64_CI.read_text(encoding="utf-8")

    assert "runs-on: ubuntu-24.04" in workflow
    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "linux/amd64" in workflow
    assert "--container-only" in workflow
    assert "--worker-resilience" in workflow
    assert "upload-artifact" not in workflow
    assert "pull_request_target" not in workflow
    assert "@de0fac2e4500dabe0009e67214ff5f5447ce83dd" in workflow
    assert "Actions log retention: 7 days" in workflow
    assert "Checkout tested revision" in workflow


def test_build_dependency_lock_is_complete_and_hashed():
    requirements = _logical_requirements(
        BUILD_LOCK.read_text(encoding="utf-8")
    )
    assert {
        re.match(r"([A-Za-z0-9_.-]+)", requirement).group(1).lower()
        for requirement in requirements
    } == {"setuptools", "wheel"}
    assert all(
        "==" in requirement and "--hash=sha256:" in requirement
        for requirement in requirements
    )


def test_parity_tolerances_separate_cross_platform_from_same_runtime():
    spec = importlib.util.spec_from_file_location(
        "verify_container_runtime_tolerances",
        CONTAINER_GATE,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    generic_expected = {"value": 0.0}
    generic_actual = {"value": 1.5e-8}
    module._assert_parity(
        generic_expected,
        generic_actual,
        parity_scope="cross_platform",
    )
    with pytest.raises(module.GateFailure, match="numeric parity mismatch"):
        module._assert_parity(generic_expected, generic_actual)

    fixed_star_expected = {
        "astronomical_data": {
            "fixed_stars": [{"speed_distance": 0.0}],
        },
    }
    fixed_star_actual = {
        "astronomical_data": {
            "fixed_stars": [{"speed_distance": 0.0045}],
        },
    }
    module._assert_parity(
        fixed_star_expected,
        fixed_star_actual,
        parity_scope="cross_platform",
    )
    with pytest.raises(module.GateFailure, match="numeric parity mismatch"):
        module._assert_parity(fixed_star_expected, fixed_star_actual)


def test_worker_recovery_retry_is_bounded_to_transient_503(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "verify_container_runtime_worker_recovery",
        CONTAINER_GATE,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    responses = [
        (503, b"", {}),
        (503, b"", {}),
        (200, b'{"ready":true}', {}),
    ]
    monkeypatch.setattr(module, "_http", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    result, transient_503s = module._chart_after_worker_recovery(
        "http://127.0.0.1:8000",
        {"input": "not-sensitive"},
    )
    assert result == {"ready": True}
    assert transient_503s == 2

    monkeypatch.setattr(
        module,
        "_http",
        lambda *args, **kwargs: (500, b"", {}),
    )
    with pytest.raises(
        module.GateFailure,
        match="HTTP 500 after 0 transient 503",
    ):
        module._chart_after_worker_recovery(
            "http://127.0.0.1:8000",
            {"input": "not-sensitive"},
        )


def test_repository_gates_include_new_production_dependency_and_scripts():
    privacy_gate = PRIVACY_DEPENDENCY_GATE.read_text(encoding="utf-8")
    delivery_gate = DELIVERY_GATE.read_text(encoding="utf-8")

    assert "deploy/requirements.in" in privacy_gate
    assert "deploy/requirements.lock" in privacy_gate
    assert "deploy/build-requirements.lock" in privacy_gate
    for script in (
        "scripts/verify_container_runtime.py",
        "scripts/verify_image_supply_chain.py",
        "scripts/verify_linux_source_build.py",
    ):
        assert script in delivery_gate
