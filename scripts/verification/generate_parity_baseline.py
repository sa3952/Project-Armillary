#!/usr/bin/env python3
"""Regenerate the governed arm64 response baseline from committed backend source."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from scripts.verification import verify_container_runtime as runtime_gate


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "deploy" / "parity-baseline-arm64.json"
PRODUCT_SOURCE_PATHS = ("backend/app", "backend/ephe", "backend/place_data")


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


def _committed_product_revision() -> str:
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
            "backend product source is dirty; commit or separately review "
            "the product change before regenerating parity evidence"
        )
    return revision.stdout.strip()


def build_baseline(
    reason: str,
    *,
    image_name: str,
    build_image: bool,
) -> dict:
    revision = _committed_product_revision()
    payloads = runtime_gate._payloads()
    if build_image:
        runtime_gate._build_image(
            image_name,
            require_clean=False,
            platform="linux/arm64",
        )
    image = runtime_gate._inspect_image(image_name, "linux/arm64")
    controls, responses = runtime_gate._single_worker_parity(
        image_name,
        payloads,
        None,
        "linux/arm64",
    )
    runtime_gate._assert_container_build_identity(
        image["revision"],
        responses,
    )
    schema_versions = sorted(
        {
            response.get("schema_version")
            for response in responses
            if isinstance(response.get("schema_version"), str)
        }
    )
    return {
        "schema_version": "private-alpha-four-mode-parity-baseline-v1",
        "source": {
            "architecture": "arm64",
            "os": "linux",
            "revision": revision,
            "image_id": image["id"],
            "evidence": (
                "governed committed-source linux/arm64 container fixture; "
                "cross-platform container consumer"
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
        "payloads": payloads,
        "responses": responses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--image",
        default="classical-astrology-private-alpha:parity-source-arm64",
    )
    parser.add_argument(
        "--build-linux-arm64",
        action="store_true",
        help="build the named linux/arm64 image before capturing responses",
    )
    args = parser.parse_args()
    try:
        payload = build_baseline(
            args.reason,
            image_name=args.image,
            build_image=args.build_linux_arm64,
        )
        output = args.output.resolve()
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
