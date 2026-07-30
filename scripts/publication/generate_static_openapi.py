#!/usr/bin/env python3
"""Generate the private-alpha OpenAPI schema as an offline release artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
# The backend package is not installed in the repository development environment.
# This release-artifact entry therefore exposes that one package root explicitly.
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.main import create_app  # noqa: E402
from app.settings import AppProfile, AppSettings  # noqa: E402


def write_static_openapi(output: Path) -> None:
    app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    serialized = json.dumps(
        app.openapi(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    write_static_openapi(arguments.output)


if __name__ == "__main__":
    main()
