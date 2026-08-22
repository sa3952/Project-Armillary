#!/usr/bin/env python3
"""Public source delivery gate without private launcher or handoff dependencies."""

from __future__ import annotations

import os
import hashlib
import json
import subprocess
import sys
import argparse
from tempfile import TemporaryDirectory
from pathlib import Path


# The public command is documented and tested from the candidate root.  Its
# source path differs before/after export, so deriving the root from __file__
# would bind the private overlay location rather than the candidate checkout.
SOURCE_ROOT = Path.cwd().resolve()


# These tests assert private-repository layout that is not part of the exported
# Corresponding Source candidate.  The same nodes remain mandatory in the
# private delivery gate; public delivery excludes only the explicitly named
# nodes instead of suppressing the whole module.
PRIVATE_REPOSITORY_TEST_NODES = (
    "tests/deployment/test_production_runtime_contract.py::"
    "test_image_carries_licence_notices_and_both_dataset_verifiers",
    "tests/deployment/test_production_runtime_contract.py::"
    "test_staging_site_bounds_place_search_and_hides_the_upstream_server_header",
)


COMMANDS = (
    [
        sys.executable,
        "-m",
        "scripts.publication.verify_publication_candidate",
        "--root",
        ".",
    ],
    [
        sys.executable,
        "-m",
        "scripts.verification.verify_privacy_dependencies",
        "--check",
    ],
    [sys.executable, "-m", "compileall", "-q", "backend/app", "scripts"],
    [
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "-q",
        "-p",
        "no:cacheprovider",
        *[
            argument
            for node in PRIVATE_REPOSITORY_TEST_NODES
            for argument in ("--deselect", node)
        ],
    ],
    [
        "node",
        "--test",
        *[
            path.as_posix()
            for path in sorted(Path("frontend/tests").glob("*.test.cjs"))
        ],
    ],
    [
        sys.executable,
        "-m",
        "scripts.publication.verify_publication_candidate",
        "--root",
        ".",
    ],
)

RELEASE_ARTIFACT_GATES = (
    "scripts.verification.verify_container_runtime",
    "scripts.publication.verify_image_supply_chain",
)


def _external_output_path(path: Path) -> Path:
    output = path.resolve()
    root = SOURCE_ROOT.resolve()
    if output == root or root in output.parents:
        raise SystemExit(
            "PUBLIC DELIVERY FAILED: release output must be outside source"
        )
    return output


def _source_identity() -> tuple[tuple[str, ...], str, str]:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=SOURCE_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise SystemExit("PUBLIC DELIVERY FAILED: source tree is not clean")
    paths_raw = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=SOURCE_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    paths = tuple(sorted(raw.decode() for raw in paths_raw.split(b"\0") if raw))
    content = hashlib.sha256()
    for relative in paths:
        content.update(relative.encode())
        content.update(b"\0")
        content.update(hashlib.sha256((SOURCE_ROOT / relative).read_bytes()).digest())
    index = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=SOURCE_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return paths, content.hexdigest(), hashlib.sha256(index).hexdigest()


def _frontend_tests() -> list[str]:
    paths = [
        path.as_posix()
        for path in sorted(Path("frontend/tests").glob("*.test.cjs"))
    ]
    if not paths:
        raise SystemExit("PUBLIC DELIVERY FAILED: frontend test universe is empty")
    return paths


def _validate_build_transaction(*, image: str, evidence_dir: Path) -> dict:
    evidence_dir = _external_output_path(evidence_dir)
    try:
        receipt = json.loads(
            (evidence_dir / "build-transaction.json").read_text(encoding="utf-8")
        )
        actual = json.loads(
            subprocess.run(
                ["docker", "image", "inspect", image],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )[0]
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=SOURCE_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, ValueError, IndexError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"PUBLIC DELIVERY FAILED: invalid build transaction: {error}")
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != "release-build-transaction-v1"
        or receipt.get("purpose") != "release-candidate"
        or receipt.get("publication_status")
        != "published_anonymously_reachable"
        or receipt.get("source_revision") != revision
        or receipt.get("image") != image
        or receipt.get("image_id") != actual.get("Id")
        or not isinstance(receipt.get("builder_isolation"), dict)
        or receipt["builder_isolation"].get("mode")
        != "ephemeral_docker_container_builder_no_cache"
        or not receipt["builder_isolation"].get("builder_name")
        or receipt.get("source_identity_unchanged") is not True
        or receipt.get("secret_present_in_log_or_runtime") is not False
        or receipt.get("materialized_context_identity_sha256")
        != receipt.get("buildkit_context_identity_sha256")
    ):
        raise SystemExit(
            "PUBLIC DELIVERY FAILED: build transaction is not bound to this "
            "published revision and runtime image"
        )
    return receipt


def _run_release_artifact_gates(
    *, image: str, build_evidence_dir: Path, output_dir: Path
) -> int:
    _validate_build_transaction(image=image, evidence_dir=build_evidence_dir)
    output_dir = _external_output_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    commands = (
        [
            sys.executable, "-m", "scripts.verification.verify_container_runtime",
            "--image", image, "--container-only",
            "--receipt", str(output_dir / "container-runtime.json"),
        ],
        [
            sys.executable, "-m", "scripts.publication.verify_image_supply_chain",
            "--image", image, "--output-dir", str(output_dir / "supply-chain"),
            "--receipt", str(output_dir / "image-supply-chain.json"),
        ],
    )
    for command in commands:
        result = subprocess.run(command, check=False, shell=False)
        if result.returncode != 0:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-image")
    parser.add_argument("--release-build-evidence-dir", type=Path)
    parser.add_argument("--release-output-dir", type=Path)
    args = parser.parse_args()
    release_values = (
        args.release_image,
        args.release_build_evidence_dir,
        args.release_output_dir,
    )
    if any(release_values) and not all(release_values):
        parser.error("release-artifact mode requires all three --release-* arguments")

    source_identity_before = _source_identity()
    # Resolve the universe before any command runs so a missing test directory
    # cannot be mistaken for a successful Node invocation.
    frontend_tests = _frontend_tests()
    with TemporaryDirectory(prefix="public-source-pycache-") as pycache:
        child_environment = os.environ.copy()
        child_environment["PYTHONPYCACHEPREFIX"] = pycache
        child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for command in COMMANDS:
            if command[:2] == ["node", "--test"]:
                command = ["node", "--test", *frontend_tests]
            result = subprocess.run(
                command,
                check=False,
                env=child_environment,
                shell=False,
            )
            if result.returncode != 0:
                return result.returncode
    if all(release_values):
        result = _run_release_artifact_gates(
            image=args.release_image,
            build_evidence_dir=args.release_build_evidence_dir,
            output_dir=args.release_output_dir,
        )
        if result:
            return result
    if _source_identity() != source_identity_before:
        raise SystemExit(
            "PUBLIC DELIVERY FAILED: source identity changed during delivery"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
