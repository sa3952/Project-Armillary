#!/usr/bin/env python3
"""Container-local readiness probe without third-party HTTP clients."""

from __future__ import annotations

import json
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen


def main() -> int:
    request = Request(
        "http://127.0.0.1:8000/api/health",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=2.0) as response:
            payload = json.load(response)
            if response.status != 200:
                return 1
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return 1
    return 0 if payload == {"status": "ok", "ready": True} else 1


if __name__ == "__main__":
    sys.exit(main())
