#!/usr/bin/env python3
"""Generate a platform-bound parity baseline from an exact public checkout."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

from scripts.verification import verify_container_runtime as runtime_gate
from scripts.verification.verify_docker_context import (
    DockerContextFailure,
    dockerfile_copy_source_strings,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_PLATFORMS = ("linux/amd64", "linux/arm64")
PRODUCT_SOURCE_PATH_GROUPS = (
    (".dockerignore",),
    ("LICENSE",),
    (
        "THIRD_PARTY_NOTICES.md",
        "publication/public_overlay/THIRD_PARTY_NOTICES.md",
    ),
    ("backend/app",),
    ("backend/ephe",),
    ("backend/place_data",),
    ("deploy/Dockerfile",),
    ("deploy/requirements.lock",),
    ("deploy/build-requirements.lock",),
    ("deploy/ephemeris.sha256",),
    ("deploy/frontend-contract.json",),
    ("deploy/entrypoint.sh",),
    ("deploy/container_healthcheck.py",),
    ("third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz",),
    ("scripts/publication/verify_linux_source_build.py",),
    ("scripts/verification/verify_ephemeris_integrity.py",),
    ("scripts/verification/verify_place_catalog.py",),
    ("scripts/verification/capture_build_evidence.py",),
)
PRODUCT_SOURCE_PATHS = tuple(
    path for group in PRODUCT_SOURCE_PATH_GROUPS for path in group
)


class BaselineGenerationFailure(RuntimeError):
    """Raised when the fixture cannot be tied to committed product source."""


def _git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _dockerfile_source_paths(dockerfile: str) -> set[str]:
    try:
        copied = dockerfile_copy_source_strings(dockerfile)
    except DockerContextFailure as error:
        raise BaselineGenerationFailure(str(error)) from error
    mounted = set(
        re.findall(
            r"/build-context/([A-Za-z0-9_./-]+)",
            dockerfile,
        )
    )
    return copied | mounted


def _validate_product_source_paths() -> None:
    missing_groups = [
        group
        for group in PRODUCT_SOURCE_PATH_GROUPS
        if not any((PROJECT_ROOT / path).exists() for path in group)
    ]
    if missing_groups:
        raise BaselineGenerationFailure(
            "declared image build input is missing: "
            + ", ".join(" or ".join(group) for group in missing_groups)
        )
    dockerfile = (PROJECT_ROOT / "deploy" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    untracked = sorted(
        _dockerfile_source_paths(dockerfile) - set(PRODUCT_SOURCE_PATHS)
    )
    if untracked:
        raise BaselineGenerationFailure(
            "untracked Docker build inputs: " + ", ".join(untracked)
        )


def _committed_product_revision() -> str:
    _validate_product_source_paths()
    revision = _git("rev-parse", "HEAD")
    if revision.returncode:
        raise BaselineGenerationFailure(
            revision.stderr.strip() or "cannot resolve Git HEAD"
        )
    dirty = _git(
        "status",
        "--porcelain=v1",
        "--",
        *PRODUCT_SOURCE_PATHS,
    )
    if dirty.returncode:
        raise BaselineGenerationFailure(
            dirty.stderr.strip() or "cannot inspect product source"
        )
    if dirty.stdout.strip():
        raise BaselineGenerationFailure(
            "numeric image build inputs are dirty; commit or separately "
            "review them before regenerating parity evidence"
        )
    return revision.stdout.strip()


def build_baseline(
    reason: str,
    *,
    image_name: str,
    platform: str,
    public_source_revision: str,
    publication_receipt: Path,
    build_evidence_dir: Path,
) -> dict:
    revision = _committed_product_revision()
    if platform not in SUPPORTED_PLATFORMS:
        raise BaselineGenerationFailure(
            f"unsupported parity platform: {platform}"
        )
    architecture = platform.split("/", 1)[1]
    if public_source_revision != revision:
        raise BaselineGenerationFailure(
            "public source revision must equal the exact checkout HEAD"
        )
    payloads = runtime_gate._payloads()
    runtime_gate._build_image(
        image_name,
        require_clean=True,
        platform=platform,
        purpose="release-candidate",
        publication_receipt=publication_receipt,
        evidence_dir=build_evidence_dir,
    )
    image = runtime_gate._inspect_image(image_name, platform)
    image_revision = image.get("revision")
    if image_revision != revision:
        raise BaselineGenerationFailure(
            "the image was not built from this working tree: "
            f"image={image_revision!r}, HEAD={revision!r}. "
            "Build the image from this checkout or check out the revision "
            "declared by the image."
        )
    controls, responses = runtime_gate._single_worker_parity(
        image_name,
        payloads,
        None,
        platform,
    )
    runtime_gate._assert_container_build_identity(
        image["revision"],
        responses,
    )
    schema_versions = sorted(
        {
            # Bound once so the isinstance narrowing reaches the element. Calling
            # .get() twice left the set typed as Any | None even though the guard
            # already excluded None.
            version
            for response in responses
            if isinstance(version := response.get("schema_version"), str)
        }
    )
    return {
        "schema_version": "private-alpha-platform-parity-baseline-v2",
        "source": {
            "platform": platform,
            "architecture": architecture,
            "os": "linux",
            "revision": revision,
            "public_source_revision": public_source_revision,
            "image_id": image["id"],
            "evidence": (
                "governed materialized-context container fixture from the "
                "exact public checkout"
            ),
            "reason": reason,
        },
        "producer": {
            "module": "scripts.verification.generate_parity_baseline",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "product_source_paths": list(PRODUCT_SOURCE_PATHS),
            "product_source_dirty": False,
            "container_controls": controls,
        },
        "response_schema_versions": schema_versions,
        # Travels with the file, because the file is published on its own and a
        # provenance note kept anywhere else would not reach its readers.
        "payload_provenance": runtime_gate.PAYLOAD_PROVENANCE,
        "payloads": payloads,
        "responses": responses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--platform",
        choices=SUPPORTED_PLATFORMS,
        required=True,
    )
    parser.add_argument(
        "--public-source-revision",
        required=True,
        help="exact 40-character revision of the public checkout",
    )
    parser.add_argument(
        "--image",
        default="classical-astrology-private-alpha:parity-source-arm64",
    )
    parser.add_argument("--publication-receipt", type=Path, required=True)
    parser.add_argument("--build-evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = build_baseline(
            args.reason,
            image_name=args.image,
            platform=args.platform,
            public_source_revision=args.public_source_revision,
            publication_receipt=args.publication_receipt,
            build_evidence_dir=args.build_evidence_dir,
        )
        architecture = args.platform.split("/", 1)[1]
        output = (
            args.output
            if args.output is not None
            else PROJECT_ROOT / "deploy" / f"parity-baseline-{architecture}.json"
        ).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    except (BaselineGenerationFailure, OSError) as exc:
        print(f"PARITY BASELINE GENERATION FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "PARITY BASELINE GENERATED "
        f"responses={len(payload['responses'])} "
        f"revision={payload['source']['revision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
