from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import tarfile
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
    "python:3.14.7-slim-trixie"
    "@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6"
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
        "pydantic==2.13.5",
        "starlette==1.6.0",
        "uvicorn==0.52.4",
        "pyswisseph==2.10.3.2",
    }


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
    member.linkname = overrides.pop("linkname", "python3.14")
    member.pax_headers = overrides.pop("pax_headers", {})
    payload_digest = overrides.pop("payload_digest", "")
    assert not overrides, f"unused overrides: {sorted(overrides)}"
    return gate._content_identity_row(member, member.name, payload_digest)


def test_content_identity_covers_runtime_relevant_metadata_only():
    baseline = _content_identity_row()
    for changed in (
        _content_identity_row(linkname="/tmp/attacker"),
        _content_identity_row(mode=0o4755),
        _content_identity_row(uid=10001),
        _content_identity_row(gid=10001),
        _content_identity_row(name="/usr/local/bin/python"),
        _content_identity_row(payload_digest="deadbeef"),
        _content_identity_row(pax_headers={"SCHILY.xattr.security.capability": "cap_net_raw+ep"}),
    ):
        assert changed != baseline
    assert _content_identity_row(pax_headers={"mtime": "1"}) == baseline
    assert _content_identity_row(
        pax_headers={"SCHILY.xattr.security.a": "x;SCHILY.xattr.security.b=y"}
    ) != _content_identity_row(
        pax_headers={"SCHILY.xattr.security.a": "x", "SCHILY.xattr.security.b": "y"}
    )




def test_docker_context_gate_enforces_closed_inventory_and_200_mib_cap():
    module = _load_script("docker_context_gate", DOCKER_CONTEXT_GATE)

    receipt = module.verify(PROJECT_ROOT)

    assert receipt["policy"] == "closed_required_paths"
    assert receipt["maximum_size_bytes"] == 200 * 1024 * 1024
    assert receipt["context_size_bytes"] < receipt["maximum_size_bytes"]
    assert receipt["forbidden_paths_present"] == []


def test_image_environment_uses_closed_keys_and_value_provenance(monkeypatch):
    module = _load_script("verify_image_environment_contract", CONTAINER_GATE)
    revision = "a" * 40
    environment = [
        "PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG=C.UTF-8",
        "PYTHON_VERSION=3.14.7",
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
        # The base is a different object from the image built on it; conflating
        # them is what let a requirement for a variable nothing sets survive.
        monkeypatch.setattr(
            module,
            "base_environment",
            lambda _platform: {
                "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:"
                        "/usr/sbin:/usr/bin:/sbin:/bin",
                "PYTHON_VERSION": "3.14.7",
                "PYTHON_SHA256": "b" * 64,
            },
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
    with pytest.raises(module.GateFailure, match="missing=.*PYTHON_SHA256"):
        inspect_with([
            item for item in environment if not item.startswith("PYTHON_SHA256=")
        ])
    with pytest.raises(module.GateFailure, match="inherited PYTHON_SHA256 differs"):
        inspect_with([
            "PYTHON_SHA256=" + "c" * 64,
            *[item for item in environment if not item.startswith("PYTHON_SHA256=")],
        ])


def test_runtime_keeps_essential_perl_base_and_rejects_nonessential_perl():
    module = _load_script("verify_image_debian_package_policy", CONTAINER_GATE)

    assert module._debian_package_policy(
        ["base-files", "perl-base:amd64", "python-runtime"]
    ) == (frozenset(), frozenset())
    missing, forbidden = module._debian_package_policy(
        ["perl", "perl-modules-5.40", "libperl5.40:amd64", "foo-perl"]
    )
    assert missing == {"perl-base"}
    assert forbidden == {
        "perl", "perl-modules-5.40", "libperl5.40", "foo-perl"
    }


def test_exact_image_gate_owns_one_bounded_worst_options_observation(monkeypatch):
    module = _load_script("verify_image_worst_options", CONTAINER_GATE)
    from app.schemas import ChartRequest

    payload = module._worst_options_payload()
    request = ChartRequest.model_validate(payload)
    assert request.options.include_aspect_perfection is True
    assert request.options.include_eclipses is True
    assert request.options.include_rise_set_transits is True
    assert request.options.triplicity_include_research_comparison is True
    assert module.MAX_BENCHMARK_RESPONSE_BYTES == 512 * 1024

    sample = {
        "a": {"bisection_iterations": 3},
        "b": [{"bisection_iterations": 5}, {"other": 1}],
    }
    assert module._count_named_fields(sample, "bisection_iterations") == 2
    metrics = iter((
        {"cpu_usage_usec": 100, "memory_current": 200, "memory_peak": 300},
        {"cpu_usage_usec": 175, "memory_current": 240, "memory_peak": 340},
    ))
    monkeypatch.setattr(module, "_cgroup_metrics", lambda _container: next(metrics))
    monkeypatch.setattr(
        module, "_timed_chart", lambda *_args: {"status": 200}
    )
    observed = module._worst_options_observation("container", "http://fixture")
    assert observed["resources"] == {
        "cpu_usage_usec_delta": 75,
        "memory_current_before": 200,
        "memory_current_after": 240,
        "memory_peak_after": 340,
    }


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


def test_image_inventory_rejects_setid_regular_files():
    module = _load_script("verify_container_setid", CONTAINER_GATE)
    member = tarfile.TarInfo("app/payload")
    member.type = tarfile.REGTYPE
    member.mode = 0o4755
    with pytest.raises(module.GateFailure, match="setuid/setgid"):
        module._assert_safe_regular_mode(member, "/app/payload")

    member.mode = 0o755
    module._assert_safe_regular_mode(member, "/app/payload")


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


def test_container_log_gate_accepts_only_closed_chart_telemetry():
    module = _load_script(
        "verify_container_runtime_log_contract",
        CONTAINER_GATE,
    )
    # Built from the emitter's own declaration rather than typed again here.
    # A copy of this dict, written when the schema had ten fields, is how the
    # image came to fail its own telemetry check.
    emitter = importlib.import_module("app.privacy_logging")
    values = {
        "event_schema_version": emitter.EVENT_SCHEMA_VERSION,
        "event": "http_request_completed",
        "request_id": "0" * 32,
        "route": "/api/chart",
        "method": "POST",
        "status_code": 200,
        "duration_bucket": "50_to_249ms",
        "request_size_bucket": "1_to_1024b",
        "outcome": "success",
        "error_code": None,
        "failure_class": None,
    }
    assert set(values) == set(emitter.ALLOWED_EVENT_FIELDS), (
        "this fixture omits a field the emitter declares"
    )
    safe_event = {name: values[name] for name in emitter.ALLOWED_EVENT_FIELDS}

    result = module._verify_container_logs(
        "PRIVACY_EVENT " + json.dumps(safe_event, separators=(",", ":")),
        frozenset({"1990", "24.1477", "120.6736"}),
    )
    assert result["chart_status_only_event_present"] is True
    assert result["raw_chart_access_log_absent"] is True

    with pytest.raises(module.GateFailure, match="disclosed"):
        module._verify_container_logs(
            '"POST /api/chart HTTP/1.1" 200\n'
            + "PRIVACY_EVENT " + json.dumps(safe_event),
            frozenset({"1990", "24.1477", "120.6736"}),
        )

    unsafe_event = {**safe_event, "unexpected": "outside-closed-schema"}
    with pytest.raises(module.GateFailure, match="closed schema"):
        module._verify_container_logs(
            "PRIVACY_EVENT " + json.dumps(unsafe_event),
            frozenset({"1990", "24.1477", "120.6736"}),
        )


def test_container_privacy_canaries_come_from_actual_request_payload():
    module = _load_script("verify_container_privacy_canary", CONTAINER_GATE)
    payload = {
        "datetime": {"year": 1990},
        "location": {"latitude": 24.1477, "longitude": 120.6736},
    }
    assert module._privacy_canaries([payload]) == frozenset({
        "1990", "24.1477", "120.6736",
    })
    with pytest.raises(module.GateFailure, match="complete privacy canary"):
        module._privacy_canaries([{"datetime": {"year": 1990}}])


    # This portable test owns the public runtime contract: the shipped Compose
    # definition and the behaviour it declares.  Provider-specific operator
    # state, host locators and credentials are outside its scope and are not
    # part of what it asserts.


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


def test_supply_chain_inventory_rejects_missing_or_wrong_audit_evidence(tmp_path):
    module = _load_script("verify_supply_chain_versions", SUPPLY_CHAIN_GATE)
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "fastapi==0.1 \\\n"
        f"    --hash=sha256:{'a' * 64}\n"
        "uvicorn==0.2 \\\n"
        f"    --hash=sha256:{'b' * 64}\n",
        encoding="utf-8",
    )
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
            lock,
        )
    with pytest.raises(module.AuditFailure, match="expected set is empty|differs"):
        module._verify_python_versions(sbom, [], lock)
    assert module._verify_python_versions(
        sbom,
        [
            {"name": "fastapi", "version": "0.1"},
            {"name": "uvicorn", "version": "0.2"},
        ],
        lock,
    ) == {"fastapi": "0.1", "uvicorn": "0.2"}


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


def test_staging_limit_zones_are_keyed_per_client_not_per_site():
    """A limit zone bounds whatever its key varies over.  Keyed on a value
    that is constant inside the template it would bound the whole site instead
    of one client, which is a different control from the one the template
    claims to declare.  Zone names carry no such guarantee, so the key is the
    property pinned here."""
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


def _synthetic_elf(needed: tuple[str, ...]) -> bytes:
    """Build an ELF64 dynamic table naming `needed` for a portable control."""
    header_size, program_entry, program_count = 64, 56, 2
    program_offset = header_size
    dynamic_offset = program_offset + program_entry * program_count

    strings = b"\x00"
    offsets = []
    for name in needed:
        offsets.append(len(strings))
        strings += name.encode("ascii") + b"\x00"

    entries = [(1, offset) for offset in offsets]
    string_offset = dynamic_offset + 16 * (len(entries) + 3)
    entries += [(5, string_offset), (10, len(strings)), (0, 0)]
    dynamic = b"".join(
        tag.to_bytes(8, "little") + value.to_bytes(8, "little")
        for tag, value in entries
    )
    total = string_offset + len(strings)

    def program_header(kind: int, offset: int, size: int) -> bytes:
        return (
            kind.to_bytes(4, "little")
            + (4).to_bytes(4, "little")
            + offset.to_bytes(8, "little")
            + offset.to_bytes(8, "little")
            + offset.to_bytes(8, "little")
            + size.to_bytes(8, "little")
            + size.to_bytes(8, "little")
            + (8).to_bytes(8, "little")
        )

    identity = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    head = (
        identity
        + (3).to_bytes(2, "little")
        + (62).to_bytes(2, "little")
        + (1).to_bytes(4, "little")
        + (0).to_bytes(8, "little")
        + program_offset.to_bytes(8, "little")
        + (0).to_bytes(8, "little")
        + (0).to_bytes(4, "little")
        + header_size.to_bytes(2, "little")
        + program_entry.to_bytes(2, "little")
        + program_count.to_bytes(2, "little")
        + (0).to_bytes(6, "little")
    )
    assert len(head) == header_size
    body = (
        program_header(1, 0, total)
        + program_header(2, dynamic_offset, len(dynamic))
        + dynamic
    )
    blob = head + body
    return blob + b"\x00" * (string_offset - len(blob)) + strings


def _source_build_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_linux_source_build",
        SOURCE_BUILD_VERIFIER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_extension_reader_names_the_libraries_the_object_records():
    verifier = _source_build_verifier()

    assert verifier.elf_dynamic_needed(
        _synthetic_elf(("libstdc++.so.6", "libc.so.6"))
    ) == ("libc.so.6", "libstdc++.so.6")
    assert verifier.elf_dynamic_needed(_synthetic_elf(("libc.so.6",))) == (
        "libc.so.6",
    )


@pytest.mark.parametrize(
    "blob",
    [
        b"\x7fELF" + b"\x00" * 8,
        b"MZ\x90\x00" + b"\x00" * 128,
        b"\x7fELF\x02\x09\x01" + b"\x00" * 128,
    ],
)
def test_an_extension_the_reader_cannot_read_is_refused_not_reported_empty(blob):
    """An empty result would let a missing dependency read as a present one."""
    verifier = _source_build_verifier()

    with pytest.raises(RuntimeError):
        verifier.elf_dynamic_needed(blob)


def _runtime_gate():
    spec = importlib.util.spec_from_file_location(
        "verify_container_runtime",
        CONTAINER_GATE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_base_the_runtime_inherits_from_is_read_from_the_build_that_pins_it():
    """The Dockerfile's unique pinned digest owns base identity."""
    gate = _runtime_gate()

    reference = gate.pinned_base_reference()

    assert "@sha256:" in reference
    assert reference in DOCKERFILE.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("dockerfile", "message"),
    [
        ("FROM python:3.14-slim AS builder\nFROM python:3.14-slim\n", "pins no base"),
        (
            "FROM a@sha256:" + "a" * 64 + "\nFROM b@sha256:" + "b" * 64 + "\n",
            "more than one base digest",
        ),
    ],
)
def test_a_build_that_does_not_pin_one_base_is_refused(tmp_path, dockerfile, message):
    gate = _runtime_gate()
    candidate = tmp_path / "Dockerfile"
    candidate.write_text(dockerfile, encoding="utf-8")

    with pytest.raises(gate.GateFailure, match=message):
        gate.pinned_base_reference(candidate)


def test_a_base_the_daemon_has_not_got_is_fetched_by_digest_not_assumed(monkeypatch):
    """A missing local base is fetched by its exact digest."""
    gate = _runtime_gate()
    calls: list[list[str]] = []
    config = json.dumps([{"Config": {"Env": ["PYTHON_VERSION=3.14.7"]}}])

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"] and len(calls) == 1:
            raise gate.GateFailure("No such image")
        return SimpleNamespace(stdout=config)

    monkeypatch.setattr(gate, "_run", fake_run)

    assert gate.base_environment("linux/amd64") == {"PYTHON_VERSION": "3.14.7"}
    assert [command[1] for command in calls] == ["image", "pull", "image"]
    assert "--platform" in calls[0] and "linux/amd64" in calls[0]
    assert "--platform" in calls[1] and "linux/amd64" in calls[1]


def test_a_base_that_cannot_be_fetched_is_refused(monkeypatch):
    gate = _runtime_gate()

    def always_fail(command, **_kwargs):
        raise gate.GateFailure("No such image")

    monkeypatch.setattr(gate, "_run", always_fail)

    with pytest.raises(gate.GateFailure):
        gate.base_environment("linux/amd64")


def test_a_base_that_declares_no_environment_is_refused(monkeypatch):
    gate = _runtime_gate()
    monkeypatch.setattr(
        gate,
        "_run",
        lambda *_a, **_k: SimpleNamespace(stdout=json.dumps([{"Config": {"Env": []}}])),
    )

    with pytest.raises(gate.GateFailure, match="declares no environment"):
        gate.base_environment(None)


def test_the_image_telemetry_check_reads_the_field_set_from_the_emitter():
    """The image gate reads telemetry shape from its sole emitter."""
    gate = _runtime_gate()
    emitter = importlib.import_module("app.privacy_logging")

    assert gate.EXPECTED_PRIVACY_EVENT_FIELDS == frozenset(
        emitter.ALLOWED_EVENT_FIELDS
    )
    assert "failure_class" in gate.EXPECTED_PRIVACY_EVENT_FIELDS
