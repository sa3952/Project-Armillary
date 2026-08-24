#!/usr/bin/env python3
"""Run the fixed, privacy-safe Private Alpha availability probe."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_PAYLOAD = Path("deploy/staging/synthetic-availability-chart.json")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
THRESHOLD = 3
COOLDOWN_SECONDS = 15 * 60


class AvailabilityFailure(RuntimeError):
    """A closed, privacy-safe availability predicate failed."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AvailabilityFailure("availability probe refuses redirects")


def _bounded_owner_file(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise AvailabilityFailure("monitor credential file is missing") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or not 1 <= metadata.st_size <= 1024
    ):
        raise AvailabilityFailure("monitor credential file ownership or mode is unsafe")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or lines[0] != lines[0].strip() or ":" not in lines[0]:
        raise AvailabilityFailure("monitor credential must be one basic-auth pair")
    return lines[0]


def validate_base_url(raw: str, *, allow_private_http: bool = False) -> str:
    parsed = urlsplit(raw)
    private_http = False
    if allow_private_http and parsed.scheme == "http" and parsed.hostname:
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        private_http = bool(address and (address.is_private or address.is_loopback))
    if (
        (parsed.scheme != "https" and not private_http)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise AvailabilityFailure(
            "availability base URL must be HTTPS or an explicit private-IP HTTP origin"
        )
    return raw.rstrip("/")


def validate_health_payload(payload: object) -> None:
    if payload != {
        "status": "ok",
        "ready": True,
        "readiness_scope": "process_liveness_only",
    }:
        raise AvailabilityFailure("health payload is not exact process liveness")


def validate_chart_payload(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != "0.13.0":
        raise AvailabilityFailure("chart payload schema is unavailable")
    dossier = payload.get("calculation_dossier")
    if not isinstance(dossier, dict):
        raise AvailabilityFailure("chart dossier is unavailable")
    receipt = dossier.get("input_receipt")
    location = receipt.get("location") if isinstance(receipt, dict) else None
    if not isinstance(location, dict) or location.get(
        "place_label"
    ) != "synthetic-availability-probe":
        raise AvailabilityFailure("chart synthetic identity is unavailable")
    provenance = dossier.get("provenance")
    if not isinstance(provenance, dict) or provenance.get(
        "all_core_calculation_sources_used_full_ephemeris"
    ) is not True:
        raise AvailabilityFailure("chart full ephemeris readiness is unavailable")


def _read_json(request: Request, *, timeout: float) -> object:
    try:
        with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200 or len(body) > MAX_RESPONSE_BYTES:
                raise AvailabilityFailure("availability endpoint returned HTTP failure")
    except HTTPError as error:
        raise AvailabilityFailure("availability endpoint returned HTTP failure") from error
    except (OSError, TimeoutError, URLError) as error:
        raise AvailabilityFailure("availability endpoint is unreachable") from error
    try:
        return json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AvailabilityFailure("availability endpoint returned invalid JSON") from error


def probe(
    *,
    base_url: str,
    source_root: Path,
    credential: str | None = None,
    timeout: float = 10.0,
    allow_private_http: bool = False,
) -> dict[str, object]:
    origin = validate_base_url(base_url, allow_private_http=allow_private_http)
    headers = {
        "Accept": "application/json",
        "User-Agent": "project-armillary-availability/1",
    }
    if credential is not None:
        encoded = base64.b64encode(credential.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    health = _read_json(
        Request(f"{origin}/api/health", headers=headers, method="GET"),
        timeout=timeout,
    )
    validate_health_payload(health)
    payload_path = source_root / SYNTHETIC_PAYLOAD
    if not payload_path.is_file() or payload_path.is_symlink():
        raise AvailabilityFailure("synthetic payload is missing or unsafe")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    chart = _read_json(
        Request(
            f"{origin}/api/chart",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        ),
        timeout=timeout,
    )
    validate_chart_payload(chart)
    return {
        "schema_version": "private-alpha-availability-probe-v1",
        "status": "passed",
        "checks": ["process_liveness", "synthetic_chart_full_ephemeris"],
        "synthetic_payload_sha256": hashlib.sha256(
            payload_path.read_bytes()
        ).hexdigest(),
    }


def initial_state() -> dict[str, object]:
    return {
        "schema_version": "private-alpha-watchdog-state-v1",
        "consecutive_failures": 0,
        "incident_open": False,
        "last_restart_epoch": None,
        "pending_notifications": [],
    }


def decide(state, *, external_ok: bool, local_ok: bool, now: int):
    updated = dict(state)
    actions = []
    if external_ok:
        if updated.get("incident_open") is True:
            actions.append("notify:incident_recovered")
        updated.update(consecutive_failures=0, incident_open=False)
        return updated, actions
    failures = int(updated.get("consecutive_failures", 0)) + 1
    updated["consecutive_failures"] = failures
    if failures < THRESHOLD:
        return updated, actions
    if updated.get("incident_open") is not True:
        updated["incident_open"] = True
        actions.append("notify:incident_opened")
    last = updated.get("last_restart_epoch")
    if not local_ok and (last is None or now - int(last) >= COOLDOWN_SECONDS):
        updated["last_restart_epoch"] = now
        actions.append("restart")
    return updated, actions


def notification_payload(*, event, failure_class, revision, restart_status):
    allowed = {
        "event": {"incident_opened", "incident_recovered", "restart_recovered", "restart_failed"},
        "failure": {"none", "external_path", "application_path"},
        "restart": {"not_attempted", "recovered", "failed"},
    }
    if (
        event not in allowed["event"]
        or failure_class not in allowed["failure"]
        or restart_status not in allowed["restart"]
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
    ):
        raise ValueError("notification vocabulary is invalid")
    return (json.dumps({
        "schema_version": "private-alpha-watchdog-notification-v1",
        "event": event,
        "failure_class": failure_class,
        "revision": revision,
        "restart_status": restart_status,
    }, sort_keys=True, separators=(",", ":")) + "\n").encode()


def run_once(*, external_probe, local_probe, restart, notify, state, now):
    updated = dict(state)
    events = []
    failures = 0

    pending = list(updated.get("pending_notifications") or [])
    updated["pending_notifications"] = []
    for item in pending:
        try:
            notify(item["event"], item["failure_class"], item["restart_status"])
            events.append(item["event"])
        except RuntimeError:
            updated["pending_notifications"].append(item)
            failures += 1

    def emit(event, failure, status):
        nonlocal failures
        try:
            notify(event, failure, status)
            events.append(event)
        except RuntimeError:
            updated["pending_notifications"] = [
                *(updated.get("pending_notifications") or []),
                {"event": event, "failure_class": failure, "restart_status": status},
            ]
            failures += 1
            events.append("notification_delivery_failed")

    external_ok = external_probe()
    local_ok = external_ok or local_probe()
    updated, actions = decide(updated, external_ok=external_ok, local_ok=local_ok, now=now)
    failure_class = "none" if external_ok else "external_path" if local_ok else "application_path"
    restart_status = "not_attempted"
    for action in actions:
        if action.startswith("notify:"):
            emit(action.split(":", 1)[1], failure_class, restart_status)
        else:
            restart(); restart_status = "recovered" if local_probe() else "failed"
            emit("restart_" + restart_status, "application_path", restart_status)
    return updated, {
        "schema_version": "private-alpha-watchdog-run-v1",
        "external_ok": external_ok,
        "local_ok": local_ok,
        "failure_class": failure_class,
        "consecutive_failures": updated["consecutive_failures"],
        "restart_status": restart_status,
        "events": events,
        "notification_failures": failures,
    }


def _state(path: Path) -> dict[str, object]:
    if not path.exists():
        return initial_state()
    value = json.loads(path.read_text())
    if set(value) != set(initial_state()) or value.get("schema_version") != initial_state()["schema_version"]:
        raise AvailabilityFailure("watchdog state is invalid")
    return value


def _save_state(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def _owner_url(path: Path) -> str:
    value = _bounded_owner_file(path)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise AvailabilityFailure("notification URL is invalid")
    return value


def _send(url: str, payload: bytes) -> None:
    try:
        with build_opener().open(Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=10) as response:
            if not 200 <= response.status < 300:
                raise AvailabilityFailure("notification endpoint returned HTTP failure")
    except (HTTPError, URLError, OSError, TimeoutError) as error:
        raise RuntimeError("notification endpoint is unavailable") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--allow-private-http", action="store_true")
    parser.add_argument("--supervise", action="store_true")
    parser.add_argument("--external-base-url")
    parser.add_argument("--local-base-url")
    parser.add_argument("--notification-url-file", type=Path)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--revision")
    args = parser.parse_args()
    credential = (
        _bounded_owner_file(args.credential_file)
        if args.credential_file is not None
        else None
    )
    if args.supervise:
        if not all((args.external_base_url, args.local_base_url, args.notification_url_file, args.state_file, args.revision)):
            raise AvailabilityFailure("supervisor inputs are incomplete")
        notification_url = _owner_url(args.notification_url_file)
        state = _state(args.state_file)
        def checked(base, secret, private):
            try:
                probe(base_url=base, source_root=args.source_root, credential=secret, allow_private_http=private)
                return True
            except AvailabilityFailure:
                return False
        def notify(event, failure, status):
            _send(notification_url, notification_payload(event=event, failure_class=failure, revision=args.revision, restart_status=status))
        updated, result = run_once(
            external_probe=lambda: checked(args.external_base_url, credential, False),
            local_probe=lambda: checked(args.local_base_url, None, True),
            restart=lambda: subprocess.run([
                "docker", "compose", "--file", str(args.source_root / "deploy/compose.yaml"),
                "--file", str(args.source_root / "deploy/staging/compose.staging.yaml"),
                "restart", "private-alpha-app",
            ], check=True, stdout=subprocess.DEVNULL),
            notify=notify, state=state, now=int(time.time()),
        )
        _save_state(args.state_file, updated)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    result = probe(
        base_url=args.base_url,
        source_root=args.source_root,
        credential=credential,
        allow_private_http=args.allow_private_http,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AvailabilityFailure, json.JSONDecodeError) as error:
        print(f"PRIVATE ALPHA AVAILABILITY FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from None
