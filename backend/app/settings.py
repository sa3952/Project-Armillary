"""Closed-vocabulary application profiles for local and hosted execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import re
from collections.abc import Mapping


PROFILE_ENVIRONMENT_VARIABLE = "CLASSICAL_ASTROLOGY_PROFILE"
SOURCE_REVISION_ENVIRONMENT_VARIABLE = (
    "CLASSICAL_ASTROLOGY_SOURCE_REVISION"
)
BUILD_ENVIRONMENT_REVISION_SOURCE = (
    "build_environment:CLASSICAL_ASTROLOGY_SOURCE_REVISION"
)
_SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class AppProfile(str, Enum):
    LOCAL = "local"
    PRIVATE_ALPHA = "private_alpha"


@dataclass(frozen=True)
class AppSettings:
    profile: AppProfile
    source_revision: str | None = None
    revision_source: str | None = None
    # CG-11 evidence-retain: test-owned dependency-injection seam.  Tests
    # produce a missing/corrupt catalog path; create_app is the consumer and
    # the hosted unavailable-response regression is the verification entry.
    # Production intentionally leaves this unset so PlaceCatalog selects its
    # bundled immutable default.
    place_catalog_path: str | None = None

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
    def is_private_alpha(self) -> bool:
        return self.profile is AppProfile.PRIVATE_ALPHA

    @property
    def live_openapi_url(self) -> str | None:
        return None if self.is_private_alpha else "/openapi.json"

    @property
    def expose_runtime_health(self) -> bool:
        return not self.is_private_alpha

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
    raw_profile = source.get(PROFILE_ENVIRONMENT_VARIABLE, AppProfile.LOCAL.value)
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
        profile=profile,
        source_revision=source_revision,
        revision_source=revision_source,
    )
