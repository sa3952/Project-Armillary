#!/usr/bin/env python3
"""Public source delivery gate for the closed publication tree."""

from __future__ import annotations

import os
import json
import re
import subprocess
import sys
import argparse
from tempfile import TemporaryDirectory
from pathlib import Path

# Keep the documented ``python scripts/verify_delivery.py`` entry portable and
# self-clean: direct execution otherwise exposes only ``scripts/`` on sys.path
# and can write bytecode before the gate creates its child-process sandbox.
if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.tools.source_tree_identity import observe_source_tree


# The public command is documented and tested from the candidate root.  Its
# source path differs before/after export, so deriving the root from __file__
# would bind the private overlay location rather than the candidate checkout.
SOURCE_ROOT = Path.cwd().resolve()


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
        "-p",
        "no:cacheprovider",
        "-p",
        "anyio.pytest_plugin",
        "-p",
        "_hypothesis_pytestplugin",
        # Every published test runs.  There is no exclusion list: pytest ignores
        # a --deselect for a node that does not exist, so such a list fails open
        # and stops describing the tree the moment a test is renamed.
        "tests",
        "-q",
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
    try:
        if (SOURCE_ROOT / ".git").exists():
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=SOURCE_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if status:
                raise RuntimeError("source tree is not clean")
        observed = observe_source_tree(SOURCE_ROOT)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"PUBLIC DELIVERY FAILED: {error}") from None
    return observed.paths, observed.content_sha256, observed.index_sha256


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
        embedded_witness = json.loads(
            subprocess.run(
                [
                    "docker", "run", "--rm", "--entrypoint", "/bin/cat",
                    image,
                    "/usr/local/share/project-armillary/build-evidence/"
                    "buildkit-witness-consumed.json",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
    except (OSError, ValueError, IndexError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"PUBLIC DELIVERY FAILED: invalid build transaction: {error}")
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != "release-build-transaction-v2"
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
        or receipt.get("build_witness_plaintext_present_in_log_or_runtime")
        is not False
        or re.fullmatch(
            r"[0-9a-f]{64}", str(receipt.get("build_witness_sha256", ""))
        ) is None
        or embedded_witness != {
            "consumed": True,
            "nonempty": True,
            "witness_classification": "generated_noncredential_build_witness",
            "witness_sha256": receipt.get("build_witness_sha256"),
        }
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
            "--image", image,
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
        child_environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
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
        exit_code = _run_release_artifact_gates(
            image=args.release_image,
            build_evidence_dir=args.release_build_evidence_dir,
            output_dir=args.release_output_dir,
        )
        if exit_code:
            return exit_code
    if _source_identity() != source_identity_before:
        raise SystemExit(
            "PUBLIC DELIVERY FAILED: source identity changed during delivery"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
