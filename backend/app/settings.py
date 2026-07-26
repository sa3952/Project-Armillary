"""Closed-vocabulary application profiles for local and hosted execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from collections.abc import Mapping


PROFILE_ENVIRONMENT_VARIABLE = "CLASSICAL_ASTROLOGY_PROFILE"


class AppProfile(str, Enum):
    LOCAL = "local"
    PRIVATE_ALPHA = "private_alpha"


@dataclass(frozen=True)
class AppSettings:
    profile: AppProfile

    @property
    def is_private_alpha(self) -> bool:
        return self.profile is AppProfile.PRIVATE_ALPHA

    @property
    def live_openapi_url(self) -> str | None:
        return None if self.is_private_alpha else "/openapi.json"

    @property
    def expose_runtime_health(self) -> bool:
        return not self.is_private_alpha


def load_settings(environment: Mapping[str, str] | None = None) -> AppSettings:
    source = os.environ if environment is None else environment
    raw_profile = source.get(PROFILE_ENVIRONMENT_VARIABLE, AppProfile.LOCAL.value)
    try:
        profile = AppProfile(raw_profile)
    except ValueError as error:
        raise RuntimeError(
            f"unsupported application profile: {raw_profile!r}"
        ) from error
    return AppSettings(profile=profile)
