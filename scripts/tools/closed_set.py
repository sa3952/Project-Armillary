"""Fail-closed comparison for independently produced finite universes."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable


class ClosedSetError(ValueError):
    pass


@dataclass(frozen=True)
class ClosedSetResult:
    expected_count: int
    observed_count: int


def _items(values: Iterable[str], *, role: str) -> tuple[str, ...]:
    items = tuple(values)
    if any(not isinstance(item, str) or not item for item in items):
        raise ClosedSetError(f"{role} contains an invalid identity")
    if len(set(items)) != len(items):
        raise ClosedSetError(f"{role} contains duplicate identities")
    return items


def require_closed_set(
    expected: Iterable[str],
    observed: Iterable[str],
    *,
    role: str,
    allow_empty: bool = False,
) -> ClosedSetResult:
    """Require two caller-produced universes to be equal.

    The caller owns provenance and enumeration.  This primitive deliberately
    does not discover files, parse evidence or guess whether emptiness is
    meaningful.
    """

    expected_items = _items(expected, role=f"{role} expected set")
    observed_items = _items(observed, role=f"{role} observed set")
    if not expected_items and not allow_empty:
        raise ClosedSetError(f"{role} expected set is empty")
    missing = sorted(set(expected_items) - set(observed_items))
    unexpected = sorted(set(observed_items) - set(expected_items))
    if missing or unexpected:
        raise ClosedSetError(
            f"{role} differs: missing={missing!r} unexpected={unexpected!r}"
        )
    return ClosedSetResult(len(expected_items), len(observed_items))
