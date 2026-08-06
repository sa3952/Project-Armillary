"""Serve only the closed set of browser runtime assets."""

from __future__ import annotations

import os
from typing import Any, Iterable

from fastapi.staticfiles import StaticFiles

from .frontend_assets import asset_parent_directories, validate_asset_name


class RuntimeStaticFiles(StaticFiles):
    """Reject files that are not declared browser runtime assets.

    The policy remains effective for local source checkouts and for hosted
    immutable frontend releases mounted read-only into the application.
    """

    def __init__(self, *, allowed_assets: Iterable[str], **kwargs: Any) -> None:
        assets = frozenset(validate_asset_name(name) for name in allowed_assets)
        if not assets:
            raise ValueError("runtime frontend allowlist must not be empty")
        self._allowed_paths = assets | asset_parent_directories(assets)
        super().__init__(**kwargs)

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        normalized = path.strip("/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        # StaticFiles only resolves extensionless HTML names after lookup_path.
        # Apply that resolution inside the manifest allowlist boundary so
        # /zh-TW/calculate can be canonical without permitting undeclared files.
        html_candidate = f"{normalized}.html"
        if normalized and html_candidate in self._allowed_paths:
            normalized = html_candidate
            path = html_candidate
        if (
            normalized not in {"", "."}
            and normalized not in self._allowed_paths
        ):
            return "", None
        return super().lookup_path(path)
