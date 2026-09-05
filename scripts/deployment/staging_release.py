#!/usr/bin/env python3
"""Registry-free release packet, deploy, frontend, and rollback operations."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import stat
import sys

from scripts.deployment.authorization_markers import claim, sha256_file
from scripts.deployment.release_packet import (
    EXTERNAL_FRONTEND_MODE,
    IMAGE_REPOSITORY,
    _full_revision,
    _inspect,
    _validated_identity,
    build_release_packet_receipt,
    deploy_identity,
    validate_release_packet,
    verify_source_archive_revision,
)
from scripts.deployment.immutable_frontend import (
    SOURCE_URL_PREFIX,
    bind_frontend_release,
    frontend_install_plan,
    install_frontend_release,
    verify_frontend_release,
    verify_bound_frontend_identity,
)
from scripts.deployment.release_transaction import (
    deploy_transaction,
    rollback_readiness,
    rollback_transaction,
)
from scripts.deployment.run_staging_privacy_canary import (
    validate_receipt as validate_privacy_receipt,
)
from scripts.deployment.verify_staging_http import _request
from scripts.publication.verify_image_supply_chain import (
    validate_receipt as validate_supply_chain_receipt,
)
from scripts.tools.staging_secure_io import (
    atomic_owner_only_replace,
    path_matches_descriptor,
    require_trusted_parent,
    safe_absolute_path as safe_path,
    unlink_if_descriptor,
)
import time

ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "deploy" / "compose.yaml"
STAGING_COMPOSE = ROOT / "deploy" / "staging" / "compose.staging.yaml"
FRONTEND_COMPOSE = (
    ROOT / "deploy" / "compose.frontend-release.yaml"
)
COMPOSE = (
    "docker", "compose",
    "--file", str(BASE_COMPOSE),
    "--file", str(STAGING_COMPOSE),
)
HOST_MARKER = Path("/var/lib/private-alpha/.release-apply-authorized")
HOST_LOCK = Path("/var/lib/private-alpha/deployment.lock")
DEFAULT_STATE = Path("/var/lib/private-alpha/deployment-state.json")
DEFAULT_FRONTEND_RELEASE_ROOT = Path(
    "/var/lib/private-alpha/frontend-releases"
)
# The application container stays on an `internal: true` network, which Docker
# cannot publish ports from, so probes address it directly at the address
# pinned in deploy/staging/compose.staging.yaml rather than on host loopback.
# Changing either side requires changing the other in the same commit.
APP_PROBE_HOST = "172.31.240.2"
APP_PROBE_PORT = 8000
PREACTIVATION_HOST = "preactivation.invalid"
PREACTIVATION_CANARY = "preactivation-private-canary-1997-08-17"

_UNEXPOSED_PROBE_PROGRAM = r'''
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

request_data = json.load(sys.stdin)
origin = "http://127.0.0.1:8000"
headers = {"Accept": "application/json", "Host": request_data["host"]}

def request(path, body=None):
    current = dict(headers)
    if body is not None:
        current["Content-Type"] = "application/json"
    try:
        response = urlopen(
            Request(origin + path, data=body, headers=current), timeout=10
        )
    except HTTPError as error:
        response = error
    with response:
        payload = response.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            raise RuntimeError("candidate response exceeded probe bound")
        return response.status, payload, dict(response.headers)

checks = {}
status, raw, _ = request("/api/health")
checks["process_liveness"] = status == 200 and json.loads(raw) == {
    "status": "ok",
    "ready": True,
    "readiness_scope": "process_liveness_only",
}

status, raw, _ = request(
    "/api/chart",
    json.dumps(request_data["chart"], separators=(",", ":")).encode(),
)
chart = json.loads(raw) if status == 200 else {}
dossier = chart.get("calculation_dossier") or {}
location = (dossier.get("input_receipt") or {}).get("location") or {}
checks["synthetic_chart_full_ephemeris"] = (
    status == 200
    and location.get("place_label") == "synthetic-availability-probe"
    and (dossier.get("provenance") or {}).get(
        "all_core_calculation_sources_used_full_ephemeris"
    ) is True
)

status, raw, _ = request(
    "/api/places/search",
    json.dumps(
        {"query": "臺中", "country_code": "TW", "limit": 3},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode(),
)
places = json.loads(raw) if status == 200 else {}
checks["synthetic_place_search"] = (
    status == 200
    and (places.get("execution") or {}).get("runtime_outbound") is False
    and bool(places.get("results"))
)

status, raw, _ = request("/api/client-config")
client = json.loads(raw) if status == 200 else {}
checks["release_identity"] = (
    status == 200
    and client.get("release_identity") == request_data["release_identity"]
)

malformed = ('{"private_canary":"' + request_data["canary"]).encode()
status, raw, _ = request("/api/chart", malformed)
checks["privacy"] = status == 400 and request_data["canary"].encode() not in raw
print(json.dumps(checks, sort_keys=True))
'''




_sha256 = sha256_file


@contextmanager
def _host_lock():
    lock = safe_path(HOST_LOCK)
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        lock,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o077
        ):
            raise RuntimeError("deployment lock must be root-owned mode 0600")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)






def _assert_host_frontend_release_permissions(candidate: dict) -> None:
    frontend = candidate.get("frontend")
    if not isinstance(frontend, dict):
        raise RuntimeError("combined release omits frontend identity")
    release = safe_path(Path(str(frontend["release_directory"])))
    for path in (release, *release.rglob("*")):
        metadata = path.lstat()
        if metadata.st_uid != 0:
            raise RuntimeError(
                f"frontend release path must be root-owned: {path.name}"
            )
        if metadata.st_mode & 0o222:
            raise RuntimeError(
                f"frontend release path must be immutable: {path.name}"
            )




def _load_json(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"required regular file missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    atomic_owner_only_replace(path, payload, role="deployment state")


def _reserve_output(path: Path) -> int:
    require_trusted_parent(path, role="release packet output")
    return os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )


def _require_path_identity(path: Path, descriptor: int) -> None:
    if not path_matches_descriptor(path, descriptor):
        raise RuntimeError(f"release packet output object changed: {path.name}")


def _expected_image_id(receipt: dict, expected_image_id: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id):
        raise ValueError("expected image ID must be sha256 plus 64 lowercase hex")
    if receipt.get("image_id") != expected_image_id:
        raise ValueError("release packet differs from approved image ID")
    return expected_image_id


def _ensure_apply_host(purpose: str, bindings: dict[str, str]) -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("--apply requires Linux amd64")
    if os.geteuid() != 0:
        raise RuntimeError("--apply requires root")
    claim(
        HOST_MARKER,
        expected_purpose=purpose,
        expected_bindings={
            **bindings,
            "script_sha256": sha256_file(Path(__file__)),
        },
    )


@contextmanager
def _authorized_host_action(
    purpose: str,
    observe_bindings: Callable[[], dict[str, str]],
):
    """Observe, authorize and mutate under one lock.

    The bindings arrive as a callable rather than a mapping on purpose.  A
    mapping literal is evaluated before this function is entered, so every
    digest in it would be read before the lock exists, and the one-use grant
    would then describe an object that the mutation is free to re-resolve
    afterwards.  Producing them here puts the observation, the authorization
    and the mutation inside the same lock.
    """

    with _host_lock():
        _ensure_apply_host(purpose, observe_bindings())
        yield


def _clean_git_revision(
    root: Path,
    expected_revision: str,
    label: str,
) -> str:
    expected = _full_revision(expected_revision, label)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if status:
        raise RuntimeError(f"{label} checkout must be clean")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if actual != expected:
        raise RuntimeError(f"{label} checkout HEAD mismatch")
    return actual


def _packet_export(args: argparse.Namespace) -> None:
    private_revision = _clean_git_revision(
        ROOT, args.private_tooling_revision, "private tooling revision"
    )
    public_root = safe_path(args.public_source_root)
    public_revision = _clean_git_revision(
        public_root, args.public_source_revision, "public source revision"
    )
    inspection = _inspect(args.image)
    _expected_image_id({"image_id": inspection["Id"]}, args.expected_image_id)
    _validated_identity(inspection, public_revision)
    source_archive = safe_path(args.source_archive)
    transfer_archive = safe_path(args.transfer_archive)
    receipt_path = safe_path(args.receipt)
    outputs = (source_archive, transfer_archive, receipt_path)
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise RuntimeError("refusing to overwrite release packet artifact")
    if any(ROOT == path or ROOT in path.parents for path in outputs):
        raise RuntimeError("release packet artifacts must be outside the repository")
    if not args.apply:
        print(json.dumps({
            "mode": "plan",
            "private_tooling_revision": private_revision,
            "public_source_revision": public_revision,
            "image_id": inspection["Id"],
            "source_archive_filename": source_archive.name,
            "transfer_archive_filename": transfer_archive.name,
        }, indent=2))
        return
    source_archive.parent.mkdir(parents=True, exist_ok=True)
    transfer_archive.parent.mkdir(parents=True, exist_ok=True)
    source_partial = source_archive.with_name(f".{source_archive.name}.partial")
    transfer_partial = transfer_archive.with_name(
        f".{transfer_archive.name}.partial"
    )
    if source_partial.exists() or transfer_partial.exists():
        raise RuntimeError("stale release packet partial artifact exists")
    published: list[tuple[Path, int]] = []
    partials: list[tuple[Path, int]] = []
    try:
        partials.append((source_partial, _reserve_output(source_partial)))
        partials.append((transfer_partial, _reserve_output(transfer_partial)))
        subprocess.run(
            [
                "git",
                "archive",
                "--format=tar.gz",
                f"--output={source_partial}",
                public_revision,
            ],
            cwd=public_root,
            check=True,
        )
        _require_path_identity(source_partial, partials[0][1])
        verify_source_archive_revision(source_partial, public_revision)
        subprocess.run(
            ["docker", "save", "--output", str(transfer_partial), args.image],
            check=True,
        )
        _require_path_identity(transfer_partial, partials[1][1])
        receipt = build_release_packet_receipt(
            private_tooling_revision=private_revision,
            public_source_revision=public_revision,
            source_archive=source_partial,
            transfer_archive=transfer_partial,
            inspection=inspection,
        )
        source_identity = receipt.get("source_archive")
        transfer_identity = receipt.get("transfer_archive")
        if not isinstance(source_identity, dict) or not isinstance(
            transfer_identity, dict
        ):
            raise RuntimeError("release packet archive identities are malformed")
        source_identity["filename"] = source_archive.name
        transfer_identity["filename"] = transfer_archive.name
        validate_release_packet(
            receipt,
            source_archive=source_partial,
            transfer_archive=transfer_partial,
            expected_private_revision=private_revision,
            expected_public_revision=public_revision,
            expected_source_filename=source_archive.name,
            expected_transfer_filename=transfer_archive.name,
        )
        os.replace(source_partial, source_archive)
        published.append((source_archive, partials[0][1]))
        os.replace(transfer_partial, transfer_archive)
        published.append((transfer_archive, partials[1][1]))
        _atomic_json(receipt_path, receipt)
    except Exception:
        for path, descriptor in reversed(published):
            unlink_if_descriptor(path, descriptor)
        raise
    finally:
        for path, descriptor in reversed(partials):
            unlink_if_descriptor(path, descriptor)
        for _path, descriptor in partials:
            os.close(descriptor)
    print(json.dumps({"mode": "exported_release_packet", **receipt}, indent=2))


def _packet_identity(args: argparse.Namespace) -> tuple[dict, dict]:
    source_archive = safe_path(args.source_archive)
    transfer_archive = safe_path(args.transfer_archive)
    receipt = _load_json(safe_path(args.receipt))
    _expected_image_id(receipt, args.expected_image_id)
    identity = validate_release_packet(
        receipt,
        source_archive=source_archive,
        transfer_archive=transfer_archive,
        expected_private_revision=args.private_tooling_revision,
        expected_public_revision=args.public_source_revision,
    )
    return receipt, identity


def _packet_verify(args: argparse.Namespace) -> None:
    _receipt, identity = _packet_identity(args)
    print(json.dumps({"mode": "verified_release_packet", **identity}, indent=2))


def _packet_load(args: argparse.Namespace) -> None:
    receipt, identity = _packet_identity(args)
    if not args.apply:
        print(json.dumps({"mode": "plan_local_load", **identity}, indent=2))
        return
    with _authorized_host_action(
        "release-packet-load",
        lambda: {
            "image_id": str(receipt["image_id"]),
            "transfer_archive_sha256": _sha256(
                safe_path(args.transfer_archive)
            ),
        },
    ):
        subprocess.run(
            ["docker", "load", "--input", str(safe_path(args.transfer_archive))],
            check=True,
        )
        inspection = _inspect(str(receipt["image_id"]))
        loaded = _validated_identity(
            inspection, str(receipt["public_source_revision"])
        )
        if loaded["image_id"] != receipt["image_id"]:
            raise RuntimeError("loaded image ID does not match release packet")
        tag = (
            f"{IMAGE_REPOSITORY}:release-"
            f"{str(receipt['public_source_revision'])[:12]}"
        )
        subprocess.run(
            ["docker", "image", "tag", loaded["image_id"], tag],
            check=True,
        )
    print(json.dumps({
        "mode": "loaded_release_packet",
        "tag": tag,
        **identity,
    }, indent=2))


def _state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 2, "current": None, "previous": None}
    state = _load_json(path)
    if state.get("schema_version") != 2:
        raise ValueError("legacy deployment state is unsupported")
    return state


def _activate(identity: dict) -> None:
    verify_bound_frontend_identity(identity)
    backend = identity["backend"]
    revision = backend["vcs_revision"]
    tag = f"release-{revision[:12]}"
    expected_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", f"{IMAGE_REPOSITORY}:{tag}"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if expected_id != backend["image_id"]:
        raise RuntimeError("release tag resolves to the wrong image ID")
    environment = {**os.environ, "IMAGE_TAG": tag, "VCS_REF": revision}
    compose = (*COMPOSE, "--file", str(FRONTEND_COMPOSE))
    frontend = identity["frontend"]
    environment.update({
        "FRONTEND_RELEASE_DIR": str(frontend["release_directory"]),
        "FRONTEND_RELEASE_DIGEST": str(frontend["artifact_digest"]),
        "BACKEND_IMAGE_ID": str(backend["image_id"]),
        "COMBINED_RELEASE_ID": str(identity["combined_release_id"]),
    })
    subprocess.run(
        [
            *compose,
            "up", "--detach", "--no-build", "--force-recreate",
            "private-alpha-app",
        ],
        check=True,
        env=environment,
    )


def compose_ps_health(stdout: str) -> tuple[bool, str]:
    """Parse array, object or NDJSON output and distinguish failure reasons."""
    text = stdout.strip()
    if not text:
        return False, "no_compose_records"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            records = [
                json.loads(line)
                for raw in text.splitlines()
                if (line := raw.strip())
            ]
        except json.JSONDecodeError:
            return False, "unparsable_compose_output"
    else:
        records = [parsed] if isinstance(parsed, dict) else parsed
    if not isinstance(records, list) or any(
        not isinstance(item, dict) for item in records
    ):
        return False, "unparsable_compose_output"

    if len(records) != 1:
        return False, (
            "no_compose_records" if not records else "multiple_compose_records"
        )
    record = records[0]
    if record.get("State") != "running":
        return False, "container_not_running"
    health = record.get("Health")
    if health == "":
        return False, "image_declares_no_healthcheck"
    if health != "healthy":
        return False, "container_not_healthy"
    return True, "healthy"


def _healthy(_identity: dict) -> bool:
    reason = "never_polled"
    for _ in range(60):
        result = subprocess.run(
            [
                *COMPOSE,
                "ps", "--format", "json", "private-alpha-app",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            reason = "compose_ps_failed"
        else:
            healthy, reason = compose_ps_health(result.stdout)
            if healthy:
                container = subprocess.run(
                    [
                        *COMPOSE,
                        "ps", "--quiet", "private-alpha-app",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                container_id = container.stdout.strip()
                if container.returncode != 0 or not container_id:
                    reason = "container_identity_unavailable"
                    continue
                actual_image = subprocess.run(
                    [
                        "docker", "inspect", "--format", "{{.Image}}",
                        container_id,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                backend = _identity.get("backend") or _identity
                if (
                    actual_image.returncode == 0
                    and actual_image.stdout.strip() == backend.get("image_id")
                ):
                    return True
                reason = "running_image_identity_mismatch"
        time.sleep(2)
    # Say which of the failure modes ended the wait. Without this the caller
    # cannot tell a broken service from a broken health check.
    print(f"health wait ended: {reason}", file=sys.stderr)
    return False


def _deactivate(_identity: dict) -> None:
    subprocess.run(
        [
            *COMPOSE,
            "down", "--remove-orphans",
        ],
        check=True,
    )


def _privacy_probe(identity: dict) -> bool:
    def local_get(path: str) -> tuple[int, bytes, dict[str, str]] | None:
        try:
            status, body, headers = _request(
                f"http://{APP_PROBE_HOST}:{APP_PROBE_PORT}{path}",
                timeout=5,
                maximum_bytes=4096,
            )
            return None if status == 0 else (status, body, headers)
        except (OSError, TimeoutError, ValueError):
            return None

    try:
        health = local_get("/api/health")
        if health is None or health[0] != 200:
            return False
        expected_health = {
            "status": "ok",
            "ready": True,
            "readiness_scope": "process_liveness_only",
        }
        if json.loads(health[1]) != expected_health:
            return False
        headers = {key.casefold(): value for key, value in health[2].items()}
        if "noindex" not in headers.get("x-robots-tag", "").casefold():
            return False
        for path in ("/api/runtime-health", "/openapi.json"):
            hidden = local_get(path)
            if hidden is None or hidden[0] != 404:
                return False
        configuration = local_get("/api/client-config")
        if configuration is None or configuration[0] != 200:
            return False
        release_identity = json.loads(configuration[1]).get("release_identity")
        frontend = identity["frontend"]
        backend = identity["backend"]
        if (
            not isinstance(release_identity, dict)
            or release_identity.get("combined_release_id")
            != identity["combined_release_id"]
            or release_identity.get("backend", {}).get("image_id")
            != backend["image_id"]
            or release_identity.get("backend", {}).get("public_source_revision")
            != backend["vcs_revision"]
            or release_identity.get("frontend", {}).get("artifact_digest")
            != frontend["artifact_digest"]
            or release_identity.get("frontend", {}).get("public_source_revision")
            != frontend["public_source_revision"]
            or release_identity.get("contracts") != frontend["contracts"]
        ):
            return False
        return True
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def _pre_activation_serviceability(identity: dict) -> dict:
    """Run the exact candidate on network=none before changing live traffic."""

    verify_bound_frontend_identity(identity)
    backend = identity["backend"]
    frontend = identity["frontend"]
    payload_path = ROOT / "deploy/staging/synthetic-availability-chart.json"
    payload = _load_json(payload_path)
    release_directory = safe_path(Path(str(frontend["release_directory"])))
    command = [
        "docker", "run", "--detach", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--pids-limit", "96", "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1.0", "--ulimit", "nofile=65536:65536",
        "--env", f"CLASSICAL_ASTROLOGY_EXPECTED_HOST={PREACTIVATION_HOST}",
        "--env", "CLASSICAL_ASTROLOGY_REQUIRE_FRONTEND_RELEASE=1",
        "--env", "CLASSICAL_ASTROLOGY_FRONTEND_ROOT=/app/frontend",
        "--env", (
            "CLASSICAL_ASTROLOGY_FRONTEND_RELEASE_DIGEST="
            f"{frontend['artifact_digest']}"
        ),
        "--env", f"CLASSICAL_ASTROLOGY_COMBINED_RELEASE_ID={identity['combined_release_id']}",
        "--env", f"CLASSICAL_ASTROLOGY_BACKEND_IMAGE_ID={backend['image_id']}",
        "--mount", f"type=bind,src={release_directory},dst=/app/frontend,readonly",
        str(backend["image_id"]),
        "/opt/venv/bin/python", "-m", "uvicorn", "app.main:create_app",
        "--factory", "--app-dir", "/app/backend", "--host", "127.0.0.1",
        "--port", "8000", "--workers", "1", "--loop", "asyncio",
        "--http", "h11", "--ws", "none", "--no-access-log",
    ]
    started = subprocess.run(
        command, check=True, text=True, capture_output=True
    )
    container_id = started.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        raise RuntimeError("unexposed candidate container ID is invalid")
    try:
        health = ""
        for _ in range(60):
            observed = subprocess.run(
                [
                    "docker", "inspect", "--format",
                    "{{.State.Health.Status}}", container_id,
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            health = observed.stdout.strip()
            if observed.returncode == 0 and health == "healthy":
                break
            if observed.returncode or health == "unhealthy":
                raise RuntimeError("unexposed candidate did not become healthy")
            time.sleep(1)
        else:
            raise RuntimeError("unexposed candidate health wait expired")

        inspection = json.loads(subprocess.run(
            ["docker", "inspect", container_id],
            check=True,
            text=True,
            capture_output=True,
        ).stdout)[0]
        host_config = inspection.get("HostConfig") or {}
        network_settings = inspection.get("NetworkSettings") or {}
        traffic_exposed = not (
            host_config.get("NetworkMode") == "none"
            and not host_config.get("PortBindings")
            and not network_settings.get("Ports")
        )
        if traffic_exposed:
            raise RuntimeError("candidate probe container exposed traffic")

        expected_release = {
            "status": "available",
            "combined_release_id": identity["combined_release_id"],
            "backend": {
                "image_id": backend["image_id"],
                "public_source_revision": backend["vcs_revision"],
                "source_url": (
                    f"{SOURCE_URL_PREFIX}{backend['vcs_revision']}"
                ),
            },
            "frontend": {
                "artifact_digest": frontend["artifact_digest"],
                "public_source_revision": frontend["public_source_revision"],
                "source_url": frontend["source_url"],
            },
            "contracts": frontend["contracts"],
        }
        probe = subprocess.run(
            [
                "docker", "exec", "--interactive", container_id,
                "/opt/venv/bin/python", "-c", _UNEXPOSED_PROBE_PROGRAM,
            ],
            input=json.dumps({
                "host": PREACTIVATION_HOST,
                "canary": PREACTIVATION_CANARY,
                "chart": payload,
                "release_identity": expected_release,
            }),
            check=True,
            text=True,
            capture_output=True,
        )
        checks = json.loads(probe.stdout)
        logs = subprocess.run(
            ["docker", "logs", container_id],
            check=False,
            text=True,
            capture_output=True,
        )
        if PREACTIVATION_CANARY in logs.stdout + logs.stderr:
            checks["privacy"] = False
        return {"traffic_exposed": False, "checks": checks}
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container_id],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _rollback_readiness(previous: dict | None) -> dict:
    def image_present(image_id: str) -> bool:
        return subprocess.run(
            ["docker", "image", "inspect", image_id],
            check=False, capture_output=True,
        ).returncode == 0

    def release_present(path: str) -> bool:
        try:
            candidate = safe_path(Path(path))
            verified = verify_frontend_release(candidate, authority_root=ROOT)
        except (OSError, RuntimeError, ValueError):
            return False
        return candidate.name == verified.get("artifact_digest")

    return rollback_readiness(
        previous, image_present=image_present, release_present=release_present
    )


def _deploy(args: argparse.Namespace) -> None:
    receipt = _load_json(safe_path(args.receipt))
    backend_candidate = deploy_identity(receipt, args.expected_revision)
    candidate = bind_frontend_release(
        backend_candidate,
        args.frontend_release_dir,
    )
    supply_chain_path = safe_path(args.supply_chain_receipt)
    validate_supply_chain_receipt(
        _load_json(supply_chain_path), candidate
    )
    privacy_receipt_path = safe_path(args.privacy_receipt)
    validate_privacy_receipt(
        _load_json(privacy_receipt_path), candidate
    )
    if not args.apply:
        print(json.dumps({"mode": "plan", "candidate": candidate}, indent=2))
        return
    state_path = safe_path(args.state)
    with _authorized_host_action(
        "release-deploy",
        lambda: {
            "image_id": str(backend_candidate["image_id"]),
            "receipt_sha256": _sha256(safe_path(args.receipt)),
            "supply_chain_receipt_sha256": _sha256(supply_chain_path),
            "privacy_receipt_sha256": _sha256(privacy_receipt_path),
        },
    ):
        _assert_host_frontend_release_permissions(candidate)
        old = _state(state_path)
        if old.get("deployment_transaction"):
            raise RuntimeError("an earlier deployment transaction is unresolved")
        _atomic_json(state_path, {
            **old,
            "deployment_transaction": "pending_activation",
            "pending_candidate": candidate,
        })
        try:
            updated = deploy_transaction(
                old,
                candidate,
                activate=_activate,
                deactivate=_deactivate,
                healthy=_healthy,
                privacy_probe=_privacy_probe,
                pre_activate=_pre_activation_serviceability,
                readiness=_rollback_readiness,
            )
        except BaseException:
            _atomic_json(state_path, old)
            raise
        _atomic_json(state_path, updated)
    print(json.dumps({"mode": "deployed", **updated}, indent=2))


def _frontend_install(args: argparse.Namespace) -> None:
    incoming, destination, verified = frontend_install_plan(
        args.incoming_release_dir,
        args.release_root,
        expected_artifact_digest=args.expected_artifact_digest,
    )
    release_root = destination.parent
    if not args.apply:
        print(json.dumps({
            "mode": "plan_frontend_install",
            "incoming_release_directory": str(incoming),
            "destination": str(destination),
            "artifact_digest": verified["artifact_digest"],
            "public_source_revision": verified[
                "frontend_public_source_revision"
            ],
        }, indent=2))
        return
    with _authorized_host_action(
        "release-frontend-install",
        lambda: {
            "frontend_artifact_digest": args.expected_artifact_digest,
            "incoming_manifest_sha256": _sha256(
                incoming / "frontend-release.json"
            ),
        },
    ):
        installed = install_frontend_release(
            incoming,
            release_root,
            expected_artifact_digest=args.expected_artifact_digest,
        )
        root_metadata = release_root.lstat()
        if (
            root_metadata.st_uid != 0
            or root_metadata.st_mode & 0o022
        ):
            raise RuntimeError(
                "frontend release root ownership or permissions are unsafe"
            )
        _assert_host_frontend_release_permissions({
            "frontend": installed,
        })
    print(json.dumps({"mode": "frontend_installed", **installed}, indent=2))


def _rollback(args: argparse.Namespace) -> None:
    state_path = safe_path(args.state)
    if not args.apply:
        state = _state(state_path)
        print(json.dumps({"mode": "plan", "state": state}, indent=2))
        return
    with _authorized_host_action(
        "release-rollback",
        lambda: {
            "state_sha256": _sha256(state_path),
        },
    ):
        state = _state(state_path)
        updated = rollback_transaction(
            state,
            activate=_activate,
            healthy=_healthy,
            privacy_probe=_privacy_probe,
        )
        _atomic_json(state_path, updated)
    print(json.dumps({"mode": "rolled_back", **updated}, indent=2))


def _packet_arguments(parser: argparse.ArgumentParser, *, apply: bool) -> None:
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--transfer-archive", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--private-tooling-revision", required=True)
    parser.add_argument("--public-source-revision", required=True)
    parser.add_argument("--expected-image-id", required=True)
    if apply:
        parser.add_argument("--apply", action="store_true")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    packet_export = commands.add_parser("packet-export")
    packet_export.add_argument("--image", required=True)
    packet_export.add_argument("--public-source-root", type=Path, required=True)
    _packet_arguments(packet_export, apply=True)
    packet_export.set_defaults(handler=_packet_export)

    for command_name, handler in (
        ("packet-verify", _packet_verify),
        ("packet-load", _packet_load),
    ):
        packet = commands.add_parser(command_name)
        _packet_arguments(packet, apply=command_name == "packet-load")
        packet.set_defaults(handler=handler)

    deploy = commands.add_parser("deploy")
    deploy.add_argument("--receipt", type=Path, required=True)
    deploy.add_argument(
        "--supply-chain-receipt", type=Path, required=True
    )
    deploy.add_argument("--privacy-receipt", type=Path, required=True)
    deploy.add_argument("--expected-revision", required=True)
    deploy.add_argument("--frontend-release-dir", type=Path, required=True)
    deploy.add_argument("--state", type=Path, default=DEFAULT_STATE)
    deploy.add_argument("--apply", action="store_true")
    deploy.set_defaults(handler=_deploy)

    frontend_install = commands.add_parser("frontend-install")
    frontend_install.add_argument(
        "--incoming-release-dir", type=Path, required=True
    )
    frontend_install.add_argument(
        "--expected-artifact-digest", required=True
    )
    frontend_install.add_argument(
        "--release-root",
        type=Path,
        default=DEFAULT_FRONTEND_RELEASE_ROOT,
    )
    frontend_install.add_argument("--apply", action="store_true")
    frontend_install.set_defaults(handler=_frontend_install)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--state", type=Path, default=DEFAULT_STATE)
    rollback.add_argument("--apply", action="store_true")
    rollback.set_defaults(handler=_rollback)

    args = parser.parse_args()
    try:
        args.handler(args)
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        ValueError,
    ) as error:
        raise SystemExit(
            f"staging release failed: {type(error).__name__}: {error}"
        ) from None


if __name__ == "__main__":
    main()
