"""Serve only the closed set of browser runtime assets."""

from __future__ import annotations

import os
from typing import Final

from fastapi.staticfiles import StaticFiles


RUNTIME_FRONTEND_ASSETS: Final = frozenset(
    {
        "app.js",
        "client-context.js",
        "exporters.js",
        "favicon.svg",
        "index.html",
        "privacy-lifecycle.js",
        "style.css",
    }
)


class RuntimeStaticFiles(StaticFiles):
    """Reject source-checkout files that are not browser runtime assets.

    Docker context filtering remains a supply-chain control, but serving
    policy must hold independently when the local launcher uses a source
    checkout or a future deployment bind-mounts the frontend directory.
    """

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        normalized = path.strip("/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if (
            normalized not in {"", "."}
            and normalized not in RUNTIME_FRONTEND_ASSETS
        ):
            return "", None
        return super().lookup_path(path)
