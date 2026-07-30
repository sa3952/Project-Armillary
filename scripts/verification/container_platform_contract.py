"""Pure Docker platform parsing for the production runtime gate."""

from __future__ import annotations


def platform_args(platform: str | None) -> list[str]:
    """Return Docker CLI arguments without accepting or normalizing a platform."""
    return ["--platform", platform] if platform else []


def platform_contract(platform: str) -> tuple[str, str]:
    """Validate the exact production platform vocabulary."""
    parts = platform.split("/")
    if len(parts) not in (2, 3) or not all(parts[:2]):
        raise ValueError(f"unsupported Docker platform syntax: {platform}")
    operating_system, architecture = parts[:2]
    if operating_system != "linux" or architecture not in {"amd64", "arm64"}:
        raise ValueError(f"unsupported production platform: {platform}")
    return operating_system, architecture
