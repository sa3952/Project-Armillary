"""Closed-vocabulary capabilities for the supported web profiles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import re
from collections.abc import Mapping
from typing import Literal


PROFILE_ENVIRONMENT_VARIABLE = "CLASSICAL_ASTROLOGY_PROFILE"
# The host this deployment answers for.  Admission lives in the proxy, which
# strips Authorization before forwarding, so the application cannot see the
# credential and has no authorization of its own.  That is a deliberate
# topology; this is an accident catcher for the one-line mistake that would
# undo it — publishing a container port — not an authorization boundary, since
# a caller who can reach the socket can also set this header.
#
# Unset means no check: the same module runs in development and in tests, and
# refusing to start there would put the obligation on module import rather than
# on the deployment.  The deployment is where it belongs, and the staging
# configuration verifier requires it there.
EXPECTED_HOST_ENVIRONMENT_VARIABLE = "CLASSICAL_ASTROLOGY_EXPECTED_HOST"
SOURCE_REVISION_ENVIRONMENT_VARIABLE = (
    "CLASSICAL_ASTROLOGY_SOURCE_REVISION"
)
BUILD_ENVIRONMENT_REVISION_SOURCE = (
    "build_environment:CLASSICAL_ASTROLOGY_SOURCE_REVISION"
)
_SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class AppProfile(str, Enum):
    PRIVATE_ALPHA = "private_alpha"
    PUBLIC = "public"


@dataclass(frozen=True)
class ProfileCapabilities:
    emit_noindex: bool
    cache_policy: Literal["no_store", "public_split"]


_PROFILE_CAPABILITIES = {
    AppProfile.PRIVATE_ALPHA: ProfileCapabilities(
        emit_noindex=True,
        cache_policy="no_store",
    ),
    AppProfile.PUBLIC: ProfileCapabilities(
        emit_noindex=False,
        cache_policy="public_split",
    ),
}


@dataclass(frozen=True)
class AppSettings:
    profile: AppProfile
    source_revision: str | None = None
    revision_source: str | None = None
    # Test-owned dependency-injection seam.  Tests
    # produce a missing/corrupt catalog path; create_app is the consumer and
    # the hosted unavailable-response regression is the verification entry.
    # Production intentionally leaves this unset so PlaceCatalog selects its
    # bundled immutable default.
    place_catalog_path: str | None = None
    expected_host: str | None = None

    def __post_init__(self) -> None:
        if self.source_revision is None:
            if self.revision_source is not None:
                raise RuntimeError(
                    "revision_source requires an available source revision"
                )
            return
        if not _SOURCE_REVISION_PATTERN.fullmatch(self.source_revision):
            raise RuntimeError(
                "source revision must be exactly 40 lowercase hexadecimal "
                "characters"
            )
        if self.revision_source != BUILD_ENVIRONMENT_REVISION_SOURCE:
            raise RuntimeError(
                "source revision must identify its controlled build source"
            )

    @property
    def capabilities(self) -> ProfileCapabilities:
        return _PROFILE_CAPABILITIES[self.profile]

    @property
    def build_identity(self) -> dict:
        return {
            "status": (
                "available"
                if self.source_revision is not None
                else "unavailable"
            ),
            "source_revision": self.source_revision,
            "revision_source": self.revision_source,
        }


def load_settings(environment: Mapping[str, str] | None = None) -> AppSettings:
    source = os.environ if environment is None else environment
    expected_host = (source.get(EXPECTED_HOST_ENVIRONMENT_VARIABLE) or "").strip()
    raw_profile = source.get(
        PROFILE_ENVIRONMENT_VARIABLE, AppProfile.PRIVATE_ALPHA.value
    )
    try:
        profile = AppProfile(raw_profile)
    except ValueError as error:
        raise RuntimeError(
            f"unsupported application profile: {raw_profile!r}"
        ) from error

    raw_revision = source.get(SOURCE_REVISION_ENVIRONMENT_VARIABLE)
    if raw_revision in (None, "", "uncommitted"):
        source_revision = None
        revision_source = None
    elif _SOURCE_REVISION_PATTERN.fullmatch(raw_revision):
        source_revision = raw_revision
        revision_source = BUILD_ENVIRONMENT_REVISION_SOURCE
    else:
        raise RuntimeError(
            "source revision must be 'uncommitted' or exactly 40 lowercase "
            "hexadecimal characters"
        )

    return AppSettings(
        expected_host=expected_host or None,
        profile=profile,
        source_revision=source_revision,
        revision_source=revision_source,
    )
