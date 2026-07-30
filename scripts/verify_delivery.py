#!/usr/bin/env python3
"""Public source delivery gate without private launcher or handoff dependencies."""

from __future__ import annotations

import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path


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
    "scripts.publication.verify_linux_source_build",
)


def main() -> int:
    with TemporaryDirectory(prefix="public-source-pycache-") as pycache:
        child_environment = os.environ.copy()
        child_environment["PYTHONPYCACHEPREFIX"] = pycache
        child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for command in COMMANDS:
            result = subprocess.run(
                command,
                check=False,
                env=child_environment,
                shell=False,
            )
            if result.returncode != 0:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
