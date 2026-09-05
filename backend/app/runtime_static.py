"""Serve only the closed set of browser runtime assets."""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse, Response
from starlette.types import Scope

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
        self._assets = assets
        self._allowed_paths = assets | asset_parent_directories(assets)
        super().__init__(**kwargs)

    def _canonical_redirect(self, scope: Scope) -> str | None:
        if scope.get("method") not in {"GET", "HEAD"}:
            return None
        original = scope.get("path")
        if not isinstance(original, str) or not original.startswith("/"):
            return None
        path = re.sub(r"/{2,}", "/", original)
        target: str | None
        if path == "/zh-TW" and "zh-TW/index.html" in self._assets:
            target = "/zh-TW/"
        elif path == "/zh-TW/index.html" and "zh-TW/index.html" in self._assets:
            target = "/zh-TW/"
        elif path.endswith(".html") and path.lstrip("/") in self._assets:
            target = path[:-5]
        elif path.endswith("/") and path != "/zh-TW/":
            candidate = path.rstrip("/")
            target = (
                candidate
                if f"{candidate.lstrip('/')}.html" in self._assets
                else None
            )
        elif path != original and (
            path == "/zh-TW/"
            or f"{path.lstrip('/')}.html" in self._assets
            or path.lstrip("/") in self._assets
        ):
            target = path
        else:
            target = None
        if target is None:
            return None
        query = scope.get("query_string", b"")
        # A conforming HTTP server rejects a raw non-ASCII request target, so
        # this held in practice; it held because of somebody else's guarantee.
        # An ASGI caller that is not one raised here and became a 500.
        return target + (
            f"?{query.decode('ascii', errors='replace')}" if query else ""
        )

    async def get_response(self, path: str, scope: Scope) -> Response:
        redirect = self._canonical_redirect(scope)
        if redirect is not None:
            return RedirectResponse(redirect, status_code=308)
        return await super().get_response(path, scope)

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
