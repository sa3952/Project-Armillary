from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = PROJECT_ROOT / "deploy"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"
COMPOSE = DEPLOY_DIR / "compose.yaml"
LOCK = DEPLOY_DIR / "requirements.lock"
LOCK_INPUT = DEPLOY_DIR / "requirements.in"
BUILD_LOCK = DEPLOY_DIR / "build-requirements.lock"
BUILD_LOCK_INPUT = DEPLOY_DIR / "build-requirements.in"
EPHEMERIS_MANIFEST = DEPLOY_DIR / "ephemeris.sha256"
ENTRYPOINT = DEPLOY_DIR / "entrypoint.sh"
HEALTHCHECK = DEPLOY_DIR / "container_healthcheck.py"
FRONTEND_CONTRACT = DEPLOY_DIR / "frontend-contract.json"
SOURCE_BUILD_VERIFIER = PROJECT_ROOT / "scripts" / "publication" / "verify_linux_source_build.py"
EPHEMERIS_VERIFIER = PROJECT_ROOT / "scripts" / "verification" / "verify_ephemeris_integrity.py"
PLACE_CATALOG_VERIFIER = (
    PROJECT_ROOT / "scripts" / "verification" / "verify_place_catalog.py"
)
CONTAINER_GATE = PROJECT_ROOT / "scripts" / "verification" / "verify_container_runtime.py"
PARITY_BASELINE_GENERATOR = (
    PROJECT_ROOT / "scripts" / "verification" / "generate_parity_baseline.py"
)
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
    "@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
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
        FRONTEND_CONTRACT,
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
        "fastapi==0.141.1",
        "uvicorn==0.52.1",
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
    assert "FROM scratch AS release-evidence" not in dockerfile
    runtime_stage = dockerfile.split(f"FROM {PYTHON_IMAGE}", 2)[2]
    assert "COPY --from=builder /release" in runtime_stage
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
    assert "COPY frontend /app/frontend" not in dockerfile
    assert 'org.classical-astrology.frontend.mode="external-release-v1"' in dockerfile
    assert "COPY deploy/frontend-contract.json" in dockerfile
    assert "frontend-runtime-assets.json" not in dockerfile


def test_linux_source_build_verifier_can_run_from_docker_copy_location(tmp_path):
    copied = tmp_path / "build" / "verify_linux_source_build.py"
    copied.parent.mkdir()
    shutil.copyfile(SOURCE_BUILD_VERIFIER, copied)

    completed = subprocess.run(
        [sys.executable, str(copied), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--source-dir" in completed.stdout


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
    assert 'ARG VCS_REF="uncommitted"' not in dockerfile
    assert "grep -Eq '^[0-9a-f]{40}$'" in dockerfile
    assert '"git", "archive", "--format=tar"' in gate
    assert "materialize_context(" in gate
    assert (
        'CLASSICAL_ASTROLOGY_SOURCE_REVISION="${VCS_REF}"'
        in dockerfile
    )
    assert "_assert_container_build_identity" in gate
    assert "$.calculation_dossier.build_identity" in gate
    assert "does not match the OCI and mounted frontend release" in gate


def _content_identity_row(**overrides):
    import tarfile

    gate = _load_script("content_identity_gate", CONTAINER_GATE)
    member = tarfile.TarInfo(
        name=overrides.pop("name", "/usr/local/bin/python3")
    )
    member.mode = overrides.pop("mode", 0o755)
    member.uid = overrides.pop("uid", 0)
    member.gid = overrides.pop("gid", 0)
    member.type = overrides.pop("type", tarfile.SYMTYPE)
    member.linkname = overrides.pop("linkname", "python3.13")
    member.pax_headers = overrides.pop("pax_headers", {})
    payload_digest = overrides.pop("payload_digest", "")
    assert not overrides, f"unused overrides: {sorted(overrides)}"
    return gate._content_identity_row(member, member.name, payload_digest)


def test_content_identity_separates_symlink_targets():
    baseline = _content_identity_row()

    assert _content_identity_row(linkname="python3.13") == baseline
    assert _content_identity_row(linkname="/tmp/attacker") != baseline
    assert _content_identity_row(linkname="../../../bin/sh") != baseline


def test_content_identity_separates_security_capabilities():
    baseline = _content_identity_row()
    capability = _content_identity_row(
        pax_headers={
            "SCHILY.xattr.security.capability": "cap_net_raw+ep",
        }
    )

    assert capability != baseline
    assert capability != _content_identity_row(
        pax_headers={
            "SCHILY.xattr.security.capability": "cap_sys_admin+ep",
        }
    )


def test_content_identity_security_metadata_encoding_is_unambiguous():
    embedded_pair = _content_identity_row(
        pax_headers={
            "SCHILY.xattr.security.a": (
                "x;SCHILY.xattr.security.b=y"
            ),
        }
    )
    actual_pair = _content_identity_row(
        pax_headers={
            "SCHILY.xattr.security.a": "x",
            "SCHILY.xattr.security.b": "y",
        }
    )

    assert embedded_pair != actual_pair


def test_content_identity_ignores_unstable_pax_metadata():
    baseline = _content_identity_row()

    for noisy in ("mtime", "atime", "ctime", "size"):
        assert _content_identity_row(
            pax_headers={noisy: "1"},
        ) == baseline


def test_content_identity_preserves_existing_distinctions():
    baseline = _content_identity_row()

    assert _content_identity_row(mode=0o4755) != baseline
    assert _content_identity_row(uid=10001) != baseline
    assert _content_identity_row(gid=10001) != baseline
    assert _content_identity_row(
        name="/usr/local/bin/python",
    ) != baseline
    assert _content_identity_row(payload_digest="deadbeef") != baseline


def test_content_identity_schema_names_the_projection_change():
    gate = _load_script("content_identity_schema_gate", CONTAINER_GATE)

    assert gate.CONTENT_IDENTITY_SCHEMA == "content-identity-v2"


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
        "!deploy/frontend-contract.json",
        "!third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz",
        "!scripts/verification/verify_ephemeris_integrity.py",
        "!scripts/publication/verify_linux_source_build.py",
    ):
        assert required in ignored
    assert "!frontend/zh-TW/calculate.js" not in ignored

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
        "**/.env",
        "**/.env.*",
        "**/*.pem",
        "**/*.key",
        "**/*secret*",
        "**/*token*",
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
    gate = (
        PROJECT_ROOT / "scripts" / "verification" / "build_release_image.py"
    ).read_text(encoding="utf-8")

    assert "build-context-probe-8f38f069.txt" in gate
    assert ".env.private-alpha-canary" not in gate
    assert "runtime-secret-canary.txt" not in gate


def test_image_canary_scan_is_streaming_and_not_prefix_truncated():
    gate = CONTAINER_GATE.read_text(encoding="utf-8")

    assert "len(scanned) < 8 * 1024 * 1024" not in gate
    assert "canary_tail" in gate


def test_image_environment_uses_closed_keys_and_value_provenance(monkeypatch):
    module = _load_script("verify_image_environment_contract", CONTAINER_GATE)
    revision = "a" * 40
    environment = [
        "PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG=C.UTF-8",
        "GPG_KEY=" + "A" * 40,
        "PYTHON_VERSION=3.13.14",
        "PYTHON_SHA256=" + "b" * 64,
        "CLASSICAL_ASTROLOGY_PROFILE=private_alpha",
        "CLASSICAL_ASTROLOGY_REQUIRE_FRONTEND_RELEASE=1",
        f"CLASSICAL_ASTROLOGY_SOURCE_REVISION={revision}",
        "PYTHONFAULTHANDLER=1",
        "PYTHONUNBUFFERED=1",
        "PYTHONDONTWRITEBYTECODE=1",
    ]

    def inspect_with(items):
        payload = [{
            "Id": "sha256:" + "c" * 64,
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {
                "User": "10001:10001",
                "Env": items,
                "Labels": {
                    "org.opencontainers.image.revision": revision,
                    "org.projectarmillary.publication.status": (
                        "provisional_unpublished"
                    ),
                },
                "Healthcheck": {"Test": ["CMD", "container_healthcheck.py"]},
            },
        }]
        monkeypatch.setattr(
            module,
            "_run",
            lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(payload)),
        )
        return module._inspect_image("fixture", None)

    assert set(inspect_with(environment)["environment_keys"]) == {
        item.split("=", 1)[0] for item in environment
    }
    with pytest.raises(module.GateFailure, match="unexpected=.*EXTRA"):
        inspect_with([*environment, "EXTRA=not-authorized"])
    with pytest.raises(module.GateFailure, match="value mismatch: PATH"):
        inspect_with([
            "PATH=/tmp/attacker",
            *[item for item in environment if not item.startswith("PATH=")],
        ])


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


def test_container_receipt_scope_names_skipped_worker_resilience():
    module = _load_script(
        "verify_container_runtime_scope",
        CONTAINER_GATE,
    )
    assert module._container_verdict_scope(None) == (
        "container_parity_only_worker_resilience_not_run"
    )
    assert module._container_verdict_scope({"passed": True}) == (
        "container_parity_and_worker_resilience"
    )
    source = CONTAINER_GATE.read_text(encoding="utf-8")
    assert '"verdict_scope": _container_verdict_scope(resilience)' in source


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
    requested = _logical_requirements(
        BUILD_LOCK_INPUT.read_text(encoding="utf-8")
    )
    requirements = _logical_requirements(
        BUILD_LOCK.read_text(encoding="utf-8")
    )
    locked_names = {
        re.match(r"([A-Za-z0-9_.-]+)", requirement).group(1).lower()
        for requirement in requirements
    }
    requested_names = {
        re.match(r"([A-Za-z0-9_.-]+)", requirement).group(1).lower()
        for requirement in requested
    }
    assert requested_names <= locked_names
    assert {
        "setuptools",
        "wheel",
        "pdm-backend",
        "hatchling",
        "maturin",
    } <= locked_names
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
    module._assert_parity(
        fixed_star_expected,
        fixed_star_actual,
        path="$[0]",
        parity_scope="cross_platform",
    )
    with pytest.raises(module.GateFailure, match="numeric parity mismatch"):
        module._assert_parity(fixed_star_expected, fixed_star_actual)


def test_cross_runtime_tz_receipt_ignores_resolver_label_but_not_version():
    module = _load_script(
        "verify_container_runtime_tz_receipt",
        CONTAINER_GATE,
    )
    expected = {
        "library_info": {
            "tz_database": {
                "version": "2026b",
                "source": "zoneinfo_file:+VERSION",
            },
        },
        "calculation_dossier": {
            "engine": {
                "tz_database": {
                    "version": "2026b",
                    "source": "zoneinfo_file:+VERSION",
                },
            },
        },
    }
    actual = {
        "library_info": {
            "tz_database": {
                "version": "2026b",
                "source": "zoneinfo_file:tzdata.zi",
            },
        },
        "calculation_dossier": {
            "engine": {
                "tz_database": {
                    "version": "2026b",
                    "source": "zoneinfo_file:tzdata.zi",
                },
            },
        },
    }

    module._assert_parity(expected, actual, parity_scope="cross_platform")
    actual["library_info"]["tz_database"]["version"] = "2026c"
    with pytest.raises(module.GateFailure, match="parity mismatch"):
        module._assert_parity(expected, actual, parity_scope="cross_platform")


def test_committed_parity_baseline_matches_current_response_contract():
    module = _load_script(
        "verify_container_runtime_baseline_contract",
        CONTAINER_GATE,
    )

    result = module.verify_committed_parity_baseline()

    assert result["status"] == (
        "shape_numeric_compatible_with_additive_image_pending"
    )
    assert result["cases"] == 4
    # 2026-08-07: the baseline was regenerated from a target-platform container
    # at `9abe412` (`PIA-2026-08-06-008`). It had been frozen at `b2be06b`,
    # which predates the API contract moving 0.10.0 -> 0.13.0, so this literal
    # was pinning the staleness rather than the contract. Read it from the
    # committed contract instead, so the two cannot drift apart again.
    from app.main import SCHEMA_VERSION

    assert result["response_schema_version"] == SCHEMA_VERSION

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


def test_platform_specific_baseline_missing_or_wrong_platform_fails_closed(
    monkeypatch,
    tmp_path,
):
    module = _load_script(
        "verify_container_runtime_platform_baseline",
        CONTAINER_GATE,
    )
    amd64 = tmp_path / "parity-baseline-amd64.json"
    monkeypatch.setattr(
        module,
        "PARITY_BASELINES",
        {
            "linux/amd64": amd64,
            "linux/arm64": PARITY_BASELINE,
        },
    )
    with pytest.raises(module.GateFailure, match="linux/amd64 baseline"):
        module._load_committed_cross_platform_baseline(
            [],
            platform="linux/amd64",
            require_public_identity=True,
        )

    amd64.write_text(json.dumps({
        "schema_version": "private-alpha-platform-parity-baseline-v2",
        "source": {
            "platform": "linux/arm64",
            "architecture": "arm64",
            "os": "linux",
            "revision": "a" * 40,
            "public_source_revision": "a" * 40,
            "image_id": "sha256:" + "b" * 64,
        },
        "producer": {
            "module": "scripts.verification.generate_parity_baseline",
        },
        "payloads": [],
        "responses": [],
    }), encoding="utf-8")
    with pytest.raises(module.GateFailure, match="contract mismatch"):
        module._load_committed_cross_platform_baseline(
            [],
            platform="linux/amd64",
            require_public_identity=True,
        )

    payload = json.loads(amd64.read_text(encoding="utf-8"))
    payload["source"]["platform"] = "linux/amd64"
    payload["source"]["architecture"] = "amd64"
    amd64.write_text(json.dumps(payload), encoding="utf-8")
    assert module._load_committed_cross_platform_baseline(
        [],
        platform="linux/amd64",
        require_public_identity=True,
    ) == []


def test_committed_parity_baseline_rejects_numeric_drift(monkeypatch):
    """The pre-build consumer must compare values, not only JSON shape."""
    module = _load_script(
        "verify_container_runtime_baseline_numeric",
        CONTAINER_GATE,
    )
    expected = [{"schema_version": "0.test", "value": 10.0}]
    drifted = [{"schema_version": "0.test", "value": 10.001}]
    monkeypatch.setattr(module, "_payloads", lambda: [{}])
    monkeypatch.setattr(
        module,
        "_load_committed_cross_platform_baseline",
        lambda _payloads, **_kwargs: expected,
    )
    monkeypatch.setattr(module, "_local_baselines", lambda _payloads: drifted)

    with pytest.raises(module.GateFailure, match="numeric parity mismatch"):
        module.verify_committed_parity_baseline()

    monkeypatch.setattr(module, "_local_baselines", lambda _payloads: expected)
    result = module.verify_committed_parity_baseline()
    assert result["status"] == "shape_numeric_compatible"
    assert result["cases"] == 1
    assert result["response_schema_version"] == "0.test"
    assert result["protected_semantic_status"] == "current"
    assert result["protected_semantic_mismatch_count"] == 0
    assert result["additive_image_pending_count"] == 0


def test_parity_baseline_generator_rejects_an_image_from_another_revision(
    monkeypatch,
):
    generator = _load_script(
        "generate_parity_baseline_identity",
        PARITY_BASELINE_GENERATOR,
    )
    head_revision = "a" * 40
    image_revision = "b" * 40
    monkeypatch.setattr(
        generator,
        "_committed_product_revision",
        lambda: head_revision,
    )
    monkeypatch.setattr(generator.runtime_gate, "_payloads", lambda: [])
    monkeypatch.setattr(
        generator.runtime_gate,
        "_build_image",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        generator.runtime_gate,
        "_inspect_image",
        lambda _image, _platform: {
            "id": "sha256:" + "c" * 64,
            "revision": image_revision,
        },
    )
    monkeypatch.setattr(
        generator.runtime_gate,
        "_single_worker_parity",
        lambda *_args: ({}, []),
    )
    monkeypatch.setattr(
        generator.runtime_gate,
        "_assert_container_build_identity",
        lambda *_args: None,
    )

    with pytest.raises(
        generator.BaselineGenerationFailure,
        match="image was not built from this working tree",
    ):
        generator.build_baseline(
            "failure-first identity control",
            image_name="fixture",
            platform="linux/amd64",
            public_source_revision=head_revision,
            publication_receipt=Path("publication.json"),
            build_evidence_dir=Path("evidence"),
        )


def test_parity_baseline_generator_always_builds_in_the_same_invocation(monkeypatch):
    generator = _load_script(
        "generate_parity_baseline_prebuilt",
        PARITY_BASELINE_GENERATOR,
    )
    revision = "a" * 40
    monkeypatch.setattr(
        generator,
        "_committed_product_revision",
        lambda: revision,
    )
    monkeypatch.setattr(generator.runtime_gate, "_payloads", lambda: [])
    built = []
    monkeypatch.setattr(
        generator.runtime_gate,
        "_build_image",
        lambda *args, **kwargs: built.append((args, kwargs)),
    )
    monkeypatch.setattr(
        generator.runtime_gate,
        "_inspect_image",
        lambda _image, _platform: {
            "id": "sha256:" + "c" * 64,
            "revision": revision,
        },
    )
    monkeypatch.setattr(
        generator.runtime_gate,
        "_single_worker_parity",
        lambda *_args: ({}, []),
    )
    monkeypatch.setattr(
        generator.runtime_gate,
        "_assert_container_build_identity",
        lambda *_args: None,
    )

    generator.build_baseline(
        "same-invocation build provenance",
        image_name="fixture",
        platform="linux/amd64",
        public_source_revision=revision,
        publication_receipt=Path("publication.json"),
        build_evidence_dir=Path("evidence"),
    )
    assert built == [
        (
            ("fixture",),
            {
                "require_clean": True,
                "platform": "linux/amd64",
                "purpose": "release-candidate",
                "publication_receipt": Path("publication.json"),
                "evidence_dir": Path("evidence"),
            },
        )
    ]


def test_parity_baseline_generator_records_platform_public_revision_and_producer(
    monkeypatch,
):
    generator = _load_script(
        "generate_parity_baseline_platform_identity",
        PARITY_BASELINE_GENERATOR,
    )
    revision = "a" * 40
    image_id = "sha256:" + "c" * 64
    monkeypatch.setattr(generator, "_committed_product_revision", lambda: revision)
    monkeypatch.setattr(generator.runtime_gate, "_payloads", lambda: [])
    monkeypatch.setattr(generator.runtime_gate, "_build_image", lambda *_a, **_k: None)
    monkeypatch.setattr(
        generator.runtime_gate,
        "_inspect_image",
        lambda _image, platform: {
            "id": image_id,
            "revision": revision,
            "architecture": platform.split("/", 1)[1],
        },
    )
    monkeypatch.setattr(
        generator.runtime_gate,
        "_single_worker_parity",
        lambda *_args: ({}, []),
    )
    monkeypatch.setattr(
        generator.runtime_gate,
        "_assert_container_build_identity",
        lambda *_args: None,
    )

    baseline = generator.build_baseline(
        "linux amd64 candidate",
        image_name="fixture",
        platform="linux/amd64",
        public_source_revision=revision,
        publication_receipt=Path("publication.json"),
        build_evidence_dir=Path("evidence"),
    )

    assert baseline["schema_version"] == "private-alpha-platform-parity-baseline-v2"
    assert baseline["source"] == {
        "platform": "linux/amd64",
        "architecture": "amd64",
        "os": "linux",
        "revision": revision,
        "public_source_revision": revision,
        "image_id": image_id,
        "evidence": (
            "governed materialized-context container fixture from the exact "
            "public checkout"
        ),
        "reason": "linux amd64 candidate",
    }
    assert baseline["producer"]["module"] == (
        "scripts.verification.generate_parity_baseline"
    )

    with pytest.raises(
        generator.BaselineGenerationFailure,
        match="public source revision",
    ):
        generator.build_baseline(
            "wrong public identity",
            image_name="fixture",
            platform="linux/amd64",
            public_source_revision="b" * 40,
            publication_receipt=Path("publication.json"),
            build_evidence_dir=Path("evidence"),
        )


def test_parity_baseline_revision_check_covers_docker_build_inputs(monkeypatch):
    generator = _load_script(
        "generate_parity_baseline_dirty_build_input",
        PARITY_BASELINE_GENERATOR,
    )

    def fake_git(*arguments):
        if arguments == ("rev-parse", "HEAD"):
            return SimpleNamespace(
                returncode=0,
                stdout="a" * 40 + "\n",
                stderr="",
            )
        dirty = (
            " M deploy/Dockerfile\n"
            if "deploy/Dockerfile" in arguments
            else ""
        )
        return SimpleNamespace(returncode=0, stdout=dirty, stderr="")

    monkeypatch.setattr(generator, "_git", fake_git)

    with pytest.raises(
        generator.BaselineGenerationFailure,
        match="numeric image build inputs are dirty",
    ):
        generator._committed_product_revision()


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
            "committed-baseline-shape-and-numeric-compatibility"
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
        }
    assert expected_release_gates <= set(delivery_module.RELEASE_ARTIFACT_GATES)


def test_compose_is_runtime_only_and_cannot_reinterpret_raw_source_context():
    compose = COMPOSE.read_text(encoding="utf-8")

    assert re.search(r"^\s+build:\s*$", compose, re.MULTILINE) is None
    assert "context: .." not in compose
    assert "private_alpha_probe" not in compose
    assert 'image: "classical-astrology-private-alpha:${IMAGE_TAG:-local}"' in compose
    # Private staging orchestration is intentionally absent from Corresponding
    # Source. Its separate private owner test is
    # test_staging_activation_consumes_verified_image_without_building; this
    # portable test owns only the shipped Compose runtime contract.


def test_protected_semantic_comparator_distinguishes_trace_formula_order():
    from scripts.tools.semantic_currentness import protected_semantic_mismatches

    expected = {
        "calculation_trace": [{
            "title": "UTC to JD",
            "formula": "JD_UT, JD_ET = swe.utc_to_jd(...)"
        }]
    }
    actual = copy.deepcopy(expected)
    actual["calculation_trace"][0]["formula"] = (
        "JD_ET, JD_UT = swe.utc_to_jd(...)"
    )

    assert protected_semantic_mismatches(expected, actual) == [{
        "path": "$.calculation_trace[0].formula",
        "expected": "JD_UT, JD_ET = swe.utc_to_jd(...)",
        "actual": "JD_ET, JD_UT = swe.utc_to_jd(...)",
    }]


def test_committed_image_baseline_reports_semantic_rebuild_boundary():
    gate = _load_script(
        "verify_container_runtime_semantic_boundary",
        CONTAINER_GATE,
    )

    result = gate.verify_committed_parity_baseline()

    assert result["status"] == (
        "shape_numeric_compatible_with_additive_image_pending"
    )
    assert result["additive_image_pending_count"] > 0
    assert result["protected_semantic_status"] == "image_rebuild_pending"
    assert result["protected_semantic_mismatch_count"] > 0
    assert any(
        item["path"] == "$.calculation_trace[1].formula"
        for item in result["protected_semantic_mismatches"]
    )
    with pytest.raises(gate.GateFailure, match="protected semantics differ"):
        gate.verify_committed_parity_baseline(
            require_protected_semantics=True,
        )


def test_supply_chain_inventory_rejects_wrong_installed_python_version():
    module = _load_script("verify_supply_chain_versions", SUPPLY_CHAIN_GATE)
    sbom = {
        "artifacts": [
            {"type": "python", "name": "fastapi", "version": "0.1"},
            {"type": "python", "name": "uvicorn", "version": "0.2"},
        ]
    }
    with pytest.raises(module.AuditFailure, match="version mismatch"):
        module._verify_python_versions(
            sbom,
            [
                {"name": "fastapi", "version": "9.9"},
                {"name": "uvicorn", "version": "0.2"},
            ],
        )
    assert module._verify_python_versions(
        sbom,
        [
            {"name": "fastapi", "version": "0.1"},
            {"name": "uvicorn", "version": "0.2"},
        ],
    ) == {"fastapi": "0.1", "uvicorn": "0.2"}


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

    dockerignores = [PROJECT_ROOT / ".dockerignore"]
    overlay_root = PROJECT_ROOT / "publication" / "public_overlay"
    if _is_verified_public_source_export(PROJECT_ROOT):
        assert not overlay_root.exists()
    else:
        assert overlay_root.is_dir(), "private source lost its public overlay"
        overlay_dockerignore = overlay_root / ".dockerignore"
        assert overlay_dockerignore.is_file()
        dockerignores.append(overlay_dockerignore)
    for dockerignore in dockerignores:
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


def _is_verified_public_source_export(root: Path) -> bool:
    marker = root / "SOURCE_EXPORT.json"
    inventory = root / "PUBLICATION_FILES.json"
    if not marker.is_file() or not inventory.is_file():
        return False
    try:
        receipt = json.loads(marker.read_text(encoding="utf-8"))
        files = json.loads(inventory.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        receipt.get("schema_version") != 1
        or receipt.get("export_mode") != "closed_allowlist"
        or not re.fullmatch(
            r"[0-9a-f]{40}", str(receipt.get("private_source_revision", ""))
        )
    ):
        return False
    entries = files.get("files")
    if not isinstance(entries, list):
        return False
    marker_digest = hashlib.sha256(marker.read_bytes()).hexdigest()
    return any(
        entry.get("path") == "SOURCE_EXPORT.json"
        and entry.get("sha256") == marker_digest
        for entry in entries
        if isinstance(entry, dict)
    )


def _staging_nginx_template() -> str:
    template = DEPLOY_DIR / "staging" / "nginx-site.conf.template"
    if not template.is_file():
        if _is_verified_public_source_export(PROJECT_ROOT):
            pytest.skip(
                "host-only nginx template is outside the published tree; this "
                "run cannot verify limiter keys, place-search routing, connection "
                "limits, or upstream Server-header hiding"
            )
        pytest.fail(
            "host-only nginx template is missing from the private source tree"
        )
    return template.read_text(encoding="utf-8")


def test_missing_staging_template_fails_in_private_tree(monkeypatch, tmp_path):
    monkeypatch.setitem(globals(), "DEPLOY_DIR", tmp_path / "deploy")
    monkeypatch.setitem(globals(), "PROJECT_ROOT", tmp_path)

    caught = None
    try:
        _staging_nginx_template()
    except BaseException as exc:  # pytest outcomes intentionally derive here.
        caught = exc

    assert isinstance(caught, pytest.fail.Exception), repr(caught)


def test_missing_host_template_is_skipped_in_exported_tree(
    monkeypatch,
    tmp_path,
):
    marker = tmp_path / "SOURCE_EXPORT.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "export_mode": "closed_allowlist",
                "private_source_revision": "a" * 40,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "PUBLICATION_FILES.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "SOURCE_EXPORT.json",
                        "sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "DEPLOY_DIR", tmp_path / "deploy")
    monkeypatch.setitem(globals(), "PROJECT_ROOT", tmp_path)

    caught = None
    try:
        _staging_nginx_template()
    except BaseException as exc:  # pytest outcomes intentionally derive here.
        caught = exc

    assert isinstance(caught, pytest.skip.Exception), repr(caught)


def test_untracked_marker_alone_cannot_make_private_missing_template_skip(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "SOURCE_EXPORT.json").write_text("{}", encoding="utf-8")
    monkeypatch.setitem(globals(), "DEPLOY_DIR", tmp_path / "deploy")
    monkeypatch.setitem(globals(), "PROJECT_ROOT", tmp_path)

    caught = None
    try:
        _staging_nginx_template()
    except BaseException as exc:
        caught = exc

    assert isinstance(caught, pytest.fail.Exception), repr(caught)


def test_runtime_specific_value_paths_still_have_shape_coverage():
    module = _load_script("verify_container_runtime_shape", CONTAINER_GATE)
    expected = {
        "calculation_dossier": {
            "build_identity": {
                "status": "available",
                "source_revision": "a",
                "revision_source": "environment",
                "release_identity": {"backend": {"image_id": "sha256:a"}},
            },
            "engine": {"tz_database": {"source": "tzdata.zi"}},
        },
        "library_info": {"tz_database": {"source": "tzdata.zi"}},
    }

    missing = copy.deepcopy(expected)
    del missing["calculation_dossier"]["build_identity"]["status"]
    with pytest.raises(module.GateFailure, match="build-identity status mismatch"):
        module._assert_response_shape(expected, missing, path="$[0]")

    collapsed = copy.deepcopy(expected)
    collapsed["calculation_dossier"]["build_identity"] = "invalid"
    with pytest.raises(module.GateFailure, match="response type mismatch"):
        module._assert_response_shape(expected, collapsed, path="$[0]")

    unavailable = copy.deepcopy(expected)
    unavailable["calculation_dossier"]["build_identity"] = {
        "status": "unavailable",
        "source_revision": None,
        "revision_source": None,
    }
    module._assert_response_shape(expected, unavailable, path="$[0]")

    malformed_available = copy.deepcopy(expected)
    del malformed_available["calculation_dossier"]["build_identity"][
        "release_identity"
    ]
    with pytest.raises(module.GateFailure, match="response key mismatch"):
        module._assert_response_shape(
            expected, malformed_available, path="$[0]"
        )

    wrong_tz_type = copy.deepcopy(expected)
    wrong_tz_type["library_info"]["tz_database"]["source"] = None
    with pytest.raises(module.GateFailure, match="response scalar type mismatch"):
        module._assert_response_shape(expected, wrong_tz_type, path="$[0]")


def test_parity_build_input_contract_rejects_missing_and_unlisted_copy_sources(
    monkeypatch,
    tmp_path,
):
    generator = _load_script(
        "generate_parity_baseline_input_contract",
        PARITY_BASELINE_GENERATOR,
    )
    monkeypatch.setattr(generator, "PROJECT_ROOT", tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "Dockerfile").write_text(
        "COPY backend/app /app/backend/app\nCOPY extra.txt /app/extra.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "app").mkdir(parents=True)
    monkeypatch.setattr(
        generator,
        "PRODUCT_SOURCE_PATH_GROUPS",
        (("deploy/Dockerfile",), ("backend/app",)),
    )

    with pytest.raises(
        generator.BaselineGenerationFailure,
        match="untracked Docker build inputs.*extra.txt",
    ):
        generator._validate_product_source_paths()

    (tmp_path / "deploy" / "Dockerfile").write_text(
        "COPY backend/app /app/backend/app\n",
        encoding="utf-8",
    )
    (tmp_path / "backend" / "app").rmdir()
    with pytest.raises(
        generator.BaselineGenerationFailure,
        match="declared image build input is missing",
    ):
        generator._validate_product_source_paths()


def test_dockerfile_build_context_parser_covers_supported_copy_and_add_forms():
    generator = _load_script(
        "generate_parity_baseline_dockerfile_parser",
        PARITY_BASELINE_GENERATOR,
    )
    context_gate = _load_script(
        "verify_docker_context_dockerfile_parser",
        DOCKER_CONTEXT_GATE,
    )

    dockerfile = """
COPY --chown=app:app pytest.ini /app/pytest.ini
COPY pytest.ini mypy.ini /app/
ADD deploy/entrypoint.sh /app/entrypoint.sh
COPY ["deploy/container_healthcheck.py", "/app/healthcheck.py"]
COPY --from=builder /wheelhouse /wheelhouse
"""

    expected = {
        "pytest.ini",
        "mypy.ini",
        "deploy/entrypoint.sh",
        "deploy/container_healthcheck.py",
    }
    assert generator._dockerfile_source_paths(dockerfile) == expected
    assert context_gate.dockerfile_copy_source_strings(dockerfile) == expected


def test_dockerfile_build_context_parser_handles_continuations_and_rejects_bad_json():
    generator = _load_script(
        "generate_parity_baseline_dockerfile_parser_edges",
        PARITY_BASELINE_GENERATOR,
    )

    assert generator._dockerfile_source_paths(
        "COPY pytest.ini \\\n"
        "             mypy.ini /app/\n"
    ) == {"pytest.ini", "mypy.ini"}

    with pytest.raises(
        generator.BaselineGenerationFailure,
        match="invalid Dockerfile JSON COPY",
    ):
        generator._dockerfile_source_paths(
            'COPY ["pytest.ini", "/app/pytest.ini"\n'
        )


def test_removed_parity_build_flag_has_no_live_documented_consumer():
    live_text_paths = sorted(
        (PROJECT_ROOT / "docs" / "product").rglob("*.md")
    ) + sorted((PROJECT_ROOT / "scripts").rglob("*.md"))

    for path in live_text_paths:
        assert "--build-linux-arm64" not in path.read_text(encoding="utf-8"), (
            f"removed generate_parity_baseline flag remains documented in {path}"
        )


def test_staging_limit_zones_are_keyed_per_client_not_per_site():
    """`NGX-2026-08-07-E-001`. All three zones were keyed on `$server_name`,
    which is constant inside this file, so each held exactly one counter and
    limited the whole site: twelve parallel connections from one visitor got
    four 200s and eight 503s, and any one visitor could lock everyone out.

    Nothing tested the key, only the zone names, which is why it survived. The
    key is the property that matters, so it is the property pinned here."""
    template = _staging_nginx_template()

    zone_directives = [
        line.strip()
        for line in template.splitlines()
        if line.strip().startswith(("limit_req_zone", "limit_conn_zone"))
    ]
    assert zone_directives, "the staging template declares no limit zones"

    for directive in zone_directives:
        key = directive.split()[1]
        assert key == "$binary_remote_addr", (
            f"limit zone keyed on {key}, which does not vary per client: "
            f"{directive}"
        )


def test_staging_site_bounds_place_search_and_hides_the_upstream_server_header():
    """`/api/places/search` is a JSON compute endpoint whose cost varies with
    the submitted terms, but it did not match the chart location regex and so
    inherited the static zone's 120 r/m — the loosest limit in the file — while
    being the most cost-amplifying route measured."""
    template = _staging_nginx_template()

    assert "location ~ ^/api/places/search(?:/|$)" in template, (
        "place search has no dedicated location and falls back to the static zone"
    )
    search_block = template.split("location ~ ^/api/places/search(?:/|$)", 1)[1]
    search_block = search_block.split("location /", 1)[0]
    assert "zone=private_alpha_chart_client" in search_block, (
        "place search is not bounded at the chart rate"
    )
    assert "private_alpha_static_client" not in search_block
    assert "limit_conn private_alpha_connections" in search_block

    assert "proxy_hide_header Server" in template, (
        "the upstream `server: uvicorn` header is not hidden"
    )
