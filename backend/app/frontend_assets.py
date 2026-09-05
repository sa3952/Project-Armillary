"""Closed source discovery and path validation for frontend releases."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat


MANIFEST_NAME = "frontend-release.json"
DEFAULT_LOCALE_ENTRYPOINT = "zh-TW/index.html"
# Top-level files that live in frontend/ for tooling reasons and must never
# become runtime assets.  `.json` and `.mjs` are both legitimate browser asset
# suffixes, so the suffix allowlist alone does not stop them: adding
# package.json for ESLint on 2026-08-06 silently took the release from 24 to
# 28 assets, which would have shipped dev tooling and changed the release
# digest.  node_modules is excluded by directory below.
EXCLUDED_SOURCE_BASENAMES = frozenset({
    "README.md",
    "eslint.config.mjs",
    "package-lock.json",
    "package.json",
    "surfaces.json",
})
IGNORED_PLATFORM_METADATA = frozenset({".DS_Store"})
EXCLUDED_SOURCE_DIRECTORIES = frozenset({"tests", "node_modules"})
RUNTIME_SUFFIXES = frozenset({
    ".avif",
    ".css",
    ".gif",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mjs",
    ".otf",
    ".png",
    ".svg",
    ".ttf",
    ".txt",
    ".webp",
    ".woff",
    ".woff2",
    ".xml",
})
_INDEXNOW_KEY = re.compile(r"^[A-Za-z0-9-]{8,128}$")


def _discovery_asset_is_allowed(entry: Path, *, at_root: bool) -> bool:
    suffix = entry.suffix.casefold()
    if suffix not in {".txt", ".xml"}:
        return True
    if not at_root:
        return False
    if entry.name == "robots.txt":
        return True
    if entry.name == "sitemap.xml":
        return True
    if suffix == ".txt" and _INDEXNOW_KEY.fullmatch(entry.stem):
        try:
            return entry.read_text(encoding="ascii").strip() == entry.stem
        except (OSError, UnicodeError):
            return False
    return False


def validate_asset_name(name: object) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("frontend asset name is not a safe relative POSIX path")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
        or name == MANIFEST_NAME
    ):
        raise ValueError("frontend asset name is not a safe relative POSIX path")
    return name


def discover_source_assets(frontend_directory: Path) -> frozenset[str]:
    frontend = Path(os.path.abspath(frontend_directory))
    metadata = frontend.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or frontend.is_symlink():
        raise ValueError("frontend source must be a non-symlink directory")
    assets: set[str] = set()

    def collect(directory: Path, relative_parent: PurePosixPath | None = None) -> None:
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = (
                PurePosixPath(entry.name)
                if relative_parent is None
                else relative_parent / entry.name
            )
            entry_metadata = entry.lstat()
            if entry.is_symlink():
                raise ValueError(f"unsafe frontend source entry: {relative}")
            if stat.S_ISDIR(entry_metadata.st_mode):
                if relative_parent is None and entry.name in EXCLUDED_SOURCE_DIRECTORIES:
                    continue
                if entry.name.startswith("."):
                    raise ValueError(f"unsafe frontend source entry: {relative}")
                collect(entry, relative)
                continue
            if not stat.S_ISREG(entry_metadata.st_mode):
                raise ValueError(f"unsafe frontend source entry: {relative}")
            if entry.name in IGNORED_PLATFORM_METADATA:
                continue
            if relative_parent is None and entry.name in EXCLUDED_SOURCE_BASENAMES:
                continue
            if entry.name.startswith("."):
                raise ValueError(f"unsafe frontend source entry: {relative}")
            name = validate_asset_name(relative.as_posix())
            if entry.suffix.casefold() not in RUNTIME_SUFFIXES:
                raise ValueError(f"unsupported frontend asset: {name}")
            if not _discovery_asset_is_allowed(
                entry,
                at_root=relative_parent is None,
            ):
                raise ValueError(f"unsupported frontend discovery asset: {name}")
            assets.add(name)
    collect(frontend)
    if DEFAULT_LOCALE_ENTRYPOINT not in assets:
        raise ValueError(
            f"frontend release requires {DEFAULT_LOCALE_ENTRYPOINT}"
        )
    return frozenset(assets)


def asset_parent_directories(assets: set[str] | frozenset[str]) -> frozenset[str]:
    parents: set[str] = set()
    for name in assets:
        path = PurePosixPath(validate_asset_name(name))
        for parent in path.parents:
            if parent != PurePosixPath("."):
                parents.add(parent.as_posix())
    return frozenset(parents)
