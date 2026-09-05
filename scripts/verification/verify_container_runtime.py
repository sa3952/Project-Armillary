#!/usr/bin/env python3
"""Build and exercise the provider-neutral Private Alpha production image."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import ast
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
import uuid

from scripts.verification.verify_docker_context import (
    materialize_context,
)
from scripts.verification.build_release_image import (
    BUILD_EVIDENCE_PATH,
    CONTEXT_DISCRIMINATION_BYTES,
    CONTEXT_DISCRIMINATION_PATH,
    assert_embedded_contract_consistent,
    assert_context_receipts_match,
    context_manifest,
)
from scripts.verification.container_platform_contract import (
    platform_args as _platform_args,
    platform_contract,
)
from scripts.verification.build_sbom import runtime_distribution_names
from scripts.deployment.frontend_release import (
    build_release as build_frontend_release,
    combined_release_id,
)
from scripts.deployment.verify_staging_http import _request as _direct_request
from scripts.tools.output_confinement import external_output_path
from scripts.tools.closed_set import ClosedSetError, require_closed_set
from scripts.tools.source_tree_identity import materialize_git_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = "classical-astrology-private-alpha:runtime-test"
CANARY = "PRIVATE_ALPHA_BUILD_SECRET_CANARY_8f38f0696df24f8c"
ELF_MACHINE_BY_DOCKER_ARCHITECTURE = {
    "amd64": 62,
    "arm64": 183,
}
MACHINE_BY_DOCKER_ARCHITECTURE = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}
def _privacy_event_fields() -> frozenset[str]:
    """Read the telemetry field set from its sole emitter."""

    source = PROJECT_ROOT / "backend" / "app" / "privacy_logging.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError) as error:
        raise GateFailure(f"cannot read {source}: {error}") from error
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            first = node.targets[0]
            if isinstance(first, ast.Name):
                target, value = first.id, node.value
        if target != "ALLOWED_EVENT_FIELDS" or value is None:
            continue
        try:
            fields = frozenset(ast.literal_eval(value))
        except (ValueError, TypeError) as error:
            raise GateFailure(
                "ALLOWED_EVENT_FIELDS is not a literal this gate can read"
            ) from error
        if not fields or not all(isinstance(name, str) for name in fields):
            raise GateFailure("ALLOWED_EVENT_FIELDS is empty or not a set of names")
        return fields
    raise GateFailure(f"{source} declares no ALLOWED_EVENT_FIELDS")


EXPECTED_PRIVACY_EVENT_FIELDS = _privacy_event_fields()
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
REQUIRED_DEBIAN_PACKAGES = frozenset({"perl-base"})
MAX_BENCHMARK_RESPONSE_BYTES = 512 * 1024
CHART_REQUESTS = (
    PROJECT_ROOT / "frontend" / "tests" / "fixtures" / "chart-requests.json"
)
EXPECTED_IMAGE_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "PYTHON_VERSION": "3.14.7",
    "CLASSICAL_ASTROLOGY_PROFILE": "private_alpha",
    "CLASSICAL_ASTROLOGY_REQUIRE_FRONTEND_RELEASE": "1",
    "PYTHONFAULTHANDLER": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PATH": (
        "/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:"
        "/usr/sbin:/usr/bin:/sbin:/bin"
    ),
}
# Inherited environment is read from the pinned base, not copied here.
DOCKERFILE = PROJECT_ROOT / "deploy" / "Dockerfile"
# The one variable this image deliberately rewrites; its value is checked
# exactly by EXPECTED_IMAGE_ENVIRONMENT.
OVERRIDDEN_BASE_ENVIRONMENT = frozenset({"PATH"})
HIDDEN_RUNTIME_PATHS = ("/openapi.json", "/docs", "/api/runtime-health")


def _debian_package_policy(
    packages: list[str],
) -> tuple[frozenset[str], frozenset[str]]:
    normalized = frozenset(
        package.strip().split(":", maxsplit=1)[0]
        for package in packages
        if package.strip()
    )
    missing = REQUIRED_DEBIAN_PACKAGES - normalized
    forbidden = frozenset(
        package
        for package in normalized
        if package == "perl"
        or package.startswith("perl-") and package != "perl-base"
        or package.startswith("libperl")
        or package.endswith("-perl")
    )
    return missing, forbidden


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


def _copy_build_context(destination: Path, revision: str) -> Path:
    try:
        snapshot = materialize_git_snapshot(
            PROJECT_ROOT, revision, destination / "snapshot" / "source"
        )
    except RuntimeError as error:
        raise GateFailure(str(error)) from None
    context = destination / "context"
    materialize_context(
        snapshot,
        context,
        control_files={
            CONTEXT_DISCRIMINATION_PATH: CONTEXT_DISCRIMINATION_BYTES
        },
    )
    return context


def _platform_contract(platform: str) -> tuple[str, str]:
    try:
        return platform_contract(platform)
    except ValueError as exc:
        raise GateFailure(str(exc)) from exc


_BUILD_INPUT_PATHS = ("deploy", "backend", "scripts", "third_party")


def _assert_evidence_context_matches_image(image_revision: str | None) -> None:
    if not image_revision or not re.fullmatch(r"[0-9a-f]{40}", image_revision):
        raise GateFailure("release evidence requires a full image source revision")
    head = _run(["git", "rev-parse", "HEAD"], check=False).stdout.strip()
    if head != image_revision:
        raise GateFailure(
            "release evidence source differs from image source: "
            f"image={image_revision}, checkout={head}"
        )
    dirty = _run(
        ["git", "status", "--porcelain=v1", "-z", "--", *_BUILD_INPUT_PATHS],
        check=False,
    ).stdout.count("\0")
    if dirty:
        raise GateFailure(
            f"release evidence requires clean build inputs; found {dirty} changed path(s)"
        )


def _image_evidence_json(
    image: str, platform: str | None, filename: str
) -> dict[str, Any]:
    output = _run(
        [
            "docker", "run", "--rm", *_platform_args(platform),
            "--entrypoint", "/bin/cat", image,
            f"{BUILD_EVIDENCE_PATH}/{filename}",
        ]
    ).stdout
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise GateFailure(f"image build evidence is invalid: {filename}") from error
    if not isinstance(value, dict):
        raise GateFailure(f"image build evidence is not an object: {filename}")
    return value


def _exported_release_evidence(
    image: str, platform: str | None, image_revision: str | None
) -> dict[str, Any]:
    """Read receipts embedded by the exact image's own builder transaction."""
    _assert_evidence_context_matches_image(image_revision)
    filenames = {
        "source_build": "pyswisseph-linux-source-build.json",
        "buildkit_probe": "buildkit-probe-consumed.json",
        "buildkit_context": "build-context-received.json",
        "builder_toolchain": "builder-toolchain.json",
        "build_contract": "build-contract.json",
        "native_extensions": "native-extensions.json",
    }
    evidence = {
        key: _image_evidence_json(image, platform, filename)
        for key, filename in filenames.items()
    }
    with tempfile.TemporaryDirectory(prefix="release-evidence-context-") as raw:
        context = _copy_build_context(Path(raw), str(image_revision))
        expected_context = context_manifest(context)
    try:
        assert_context_receipts_match(
            expected_context, evidence["buildkit_context"]
        )
    except Exception as error:
        raise GateFailure(str(error)) from None
    if platform is None:
        raise GateFailure("image build contract verification requires a platform")
    try:
        assert_embedded_contract_consistent(
            contract=evidence["build_contract"],
            context=evidence["buildkit_context"],
            toolchain=evidence["builder_toolchain"],
            revision=str(image_revision),
            platform=platform,
        )
    except Exception as error:
        raise GateFailure(str(error)) from None
    return evidence


def pinned_base_reference(dockerfile: Path = DOCKERFILE) -> str:
    """Name the base this image is built from, read from the build that pins it."""

    try:
        lines = dockerfile.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise GateFailure(f"cannot read {dockerfile}: {error}") from error
    references = [
        line.split()[1]
        for line in lines
        if line.startswith("FROM ") and len(line.split()) > 1
    ]
    runtime = [reference for reference in references if "@sha256:" in reference]
    if not runtime:
        raise GateFailure(f"{dockerfile} pins no base image by digest")
    if len(set(runtime)) != 1:
        raise GateFailure(
            f"{dockerfile} pins more than one base digest: {sorted(set(runtime))}"
        )
    return runtime[0]


def base_environment(platform: str | None) -> dict[str, str]:
    """Read the pinned base environment; absence or unreadability is refusal."""

    reference = pinned_base_reference()
    inspect = ["docker", "image", "inspect", reference]
    try:
        raw = _run(inspect).stdout
    except GateFailure:
        # buildx need not retain the base; the digest keeps a pull exact.
        pull = ["docker", "pull", "--quiet"]
        if platform:
            pull.extend(["--platform", platform])
        _run([*pull, reference])
        raw = _run(inspect).stdout
    try:
        entries = json.loads(raw)[0]["Config"]["Env"] or []
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise GateFailure(
            f"cannot read the pinned base environment from {reference}"
        ) from error
    inherited: dict[str, str] = {}
    for item in entries:
        if not isinstance(item, str) or "=" not in item:
            raise GateFailure("pinned base environment contains an invalid entry")
        key, value = item.split("=", maxsplit=1)
        inherited[key] = value
    if not inherited:
        raise GateFailure(f"pinned base {reference} declares no environment")
    return inherited


def _inspect_image(image: str, platform: str | None) -> dict[str, Any]:
    raw = _run(["docker", "image", "inspect", image]).stdout
    image_info = json.loads(raw)[0]
    config = image_info["Config"]
    if config["User"] != "10001:10001":
        raise GateFailure(f"unexpected image user: {config['User']!r}")
    environment = config.get("Env") or []
    pairs: dict[str, str] = {}
    for item in environment:
        if not isinstance(item, str) or "=" not in item:
            raise GateFailure("image environment contains an invalid entry")
        key, value = item.split("=", maxsplit=1)
        if key in pairs:
            raise GateFailure(f"image environment repeats key: {key}")
        pairs[key] = value
    inherited = base_environment(platform)
    expected_keys = (
        set(EXPECTED_IMAGE_ENVIRONMENT)
        | set(inherited)
        | {"CLASSICAL_ASTROLOGY_SOURCE_REVISION"}
    )
    if set(pairs) != expected_keys:
        raise GateFailure(
            "image environment differs from the closed pinned-base/application "
            f"contract: missing={sorted(expected_keys - set(pairs))} "
            f"unexpected={sorted(set(pairs) - expected_keys)}"
        )
    labels = config.get("Labels", {})
    publication_status = labels.get("org.projectarmillary.publication.status")
    if publication_status not in {
        "provisional_unpublished",
        "published_anonymously_reachable",
    }:
        raise GateFailure("image publication status is absent or invalid")
    exact_values = {
        **EXPECTED_IMAGE_ENVIRONMENT,
        "CLASSICAL_ASTROLOGY_SOURCE_REVISION": labels.get(
            "org.opencontainers.image.revision"
        ),
    }
    for key, expected in exact_values.items():
        if pairs.get(key) != expected:
            raise GateFailure(f"image environment value mismatch: {key}")
    for key, value in inherited.items():
        if key in OVERRIDDEN_BASE_ENVIRONMENT or key in EXPECTED_IMAGE_ENVIRONMENT:
            continue
        if pairs[key] != value:
            raise GateFailure(
                f"inherited {key} differs from the pinned base it came from"
            )
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
        "environment_keys": sorted(pairs),
        "environment_value_sha256": {
            key: hashlib.sha256(value.encode("utf-8")).hexdigest()
            for key, value in sorted(pairs.items())
        },
        "healthcheck": healthcheck,
        "revision": config.get("Labels", {}).get(
            "org.opencontainers.image.revision"
        ),
        "publication_status": publication_status,
    }


# Docker writes these per container, so they differ between two runs of the
# same image and must not enter the content identity.
DOCKER_INJECTED_PATHS = frozenset({"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"})

APP_TREE_MANIFEST = PROJECT_ROOT / "deploy" / "image-app-tree.json"
COMPILED_ARTIFACTS_MANIFEST = PROJECT_ROOT / "deploy" / "image-app-built-extensions.json"
COMPILED_ARTIFACTS_SCHEMA = "image-app-built-extensions-v4"


# Project-built/acquired extensions only; base-image ELF is a separate scope.
COMPILED_ARTIFACT_ROOT = "/opt/venv/"


def _is_compiled_artifact(path: str) -> bool:
    return path.startswith(COMPILED_ARTIFACT_ROOT) and (
        path.endswith(".so") or ".so." in path.rsplit("/", 1)[-1]
    )


CONTENT_IDENTITY_SCHEMA = "content-identity-v2"
def _content_identity_row(
    member: tarfile.TarInfo,
    normalized: str,
    payload_digest: str,
) -> str:
    """Serialize fields that can change an entry's effective content."""

    security_pax = sorted(
        (key, value)
        for key, value in (member.pax_headers or {}).items()
        if key.startswith("SCHILY.xattr.security.")
        or key.startswith("SCHILY.xattr.trusted.")
        or key in {"SCHILY.acl.access", "SCHILY.acl.default"}
    )
    return json.dumps(
        [
            normalized,
            f"{member.mode:04o}",
            member.uid,
            member.gid,
            member.type.decode("ascii"),
            payload_digest,
            member.linkname,
            security_pax,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _assert_safe_regular_mode(member: tarfile.TarInfo, normalized: str) -> None:
    if member.isfile() and member.mode & 0o6000:
        raise GateFailure(
            f"image regular file has setuid/setgid mode: {normalized} "
            f"mode={member.mode:04o}"
        )


def _review_required(manifest: Path, observed: Any, what: str) -> None:
    """Fail closed; never auto-adopt an observed image as expectation."""

    raise GateFailure(
        f"no committed {what} to compare against: {manifest}.\n"
        "Review the observed value below, and commit it to that path only if "
        "every entry is intended:\n"
        + json.dumps(observed, indent=2, sort_keys=True)
    )


def _assert_app_tree_is_expected(observed: list[str]) -> None:
    """Compare the closed `/app` tree with its reviewed manifest."""

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


def _elf_identity(payload: bytes) -> dict[str, Any]:
    if payload[:4] != b"\x7fELF" or payload[4] != 2 or payload[5] not in {1, 2}:
        raise GateFailure("compiled artifact is not a supported ELF64 object")
    endian = "<" if payload[5] == 1 else ">"
    header_format = endian + "HHIQQQIHHHHHH"
    if len(payload) < 16 + struct.calcsize(header_format):
        raise GateFailure("ELF header is truncated")
    header = struct.unpack_from(header_format, payload, 16)
    elf_type, machine, version, entry, program_offset = header[:5]
    flags = header[6]
    program_size, program_count = header[8], header[9]
    section_offset, section_size, section_count, names_index = (
        header[5], header[10], header[11], header[12]
    )

    program_format = endian + "IIQQQQQQ"
    if program_size < struct.calcsize(program_format):
        raise GateFailure("ELF program-header size is unsupported")
    programs = []
    for index in range(program_count):
        position = program_offset + index * program_size
        if position + struct.calcsize(program_format) > len(payload):
            raise GateFailure("ELF program-header table is truncated")
        kind, policy, file_offset, virtual, _physical, file_size, memory_size, alignment = (
            struct.unpack_from(program_format, payload, position)
        )
        programs.append({
            "type": kind,
            "flags": policy,
            "offset": file_offset,
            "virtual_address": virtual,
            "file_size": file_size,
            "memory_size": memory_size,
            "alignment": alignment,
        })
    if not programs:
        raise GateFailure("ELF object has no program headers")

    section_format = endian + "IIQQQQIIQQ"
    if section_size < struct.calcsize(section_format):
        raise GateFailure("ELF section-header size is unsupported")
    sections = []
    for index in range(section_count):
        position = section_offset + index * section_size
        if position + struct.calcsize(section_format) > len(payload):
            raise GateFailure("ELF section-header table is truncated")
        sections.append(struct.unpack_from(section_format, payload, position))
    if names_index >= len(sections):
        raise GateFailure("ELF section-name table is unavailable")
    string_section = sections[names_index]
    names_end = string_section[4] + string_section[5]
    if names_end > len(payload):
        raise GateFailure("ELF section-name table is truncated")
    names = payload[string_section[4]:names_end]
    rows, allocated = [], []
    for section in sections:
        if section[0] >= len(names):
            raise GateFailure("ELF section name points outside its string table")
        end = names.find(b"\0", section[0])
        if end < 0:
            raise GateFailure("ELF section name is unterminated")
        name = names[section[0]:end].decode("utf-8", "replace")
        data_end = section[4] + section[5]
        if section[1] != 8 and data_end > len(payload):
            raise GateFailure(f"ELF section {name!r} is truncated")
        data = (
            b"" if section[1] == 8
            else payload[section[4]:data_end]
        )
        digest = hashlib.sha256(data).hexdigest()
        rows.append({
            "name": name,
            "type": section[1],
            "flags": section[2],
            "size": section[5],
            "sha256": digest,
        })
        if section[2] & 0x2:
            allocated.append({
                "name": name,
                "type": section[1],
                "flags": section[2],
                "address": section[3],
                "offset": section[4],
                "size": section[5],
                "link": section[6],
                "info": section[7],
                "alignment": section[8],
                "entry_size": section[9],
                "content_sha256": (
                    None
                    if name == ".note.gnu.build-id" or name.startswith(".debug")
                    else digest
                ),
            })
    runtime_projection = {
        "elf_header": {
            "class": 64,
            "endianness": "little" if payload[5] == 1 else "big",
            "os_abi": payload[7],
            "abi_version": payload[8],
            "type": elf_type,
            "machine": machine,
            "version": version,
            "entry": entry,
            "flags": flags,
            "program_header_offset": program_offset,
            "program_header_entry_size": program_size,
            "program_header_count": program_count,
        },
        "program_headers": programs,
        "allocated_sections": allocated,
    }
    structural = [{k:v for k,v in row.items() if k != "sha256"} for row in rows]
    canonical = lambda value: json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "elf_class": 64,
        "endianness": "little" if payload[5] == 1 else "big",
        "e_machine": machine,
        "sections": rows,
        "runtime_projection": runtime_projection,
        "structural_sha256": hashlib.sha256(canonical(structural)).hexdigest(),
        "normalized_runtime_sha256": hashlib.sha256(
            canonical(runtime_projection)
        ).hexdigest(),
    }


def _compiled_artifact_discrepancies(
    observed: dict[str, str],
    identities: dict[str, dict[str, Any]],
    architecture: str,
) -> list[dict[str, Any]]:
    """Require a reviewed runtime identity plus current ABI/ELF structure.

    The attestation is over the normalized runtime digest, not the whole file.
    Two hosted builds of the same source produced the same bytes for a
    downloaded wheel and different bytes for the extension built from source,
    while the normalized digest — allocated sections only, without the linker's
    build-id — was identical in both.  A whole-file value therefore cannot be
    attested: recording one would make the next build drift, and a control that
    cries wolf every build is a control people learn to overwrite.  Whole-file
    drift is still reported, as an observation rather than a blocker, so the
    signal is not lost.
    """

    if not COMPILED_ARTIFACTS_MANIFEST.is_file():
        return [{"class":"missing_manifest","candidate_blocker":True}]
    expected = json.loads(COMPILED_ARTIFACTS_MANIFEST.read_text(encoding="utf-8"))
    if expected.get("schema_version") != COMPILED_ARTIFACTS_SCHEMA:
        return [{"class":"invalid_manifest_schema","candidate_blocker":True}]
    by_architecture = expected.get("architectures", {})
    if architecture not in by_architecture:
        return [{"class":"missing_architecture_manifest","architecture":architecture,"candidate_blocker":True}]
    policy = by_architecture[architecture]
    required_modules = set(policy.get("required_modules", []))
    reviewed_runtime = policy.get("reviewed_normalized_runtime_sha256", {})
    reviewed_whole_file = policy.get("reviewed_whole_file_sha256", {})
    matched: set[str] = set()
    discrepancies: list[dict[str, Any]] = []
    for path, digest in sorted(observed.items()):
        module = next(
            (name for name in required_modules if path.endswith("/" + name)),
            None,
        )
        if module is None:
            discrepancies.append({
                "class": "unexpected_compiled_artifact",
                "path": path,
                "candidate_blocker": True,
            })
            continue
        matched.add(module)
        identity = identities.get(path, {})
        if (
            policy.get("python_path_fragment") not in path
            or policy.get("abi_tag") not in path
            or identity.get("elf_class") != 64
            or identity.get("e_machine") != policy.get("elf_e_machine")
        ):
            discrepancies.append({
                "class": "compiled_artifact_structural_mismatch",
                "path": path,
                "candidate_blocker": True,
            })
        runtime_digest = identity.get("normalized_runtime_sha256")
        attested = (
            reviewed_runtime.get(module)
            if isinstance(reviewed_runtime, dict)
            else None
        )
        if not isinstance(attested, str) or not re.fullmatch(
            r"[0-9a-f]{64}", attested
        ):
            discrepancies.append({
                "class": "compiled_artifact_reviewed_hash_missing",
                "path": path,
                "observed_normalized_runtime_sha256": runtime_digest,
                "candidate_blocker": True,
            })
        elif attested != runtime_digest:
            discrepancies.append({
                "class": "normalized_runtime_hash_drift",
                "path": path,
                "expected": attested,
                "observed": runtime_digest,
                "candidate_blocker": True,
            })
        whole_file = (
            reviewed_whole_file.get(module)
            if isinstance(reviewed_whole_file, dict)
            else None
        )
        if isinstance(whole_file, str) and whole_file != digest:
            discrepancies.append({
                "class": "whole_file_hash_drift",
                "path": path,
                "expected": whole_file,
                "observed": digest,
                "reason": (
                    "a source-built extension is not byte-reproducible; the "
                    "runtime identity above is what is attested"
                ),
                "candidate_blocker": False,
            })
    missing = sorted(required_modules - matched)
    if missing:
        discrepancies.append({
            "class": "compiled_artifact_absent",
            "modules": missing,
            "candidate_blocker": True,
        })
    return discrepancies


def _assert_native_extension_receipt_matches_image(
    receipt: dict[str, Any],
    observed: dict[str, str],
) -> None:
    if receipt.get("schema_version") != "builder-native-extensions-v1":
        raise GateFailure("builder native-extension receipt schema is invalid")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        raise GateFailure("builder native-extension receipt is missing artifacts")
    expected: dict[str, str] = {}
    for item in artifacts:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("sha256"), str)
            or item.get("origin_class")
            not in {"source_built_native", "acquired_native_wheel"}
        ):
            raise GateFailure("builder native-extension receipt entry is invalid")
        expected[item["path"]] = item["sha256"]
    try:
        require_closed_set(expected, observed, role="image native-extension paths")
    except ClosedSetError as error:
        raise GateFailure(str(error)) from None
    mismatched = sorted(
        path for path in expected if expected[path] != observed[path]
    )
    if mismatched:
        raise GateFailure(
            f"installed native extensions differ from builder receipt: {mismatched}"
        )


def _inventory_image(image: str, platform: str | None) -> dict[str, Any]:
    inspection = _inspect_image(image, platform)
    image_architecture = str(inspection["architecture"])
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
            compiled_identities: dict[str, dict[str, Any]] = {}
            embedded_toolchain: dict[str, Any] | None = None
            canary_found = False
            with tarfile.open(archive_path, mode="r") as archive:
                for member in archive:
                    normalized = "/" + member.name.lstrip("./")
                    _assert_safe_regular_mode(member, normalized)
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
                            if _is_compiled_artifact(normalized):
                                payload = extracted.read(); extracted = None
                                payload_digest = hashlib.sha256(payload).hexdigest()
                                compiled_artifacts[normalized] = payload_digest
                                compiled_identities[normalized] = _elf_identity(payload)
                                canary_found = canary_found or CANARY.encode() in payload
                            elif normalized == "/usr/local/share/project-armillary/build-evidence/builder-toolchain.json":
                                payload = extracted.read(); extracted = None
                                payload_digest = hashlib.sha256(payload).hexdigest()
                                embedded_toolchain = json.loads(payload)
                            digest = hashlib.sha256()
                            canary_bytes = CANARY.encode()
                            canary_tail = b""
                            while extracted is not None:
                                chunk = extracted.read(1024 * 1024)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                if canary_bytes in canary_tail + chunk:
                                    canary_found = True
                                canary_tail = (canary_tail + chunk)[
                                    -max(len(canary_bytes) - 1, 0):
                                ]
                            if extracted is not None:
                                payload_digest = digest.hexdigest()
                    if normalized not in DOCKER_INJECTED_PATHS:
                        content_identity_rows.append(
                            _content_identity_row(
                                member,
                                normalized,
                                payload_digest,
                            )
                        )
            if canary_found:
                raise GateFailure("image filesystem contains the secret canary")
            content_identity = hashlib.sha256(
                "\n".join(sorted(content_identity_rows)).encode("utf-8")
            ).hexdigest()
            _assert_app_tree_is_expected(sorted(app_entries))
            compiled_discrepancies = _compiled_artifact_discrepancies(
                compiled_artifacts,
                compiled_identities,
                image_architecture,
            )
    finally:
        _run(["docker", "rm", "--force", container], check=False)

    debian_output = _run(
        [
            "docker", "run", "--rm", *_platform_args(platform),
            "--entrypoint", "/usr/bin/dpkg-query", image,
            "--show", "--showformat=${binary:Package}\\n",
        ]
    ).stdout
    debian_packages = debian_output.splitlines()
    missing_debian, forbidden_debian = _debian_package_policy(debian_packages)
    if missing_debian or forbidden_debian:
        raise GateFailure(
            "Debian Perl package policy mismatch: "
            f"missing={sorted(missing_debian)}, "
            f"forbidden={sorted(forbidden_debian)}"
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
                "import importlib.metadata,json,platform,re;"
                "print(json.dumps({"
                "'packages':sorted({re.sub(r'[-_.]+','-',d.metadata['Name']).lower() "
                "for d in importlib.metadata.distributions()}),"
                "'python':platform.python_version(),"
                "'machine':platform.machine(),"
                "'pyswisseph':importlib.metadata.version('pyswisseph')}))"
            ),
        ]
    ).stdout
    runtime = json.loads(runtime_output.strip().splitlines()[-1])
    packages = frozenset(runtime.pop("packages"))
    missing = runtime_distribution_names(image_architecture) - packages
    forbidden = FORBIDDEN_PACKAGES & packages
    if missing or forbidden:
        raise GateFailure(
            f"production package inventory mismatch: "
            f"missing={sorted(missing)}, forbidden={sorted(forbidden)}"
        )
    runtime.update(
        _exported_release_evidence(
            image, platform, inspection["revision"]
        )
    )
    _assert_native_extension_receipt_matches_image(
        runtime["native_extensions"],
        compiled_artifacts,
    )
    source_build = runtime["source_build"]
    expected_machine = MACHINE_BY_DOCKER_ARCHITECTURE.get(
        image_architecture
    )
    expected_elf_machine = ELF_MACHINE_BY_DOCKER_ARCHITECTURE.get(
        image_architecture
    )
    if (
        runtime["python"] != "3.14.7"
        or runtime["pyswisseph"] != "2.10.3.2"
        or runtime.get("machine") != expected_machine
        or source_build.get("schema_version")
        != "pyswisseph-linux-source-build-v1"
        or source_build.get("machine") != expected_machine
        or source_build.get("source", {}).get("sha256")
        != "c54c305e83dbd5d2b71e58d8a69d8ee41de24c4d3328ce09e2af860a3537624d"
        or source_build.get("build_binding", {}).get("method")
        != "direct_file_url_with_sha256_fragment"
        or source_build.get("build_binding", {}).get("source_requirement")
        != (
            "file:///source/pyswisseph-2.10.3.2.tar.gz#sha256="
            "c54c305e83dbd5d2b71e58d8a69d8ee41de24c4d3328ce09e2af860a3537624d"
        )
        or source_build.get("wheel", {}).get("extension_format") != "ELF"
        or source_build.get("wheel", {}).get("elf_e_machine")
        != expected_elf_machine
        or runtime.get("buildkit_probe", {}).get("consumed") is not True
        or runtime.get("buildkit_probe", {}).get("nonempty") is not True
        or len(str(runtime.get("buildkit_probe", {}).get("secret_sha256", "")))
        != 64
    ):
        raise GateFailure(
            "Linux source-build, architecture, BuildKit probe, or runtime "
            "version mismatch"
        )
    return {
        "rootfs_entries": len(names),
        "app_tree_entries": len(app_entries),
        # Stable effective-content identity; Docker-injected paths are excluded.
        "content_identity_schema": CONTENT_IDENTITY_SCHEMA,
        "content_identity": content_identity,
        "compiled_artifacts": dict(sorted(compiled_artifacts.items())),
        "compiled_artifact_identities": dict(sorted(compiled_identities.items())),
        "embedded_toolchain": embedded_toolchain,
        "discrepancies": compiled_discrepancies,
        "production_packages": sorted(packages),
        "forbidden_packages_present": sorted(forbidden),
        "debian_packages": sorted(debian_packages),
        "forbidden_nonessential_perl_packages": sorted(forbidden_debian),
        "secret_canary_present": False,
        "runtime": runtime,
    }


def _request_fixture(name: str) -> dict[str, Any]:
    fixtures = json.loads(CHART_REQUESTS.read_text(encoding="utf-8"))
    payload = fixtures.get(name)
    if not isinstance(payload, dict):
        raise GateFailure(f"chart request fixture is unavailable: {name}")
    return payload


def _payloads() -> list[dict[str, Any]]:
    payload = _request_fixture("chart-sidereal-dignity-refused.json")
    payload["computation_mode"].update(
        center="barycentric", ayanamsa="hipparchos"
    )
    payload["options"] = {"include_fixed_stars": True}
    return [payload]


def _worst_options_payload() -> dict[str, Any]:
    payload = _request_fixture("chart-all-modules.json")
    payload["options"].update(
        declination_aspect_orb_degrees=0.5,
        aspect_angle_orb_degrees=2.0,
        bounds_profile="chaldaean_bounds_ptolemy_i_21_v1",
        decan_profile="chaldean_planetary_faces_firmicus_ii_4_v1",
        triplicity_profile="ptolemy_triplicity_textual_corulership_v1",
    )
    return payload


def _count_named_fields(value: object, name: str) -> int:
    if isinstance(value, dict):
        return int(name in value) + sum(
            _count_named_fields(item, name) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_count_named_fields(item, name) for item in value)
    return 0


def _timed_chart(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    status, body, _ = _http(
        base_url,
        "/api/chart",
        body=json.dumps(payload, separators=(",", ":")).encode(),
        content_type="application/json",
    )
    elapsed = time.monotonic() - started
    if status != 200:
        raise GateFailure(f"worst-options request failed with HTTP {status}")
    if len(body) > MAX_BENCHMARK_RESPONSE_BYTES:
        raise GateFailure("worst-options response exceeded the release byte budget")
    document = json.loads(body)
    return {
        "status": status,
        "elapsed_seconds": round(elapsed, 6),
        "response_bytes": len(body),
        "bisection_receipts": _count_named_fields(
            document,
            "bisection_iterations",
        ),
    }


def _cgroup_metrics(container: str) -> dict[str, int]:
    code = (
        "import json;from pathlib import Path;"
        "paths={'memory_current':'/sys/fs/cgroup/memory.current',"
        "'memory_peak':'/sys/fs/cgroup/memory.peak'};"
        "result={k:int(Path(v).read_text().strip()) for k,v in paths.items()};"
        "cpu=dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines());"
        "result['cpu_usage_usec']=int(cpu['usage_usec']);"
        "print(json.dumps(result,sort_keys=True))"
    )
    output = _run([
        "docker", "exec", container, "/opt/venv/bin/python", "-c", code,
    ]).stdout
    try:
        metrics = json.loads(output.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise GateFailure("container cgroup metrics are unreadable") from error
    if set(metrics) != {"memory_current", "memory_peak", "cpu_usage_usec"} or any(
        type(value) is not int or value < 0 for value in metrics.values()
    ):
        raise GateFailure("container cgroup metrics are invalid")
    return metrics


def _worst_options_observation(container: str, base_url: str) -> dict[str, Any]:
    payload = _worst_options_payload()
    before = _cgroup_metrics(container)
    sequential = [_timed_chart(base_url, payload) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent = list(executor.map(lambda _: _timed_chart(base_url, payload), range(2)))
    after = _cgroup_metrics(container)
    resources = {
        "cpu_usage_usec_delta": max(
            0, after["cpu_usage_usec"] - before["cpu_usage_usec"]
        ),
        "memory_current_before": before["memory_current"],
        "memory_current_after": after["memory_current"],
        "memory_peak_after": after["memory_peak"],
    }
    return {
        "claim_boundary": "local exact-image observation; not a general SLA",
        "payload_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "sequential": sequential,
        "concurrent": concurrent,
        "resources": resources,
        "response_byte_budget": MAX_BENCHMARK_RESPONSE_BYTES,
    }


def _http(
    base_url: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 60,
) -> tuple[int, bytes, dict[str, str]]:
    try:
        status, payload, headers = _direct_request(
            f"{base_url}{path}",
            body=body,
            content_type=content_type,
            extra_headers={"Accept": "application/json"},
            timeout=timeout,
            maximum_bytes=MAX_BENCHMARK_RESPONSE_BYTES,
        )
    except (OSError, TimeoutError, ValueError) as error:
        raise GateFailure(f"HTTP request failed: {error.__class__.__name__}") from error
    if status == 0:
        raise GateFailure("HTTP request failed: endpoint unavailable")
    return status, payload, headers


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
        "docker", "run",
        *_platform_args(platform),
        "--detach", "--name", container,
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "64", "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1", "--log-driver", "local",
        "--log-opt", "max-size=5m", "--log-opt", "max-file=2",
        "--publish", "127.0.0.1::8000", "--mount",
        (
            "type=bind,src=" + str(built["release_directory"])
            + ",dst=/app/frontend,readonly"
        ),
        "--env", "CLASSICAL_ASTROLOGY_REQUIRE_FRONTEND_RELEASE=1",
        "--env", "CLASSICAL_ASTROLOGY_FRONTEND_ROOT=/app/frontend",
        "--env", f"CLASSICAL_ASTROLOGY_FRONTEND_RELEASE_DIGEST={frontend_digest}",
        "--env", f"CLASSICAL_ASTROLOGY_BACKEND_IMAGE_ID={image_id}",
        "--env", f"CLASSICAL_ASTROLOGY_COMBINED_RELEASE_ID={combined}",
        image, "/opt/venv/bin/python", "-m", "uvicorn", "app.main:create_app",
        "--factory", "--app-dir", "/app/backend", "--host", "0.0.0.0",
        "--port", "8000", "--workers", str(workers),
        "--loop", "asyncio", "--http", "h11", "--ws", "none",
        "--no-access-log", "--timeout-worker-healthcheck", "5",
        "--timeout-graceful-shutdown", "10", "--backlog", "32",
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
    if status != 200 or json.loads(body) != {
        "status": "ok",
        "ready": True,
        "readiness_scope": "process_liveness_only",
    }:
        raise GateFailure("hosted readiness endpoint contract failed")
    normalized_headers = {key.casefold(): value for key, value in headers.items()}
    if (
        normalized_headers.get("x-robots-tag")
        != "noindex, nofollow, noarchive"
    ):
        raise GateFailure("hosted noindex response header is absent")
    for hidden_path in HIDDEN_RUNTIME_PATHS:
        hidden_status, _, _ = _http(base_url, hidden_path)
        if hidden_status != 404:
            raise GateFailure(f"hosted endpoint remained visible: {hidden_path}")
    request_cases = {
        "unsupported_media_type": (b"{}", "text/plain"),
        "oversized": (b" " * (16 * 1024 + 1), "application/json"),
        "malformed": (b'{"datetime":', "application/json"),
    }
    statuses = {
        name: _http(
            base_url, "/api/chart", body=body, content_type=content_type
        )[0]
        for name, (body, content_type) in request_cases.items()
    }
    if (
        statuses["unsupported_media_type"] != 415
        or statuses["oversized"] != 413
        or statuses["malformed"] >= 500
    ):
        raise GateFailure(f"request boundary mismatch: {statuses}")
    return {
        "uid": int(uid),
        "rootfs_write_rejected": True,
        "tmpfs_write_accepted": True,
        "health": "ready",
        "hidden_endpoints": list(HIDDEN_RUNTIME_PATHS),
        "request_boundary_statuses": statuses,
    }


def _privacy_canaries(payloads: list[dict[str, Any]]) -> frozenset[str]:
    canaries: set[str] = set()
    serialized = json.dumps(payloads, sort_keys=True, separators=(",", ":"))
    for payload in payloads:
        datetime_ = payload.get("datetime", {})
        location = payload.get("location", {})
        for value in (
            datetime_.get("year"),
            location.get("latitude"),
            location.get("longitude"),
        ):
            if isinstance(value, (int, float)):
                rendered = str(value)
                if rendered not in serialized:
                    raise GateFailure("privacy canary is absent from the sent payload")
                canaries.add(rendered)
    if len(canaries) < 3:
        raise GateFailure("actual chart requests provide no complete privacy canary set")
    return frozenset(canaries)


def _verify_container_logs(
    logs: str,
    sensitive_values: frozenset[str],
) -> dict[str, Any]:
    for forbidden in (
        '"POST /api/chart HTTP/',
        CANARY,
        *sorted(sensitive_values),
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


def _single_worker_acceptance(
    image: str,
    payloads: list[dict[str, Any]],
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
                container_results.append(result)
            except GateFailure as exc:
                raise GateFailure(
                    f"single-worker case {case_index}: {exc}"
                ) from exc
        worst_options = _worst_options_observation(container, base_url)
        logs = (
            _run(["docker", "logs", container], check=False).stdout
            + _run(["docker", "logs", container], check=False).stderr
        )
        log_controls = _verify_container_logs(
            logs,
            _privacy_canaries(payloads),
        )
        return {
            "calculation_cases": len(payloads),
            "runtime_smoke": "one_nondefault_synthetic_chart",
            "controls": controls,
            "log_controls": log_controls,
            "worst_options": worst_options,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--platform",
        help="Docker target platform, for example linux/amd64",
    )
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="emit only a closed summary; retain full JSON in --receipt",
    )
    args = parser.parse_args()

    started = time.monotonic()
    try:
        image = _inspect_image(args.image, args.platform)
        inventory = _inventory_image(args.image, args.platform)
        payloads = _payloads()
        stage_discrepancies: list[dict[str, Any]] = []
        try:
            single_worker, container_results = _single_worker_acceptance(
                args.image, payloads, args.platform,
            )
            _assert_container_build_identity(image["revision"], container_results)
        except (GateFailure, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            single_worker = {"status": "failed", "error": str(exc)}
            stage_discrepancies.append(
                {"stage": "single_worker", "candidate_blocker": True, "error": str(exc)}
            )
        receipt = {
            "schema_version": "private-alpha-artifact-acceptance-v1",
            "image": image,
            "inventory": inventory,
            "single_worker": single_worker,
            "stage_discrepancies": stage_discrepancies,
            "verdict_scope": "artifact_identity_and_single_worker_runtime",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        if args.receipt is not None:
            receipt_path = external_output_path(
                args.receipt,
                source_root=PROJECT_ROOT,
                role="container runtime receipt",
            )
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        blockers = stage_discrepancies + [
            item
            for item in inventory.get("discrepancies", [])
            if item.get("candidate_blocker") is True
        ]
        if blockers:
            print(json.dumps(receipt, indent=2, sort_keys=True))
            raise GateFailure(
                "container checks completed with candidate-blocking discrepancies: "
                + json.dumps(blockers, sort_keys=True)
            )
        if args.quiet:
            print(
                "OK: artifact_identity_and_single_worker_runtime "
                f"({len(payloads)} modes, architecture={image['architecture']})"
            )
        else:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except (GateFailure, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
