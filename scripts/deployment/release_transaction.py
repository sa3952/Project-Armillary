#!/usr/bin/env python3
"""Pure activation and rollback state transitions."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Callable

PRE_ACTIVATION_STATES = (
    "installed_unexposed",
    "serviceable",
    "activated",
)
SERVICEABILITY_CHECKS = frozenset({
    "process_liveness",
    "synthetic_chart_full_ephemeris",
    "synthetic_place_search",
    "release_identity",
    "privacy",
})


def _candidate_identity(candidate: dict) -> dict:
    backend = candidate.get("backend") or candidate
    return {
        "image_id": backend.get("image_id"),
        "vcs_revision": backend.get("vcs_revision"),
        "combined_release_id": candidate.get("combined_release_id"),
    }


def build_pre_activation_receipt(candidate: dict, observation: object) -> dict:
    """Turn actual unexposed probes into the state that controls activation."""

    if not isinstance(observation, dict) or set(observation) != {
        "traffic_exposed", "checks"
    }:
        raise ValueError("pre-activation observation is invalid")
    if observation.get("traffic_exposed") is not False:
        raise ValueError("candidate is exposed before serviceability")
    checks = observation.get("checks")
    if (
        not isinstance(checks, dict)
        or set(checks) != SERVICEABILITY_CHECKS
        or not all(value is True for value in checks.values())
    ):
        raise ValueError("pre-activation serviceability is incomplete")
    return {
        "schema_version": "pre-activation-serviceability-v1",
        "candidate": _candidate_identity(candidate),
        "states": [
            {"state": "installed_unexposed", "traffic_exposed": False},
            {"state": "serviceable", "checks": dict(checks)},
        ],
    }


def complete_activation_receipt(receipt: dict) -> dict:
    """Record activation only after the caller has performed it."""

    states = receipt.get("states") if isinstance(receipt, dict) else None
    if not isinstance(states, list) or [
        item.get("state") if isinstance(item, dict) else None
        for item in states
    ] != list(PRE_ACTIVATION_STATES[:2]):
        raise ValueError("candidate was not serviceable before activation")
    return {**receipt, "states": [*states, {"state": "activated"}]}


def validate_pre_activation_receipt(
    receipt: object,
    expected_candidate: dict,
) -> dict:
    """Validate the transaction state consumed later by Gate E."""

    expected_identity = _candidate_identity(expected_candidate)
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema_version", "candidate", "states"}
        or receipt.get("schema_version") != "pre-activation-serviceability-v1"
        or receipt.get("candidate") != expected_identity
    ):
        raise ValueError("pre-activation receipt identity is invalid")
    states = receipt.get("states")
    if not isinstance(states, list) or [
        item.get("state") if isinstance(item, dict) else None
        for item in states
    ] != list(PRE_ACTIVATION_STATES):
        raise ValueError("pre-activation state order is invalid")
    installed, serviceable, activated = states
    if installed != {
        "state": "installed_unexposed",
        "traffic_exposed": False,
    }:
        raise ValueError("candidate was not installed in an unexposed state")
    checks = serviceable.get("checks")
    if (
        set(serviceable) != {"state", "checks"}
        or not isinstance(checks, dict)
        or set(checks) != SERVICEABILITY_CHECKS
        or not all(value is True for value in checks.values())
    ):
        raise ValueError("pre-activation serviceability is incomplete")
    if activated != {"state": "activated"}:
        raise ValueError("activation state is invalid")
    return expected_identity


def _read_json(path: Path) -> dict:
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink() or metadata.st_size > 64 * 1024:
        raise ValueError(f"unsafe transaction input: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"transaction input is not an object: {path}")
    return value

def rollback_readiness(
    previous: dict | None,
    *,
    image_present: Callable[[str], bool],
    release_present: Callable[[str], bool],
) -> dict:
    """Report whether the previous backend/frontend pair remains resolvable."""

    if not previous:
        return {
            "status": "no_previous_deployment",
            "image_present": None,
            "frontend_release_present": None,
        }
    image = str(previous.get("image_id") or "")
    release = str(previous.get("frontend_release_dir") or "")
    image_ok = image_present(image) if image else False
    release_ok = release_present(release) if release else False
    ready = image_ok and release_ok
    return {
        "status": "rollback_artifacts_present" if ready else "rollback_artifacts_missing",
        "image_present": image_ok,
        "frontend_release_present": release_ok,
    }

def deploy_transaction(
    old: dict,
    candidate: dict,
    *,
    activate: Callable[[dict], None],
    deactivate: Callable[[dict], None],
    healthy: Callable[[dict], bool],
    privacy_probe: Callable[[dict], bool],
    pre_activate: Callable[[dict], dict],
    readiness: Callable[[dict | None], dict] | None = None,
) -> dict:
    if old.get("current") is not None and readiness is None:
        raise RuntimeError("rollback readiness verifier is required")
    evaluated = (
        readiness(old.get("current"))
        if readiness
        else {
            "status": "no_previous_deployment",
            "image_present": None,
            "frontend_release_present": None,
        }
    )
    if (
        old.get("current") is not None
        and evaluated is not None
        and evaluated.get("status") != "rollback_artifacts_present"
    ):
        raise RuntimeError("required rollback artifacts are not ready")
    pre_activation_receipt = build_pre_activation_receipt(
        candidate,
        pre_activate(candidate),
    )
    activate(candidate)
    pre_activation_receipt = complete_activation_receipt(
        pre_activation_receipt
    )
    if healthy(candidate) and privacy_probe(candidate):
        return {
            "schema_version": 2,
            "current": candidate,
            "previous": old.get("current"),
            "rollback_readiness": evaluated,
            "pre_activation_receipt": pre_activation_receipt,
        }
    previous = old.get("current")
    if previous:
        activate(previous)
        if not healthy(previous) or not privacy_probe(previous):
            raise RuntimeError("candidate failed and previous did not recover")
        raise RuntimeError("candidate failed; previous was restored")
    deactivate(candidate)
    raise RuntimeError("candidate failed; candidate was deactivated")


def rollback_transaction(
    state: dict,
    *,
    activate: Callable[[dict], None],
    healthy: Callable[[dict], bool],
    privacy_probe: Callable[[dict], bool],
) -> dict:
    previous = state.get("previous")
    current = state.get("current")
    if not previous or not current:
        raise ValueError("both current and previous are required for rollback")
    activate(previous)
    if not healthy(previous) or not privacy_probe(previous):
        activate(current)
        if not healthy(current) or not privacy_probe(current):
            raise RuntimeError("rollback failed and current did not recover")
        raise RuntimeError("rollback failed; current was restored")
    return {
        "schema_version": 2,
        "current": previous,
        "previous": current,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pre-activation-receipt", type=Path, required=True)
    parser.add_argument("--authority-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        authority = _read_json(args.authority_file)
        release = authority.get("release")
        if not isinstance(release, dict):
            raise ValueError("host authority contains no release identity")
        identity = validate_pre_activation_receipt(
            _read_json(args.pre_activation_receipt),
            release,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"PRE-ACTIVATION RECEIPT FAILED: {error}")
        return 1
    print(
        "PRE-ACTIVATION RECEIPT PASSED "
        f"combined_release_id={identity['combined_release_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
