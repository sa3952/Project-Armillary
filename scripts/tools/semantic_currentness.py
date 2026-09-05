"""Bounded protected-semantic comparison shared by parity and fixture consumers."""
from __future__ import annotations

from typing import Any


_EXACT_KEYS = frozenset({
    "authority",
    "code",
    "dossier_version",
    "formula",
    "method",
    "reason_code",
    "schema_version",
    "source",
    "status",
    "title",
})
_SUFFIXES = ("_authority", "_code", "_id", "_method", "_source", "_status", "_version")
_RUNTIME_PREFIXES = (
    "$.calculation_dossier.build_identity",
    "$.calculation_dossier.engine.tz_database",
    "$.library_info.tz_database",
)


def _protected_key(key: str) -> bool:
    return key in _EXACT_KEYS or key.endswith(_SUFFIXES)


def protected_semantic_values(value: Any, path: str = "$") -> dict[str, str]:
    """Return user/contract semantic strings, excluding named runtime identity axes."""

    if any(path == prefix or path.startswith(prefix + ".") for prefix in _RUNTIME_PREFIXES):
        return {}
    found: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if isinstance(item, str) and _protected_key(key):
                found[child] = item
            else:
                found.update(protected_semantic_values(item, child))
    elif (
        isinstance(value, list)
        and path == "$.calculation_dossier.warnings"
        and all(isinstance(item, dict) and isinstance(item.get("code"), str) for item in value)
    ):
        for item in value:
            found.update(protected_semantic_values(
                item,
                f"{path}[code={item['code']},source={item.get('source')}]",
            ))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(protected_semantic_values(item, f"{path}[{index}]"))
    return found


def protected_semantic_mismatches(
    expected: Any,
    actual: Any,
) -> list[dict[str, str | None]]:
    expected_values = protected_semantic_values(expected)
    actual_values = protected_semantic_values(actual)
    return [
        {
                "path": path,
                "expected": expected_values.get(path),
                "actual": actual_values.get(path),
        }
        for path in sorted(set(expected_values) | set(actual_values))
        if expected_values.get(path) != actual_values.get(path)
    ]
