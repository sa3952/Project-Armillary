from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil

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
SOURCE_BUILD_VERIFIER = PROJECT_ROOT / "scripts" / "publication" / "verify_linux_source_build.py"
EPHEMERIS_VERIFIER = PROJECT_ROOT / "scripts" / "verification" / "verify_ephemeris_integrity.py"
PLACE_CATALOG_VERIFIER = (
    PROJECT_ROOT / "scripts" / "verification" / "verify_place_catalog.py"
)
CONTAINER_GATE = PROJECT_ROOT / "scripts" / "verification" / "verify_container_runtime.py"
DOCKER_CONTEXT_GATE = PROJECT_ROOT / "scripts" / "verification" / "verify_docker_context.py"
SUPPLY_CHAIN_GATE = PROJECT_ROOT / "scripts" / "publication" / "verify_image_supply_chain.py"
PRIVATE_AMD64_CI = (
    PROJECT_ROOT / ".github" / "workflows" / "private-engineering-ci.yml"
)
PUBLIC_AMD64_CI = (
    PROJECT_ROOT / ".github" / "workflows" / "production-amd64.yml"
)
AMD64_CI = (
    PRIVATE_AMD64_CI
    if PRIVATE_AMD64_CI.is_file()
    else PUBLIC_AMD64_CI
)
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
PARITY_BASELINE = DEPLOY_DIR / "parity-baseline-arm64.json"
PRIVACY_DEPENDENCY_GATE = (
    PROJECT_ROOT / "scripts" / "verification" / "verify_privacy_dependencies.py"
)
PRIVATE_DELIVERY_GATE = (
    PROJECT_ROOT / "scripts" / "verification" / "verify_delivery.py"
)
PUBLIC_DELIVERY_GATE = PROJECT_ROOT / "scripts" / "verify_delivery.py"
DELIVERY_GATE = (
    PRIVATE_DELIVERY_GATE
    if PRIVATE_DELIVERY_GATE.is_file()
    else PUBLIC_DELIVERY_GATE
)

PYTHON_IMAGE = (
    "python:3.13.14-slim-trixie"
    "@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
)


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
        DOCKER_CONTEXT_GATE,
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
    assert '"--limit-concurrency"' not in dockerfile
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


def test_container_build_identity_matches_the_oci_revision_label():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    gate = CONTAINER_GATE.read_text(encoding="utf-8")

    assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert (
        'CLASSICAL_ASTROLOGY_SOURCE_REVISION="${VCS_REF}"'
        in dockerfile
    )
    assert "_assert_container_build_identity" in gate
    assert "$.calculation_dossier.build_identity" in gate
    assert "does not match the OCI image revision" in gate


def test_dockerignore_excludes_local_build_review_and_secret_material():
    ignored = DOCKERIGNORE.read_text(encoding="utf-8")

    assert next(
        line for line in ignored.splitlines() if line and not line.startswith("#")
    ) == "**"
    for pattern in (
        ".git",
        "backend/.venv",
        ".build",
        "dist",
        ".hypothesis",
        ".claude",
        ".codex",
        "validation",
        "publication",
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
    for required in (
        "!backend/app/**",
        "!backend/ephe/**",
        "!backend/place_data/**",
        "!deploy/Dockerfile",
        "!frontend/app.js",
        "!third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz",
        "!third_party/pyswisseph/LICENSE.txt",
        "!scripts/verification/verify_ephemeris_integrity.py",
        "!scripts/publication/verify_linux_source_build.py",
    ):
        assert required in ignored

    policy_lines = [
        line
        for line in ignored.splitlines()
        if line and not line.startswith("#")
    ]
    reopen_at = policy_lines.index("!backend/app/**")
    for final_deny in (
        "**/.DS_Store",
        "**/__pycache__",
        "**/*.py[cod]",
        "frontend/tests",
        "frontend/tests/**",
    ):
        assert policy_lines.index(final_deny, reopen_at) > reopen_at, (
            f"{final_deny} is overridden by the later backend reopen rule"
        )


def test_docker_context_gate_enforces_closed_inventory_and_200_mib_cap():
    module = _load_script("docker_context_gate", DOCKER_CONTEXT_GATE)

    receipt = module.verify(PROJECT_ROOT)

    assert receipt["policy"] == "closed_required_paths"
    assert receipt["maximum_size_bytes"] == 200 * 1024 * 1024
    assert receipt["context_size_bytes"] < receipt["maximum_size_bytes"]
    assert receipt["forbidden_paths_present"] == []
    assert receipt["validation_paths_present"] == []


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
    assert "@d23441a48e516b6c34aea4fa41551a30e30af803" in workflow
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


def test_committed_parity_baseline_matches_current_response_contract():
    module = _load_script(
        "verify_container_runtime_baseline_contract",
        CONTAINER_GATE,
    )

    result = module.verify_committed_parity_baseline()

    assert result["status"] == "compatible"
    assert result["cases"] == 4
    assert result["response_schema_version"] == "0.10.0"

    baseline = json.loads(PARITY_BASELINE.read_text(encoding="utf-8"))
    assert (
        baseline["producer"]["module"]
        == "scripts.verification.generate_parity_baseline"
    )
    assert baseline["producer"]["product_source_dirty"] is False
    assert baseline["source"]["os"] == "linux"
    assert baseline["source"]["architecture"] == "arm64"
    assert baseline["source"]["image_id"].startswith("sha256:")
    assert re.fullmatch(
        r"[0-9a-f]{40}",
        baseline["source"]["revision"],
    )
    serialized = PARITY_BASELINE.read_text(encoding="utf-8")
    assert "backend/tests/" not in serialized
    assert "birth_time_sensitivity" in serialized


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


def test_container_log_gate_accepts_only_closed_chart_telemetry():
    module = _load_script(
        "verify_container_runtime_log_contract",
        CONTAINER_GATE,
    )
    safe_event = {
        "event_schema_version": "privacy-request-event-v1",
        "event": "http_request_completed",
        "request_id": "0" * 32,
        "route": "/api/chart",
        "method": "POST",
        "status_code": 200,
        "duration_bucket": "50_to_249ms",
        "request_size_bucket": "1_to_1024b",
        "outcome": "success",
        "error_code": None,
    }

    result = module._verify_container_logs(
        "PRIVACY_EVENT " + json.dumps(safe_event, separators=(",", ":"))
    )
    assert result["chart_status_only_event_present"] is True
    assert result["raw_chart_access_log_absent"] is True

    with pytest.raises(module.GateFailure, match="disclosed"):
        module._verify_container_logs(
            '"POST /api/chart HTTP/1.1" 200\n'
            + "PRIVACY_EVENT "
            + json.dumps(safe_event)
        )

    unsafe_event = {**safe_event, "unexpected": "outside-closed-schema"}
    with pytest.raises(module.GateFailure, match="closed schema"):
        module._verify_container_logs(
            "PRIVACY_EVENT " + json.dumps(unsafe_event)
        )


def test_repository_gates_include_new_production_dependency_and_scripts():
    privacy_gate = PRIVACY_DEPENDENCY_GATE.read_text(encoding="utf-8")
    delivery_module = _load_script(
        "verify_delivery_production_contract",
        DELIVERY_GATE,
    )

    assert "deploy/requirements.in" in privacy_gate
    assert "deploy/requirements.lock" in privacy_gate
    assert "deploy/build-requirements.lock" in privacy_gate
    if hasattr(delivery_module, "AUTOMATED_GATES"):
        assert (
            "container-parity-baseline-compatibility"
            in delivery_module.AUTOMATED_GATES
        )
        expected_release_gates = {
            "scripts.publication.verify_image_supply_chain",
            "scripts.publication.verify_linux_source_build",
        }
    else:
        expected_release_gates = {
            "scripts.verification.verify_container_runtime",
            "scripts.publication.verify_image_supply_chain",
            "scripts.publication.verify_linux_source_build",
        }
    assert expected_release_gates <= set(delivery_module.RELEASE_ARTIFACT_GATES)


def test_final_stage_removes_apt_lists_and_strips_inherited_setid_bits():
    """The pinned Debian base ships APT package lists (~231 KiB across six
    entries) and eleven setuid/setgid utilities that this service never calls.
    The supported Compose profile blocks privilege gain, so no escalation is
    claimed, but neither belongs in a runtime image whose only job is to serve
    one ASGI application."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    final_stage = dockerfile.split("FROM python:", 2)[2]

    assert "rm --recursive --force /var/lib/apt/lists/*" in final_stage, (
        "final stage does not clear the inherited APT lists"
    )
    assert "-perm /6000" in final_stage and "chmod a-s" in final_stage, (
        "final stage does not strip inherited setuid/setgid bits"
    )
    # Stripping the mode is preferred over deleting the binaries: an unexpected
    # dependency still finds the file, it simply cannot escalate.
    assert "rm --recursive --force /usr/bin/su" not in final_stage


def test_image_carries_licence_notices_and_both_dataset_verifiers():
    """Both `.dockerignore` variants deliberately admit LICENSE to the build
    context, but the Dockerfile never copied it, so the running container held
    no copy of the licence the service is distributed under.  The place catalog
    is opened with immutable=1 at runtime, which instructs SQLite to trust the
    file, so its existing verifier belongs in the startup gate alongside the
    ephemeris one."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY LICENSE /app/LICENSE" in dockerfile
    assert "/app/THIRD_PARTY_NOTICES.md" in dockerfile
    assert "/build-context/THIRD_PARTY_NOTICES.md" in dockerfile
    assert (
        "/build-context/publication/public_overlay/THIRD_PARTY_NOTICES.md"
        in dockerfile
    )
    assert "missing THIRD_PARTY_NOTICES.md" in dockerfile
    assert (
        "COPY scripts/verification/verify_place_catalog.py "
        "/app/scripts/verification/verify_place_catalog.py"
    ) in dockerfile
    assert "/app/scripts/verification/verify_place_catalog.py \\" in dockerfile, (
        "the copied verifier is not included in the read-only mode pass"
    )

    for dockerignore in (
        PROJECT_ROOT / ".dockerignore",
        PROJECT_ROOT / "publication" / "public_overlay" / ".dockerignore",
    ):
        text = dockerignore.read_text(encoding="utf-8")
        assert "!LICENSE" in text
        assert "!scripts/verification/verify_place_catalog.py" in text, (
            f"{dockerignore} does not admit the place-catalog verifier"
        )


def test_entrypoint_refuses_an_absent_or_wrong_profile_and_gates_both_datasets():
    """`load_settings()` defaults to the permissive local profile when
    CLASSICAL_ASTROLOGY_PROFILE is absent, which silently drops the 16 KiB body
    bound, the JSON content-type gate, error minimization and the noindex
    header.  A deployed image must refuse that rather than downgrade."""
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")

    assert "CLASSICAL_ASTROLOGY_PROFILE:?" in entrypoint, (
        "entrypoint does not refuse an absent profile"
    )
    assert 'CLASSICAL_ASTROLOGY_PROFILE}" != "private_alpha"' in entrypoint
    assert "exit 1" in entrypoint
    assert "verify_ephemeris_integrity.py --check" in entrypoint
    assert "verify_place_catalog.py --check" in entrypoint

    ephemeris_at = entrypoint.index("verify_ephemeris_integrity.py")
    catalog_at = entrypoint.index("verify_place_catalog.py")
    exec_at = entrypoint.index('exec "$@"')
    assert ephemeris_at < exec_at and catalog_at < exec_at, (
        "a dataset gate runs after the listener starts"
    )
    # The refusal must not print the environment it rejected.
    assert "$CLASSICAL_ASTROLOGY_PROFILE" not in entrypoint.replace(
        '"${CLASSICAL_ASTROLOGY_PROFILE}"', ""
    )


def test_place_catalog_startup_verifier_rejects_a_corrupted_catalog(tmp_path):
    module = _load_script(
        "verify_place_catalog_negative_control",
        PLACE_CATALOG_VERIFIER,
    )
    source_dir = PROJECT_ROOT / "backend" / "place_data"
    data_dir = tmp_path / "place_data"
    data_dir.mkdir()
    shutil.copy2(source_dir / "catalog_manifest.json", data_dir)
    shutil.copy2(source_dir / "places.sqlite3", data_dir)

    catalog = data_dir / "places.sqlite3"
    with catalog.open("r+b") as handle:
        original = handle.read(1)
        handle.seek(0)
        handle.write(bytes([original[0] ^ 0xFF]))

    module.DATA_DIR = data_dir
    module.MANIFEST_PATH = data_dir / "catalog_manifest.json"
    with pytest.raises(SystemExit, match="SHA-256"):
        module.verify()


def test_staging_site_bounds_place_search_and_hides_the_upstream_server_header():
    """`/api/places/search` is a JSON compute endpoint whose cost varies with
    the submitted terms, but it did not match the chart location regex and so
    inherited the static zone's 120 r/m — the loosest limit in the file — while
    being the most cost-amplifying route measured."""
    template = (
        DEPLOY_DIR / "staging" / "nginx-site.conf.template"
    ).read_text(encoding="utf-8")

    assert "location ~ ^/api/places/search(?:/|$)" in template, (
        "place search has no dedicated location and falls back to the static zone"
    )
    search_block = template.split("location ~ ^/api/places/search(?:/|$)", 1)[1]
    search_block = search_block.split("location /", 1)[0]
    assert "zone=private_alpha_chart_global" in search_block, (
        "place search is not bounded at the chart rate"
    )
    assert "private_alpha_static_global" not in search_block
    assert "limit_conn private_alpha_connections" in search_block

    assert "proxy_hide_header Server" in template, (
        "the upstream `server: uvicorn` header is not hidden"
    )
