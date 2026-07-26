#!/usr/bin/env python3
"""Public source delivery gate without private launcher or handoff dependencies."""

from __future__ import annotations

import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path


COMMANDS = (
    [sys.executable, "scripts/verify_publication_candidate.py", "--root", "."],
    [sys.executable, "scripts/verify_privacy_dependencies.py", "--check"],
    [sys.executable, "-m", "compileall", "-q", "backend/app", "scripts"],
    [
        sys.executable,
        "-m",
        "pytest",
        "backend/tests",
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
    [sys.executable, "scripts/verify_publication_candidate.py", "--root", "."],
)

# Extended release gates:
# scripts/verify_container_runtime.py
# scripts/verify_image_supply_chain.py
# scripts/verify_linux_source_build.py


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
