#!/usr/bin/env python3
"""Canonical one-context, one-build transaction for release-capable images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from scripts.deployment.frontend_release import validate_publication_receipt
from scripts.tools.output_confinement import external_output_path
from scripts.tools.source_tree_identity import (
    SourceTreeIdentity,
    maintained_source_paths,
    observe_source_tree,
)
from scripts.verification.verify_docker_context import materialize_context


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_EVIDENCE_PATH = "/usr/local/share/project-armillary/build-evidence"
CONTEXT_DISCRIMINATION_PATH = Path("build-context-probe-8f38f069.txt")
CONTEXT_DISCRIMINATION_BYTES = b"governed-context-control-v1\n"
BUILD_PURPOSES = (
    "diagnostic",
    "release-candidate",
    "reproducibility-comparison",
)


class BuildTransactionFailure(RuntimeError):
    pass


def clean_checkout_required(*, purpose: str, requested: bool) -> bool:
    """Release/comparison builds are clean-tree operations by definition."""
    if purpose not in BUILD_PURPOSES:
        raise BuildTransactionFailure(f"unknown build purpose: {purpose}")
    return requested or purpose != "diagnostic"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def context_manifest(root: Path) -> dict[str, object]:
    """Independently describe the materializer output before BuildKit sees it."""
    root = root.resolve()
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        entry: dict[str, object] = {
            "path": relative,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "size_bytes": None,
            "sha256": None,
            "symlink_target": None,
        }
        if path.is_symlink():
            entry.update(type="symlink", symlink_target=os.readlink(path))
        elif path.is_file():
            entry.update(
                type="file",
                size_bytes=metadata.st_size,
                sha256=_sha256(path),
            )
        elif path.is_dir():
            entry["type"] = "directory"
        else:
            entry["type"] = "special"
        entries.append(entry)
    canonical = json.dumps(
        entries, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema_version": "build-context-identity-v1",
        "producer": "materialized_context_observation",
        "entries": entries,
        "identity_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def assert_context_receipts_match(
    expected: dict[str, object], observed: dict[str, object]
) -> None:
    expected_entries = expected.get("entries")
    observed_entries = observed.get("entries")
    if not isinstance(expected_entries, list) or not isinstance(
        observed_entries, list
    ):
        raise BuildTransactionFailure("BuildKit context receipt entries are invalid")
    if expected_entries != observed_entries:
        expected_by_path = {
            str(item.get("path")): item
            for item in expected_entries or []
            if isinstance(item, dict)
        }
        observed_by_path = {
            str(item.get("path")): item
            for item in observed_entries or []
            if isinstance(item, dict)
        }
        extra = sorted(set(observed_by_path) - set(expected_by_path))
        missing = sorted(set(expected_by_path) - set(observed_by_path))
        changed = sorted(
            path
            for path in set(expected_by_path) & set(observed_by_path)
            if expected_by_path[path] != observed_by_path[path]
        )
        raise BuildTransactionFailure(
            "BuildKit context differs from materialized context: "
            f"extra={extra}, missing={missing}, changed={changed}"
        )


def assert_embedded_contract_consistent(
    *,
    contract: dict[str, object],
    context: dict[str, object],
    toolchain: dict[str, object],
    revision: str,
    platform: str,
) -> None:
    toolchain_identity = hashlib.sha256(
        json.dumps(toolchain, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if contract != {
        "schema_version": "build-contract-v1",
        "source_revision": revision,
        "target_platform": platform,
        "build_context_identity_sha256": context.get("identity_sha256"),
        "toolchain_identity_sha256": toolchain_identity,
    }:
        raise BuildTransactionFailure(
            "runtime build contract is inconsistent with embedded evidence"
        )


def publication_status_for_build(
    *, purpose: str, publication_receipt: Path | None, revision: str
) -> str:
    if purpose not in BUILD_PURPOSES:
        raise BuildTransactionFailure(f"unknown build purpose: {purpose}")
    if publication_receipt is None:
        if purpose != "diagnostic":
            raise BuildTransactionFailure(
                f"{purpose} requires a verified publication receipt"
            )
        return "provisional_unpublished"
    try:
        validate_publication_receipt(
            publication_receipt, expected_revision=revision
        )
    except (OSError, ValueError) as error:
        raise BuildTransactionFailure(str(error)) from None
    return "published_anonymously_reachable"


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 1800,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode:
        raise BuildTransactionFailure(
            f"command failed ({completed.returncode}): {' '.join(command[:4])}\n"
            f"{completed.stderr[-4000:]}"
        )
    return completed


def _clean_revision(root: Path, require_clean: bool) -> str:
    revision = _run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise BuildTransactionFailure("build requires an exact Git revision")
    status = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
    ).stdout
    if require_clean and status:
        raise BuildTransactionFailure("release-capable build requires a clean checkout")
    return revision


def _git_snapshot(root: Path, revision: str, destination: Path) -> Path:
    destination.mkdir(parents=True)
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", revision],
        cwd=root,
        stdout=subprocess.PIPE,
    )
    if archive.stdout is None:
        raise BuildTransactionFailure("cannot read Git archive")
    try:
        with tarfile.open(fileobj=archive.stdout, mode="r|") as source:
            source.extractall(destination, filter="data")
    finally:
        archive.stdout.close()
    if archive.wait() != 0:
        raise BuildTransactionFailure("cannot materialize exact Git snapshot")
    return destination


def _path_signature(root: Path, paths: tuple[str, ...]) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    for relative in paths:
        path = root / relative
        try:
            metadata = path.lstat()
            rows.append(
                (
                    relative,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ino,
                    os.readlink(path) if path.is_symlink() else None,
                )
            )
        except FileNotFoundError:
            rows.append((relative, "missing"))
    return tuple(rows)


class _BoundedSourceWatcher:
    """Detect sustained source writes; exact postflight remains authoritative."""

    def __init__(self, root: Path, paths: tuple[str, ...], interval: float = 0.1):
        self.root = root
        self.paths = paths
        self.interval = interval
        self.initial = _path_signature(root, paths)
        self.changed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            if _path_signature(self.root, self.paths) != self.initial:
                self.changed = True

    def __enter__(self) -> "_BoundedSourceWatcher":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def _extract_build_evidence_and_scan_secret(
    *, image: str, destination: Path, secret: str
) -> dict[str, object]:
    container = _run(["docker", "create", image]).stdout.strip()
    if not container:
        raise BuildTransactionFailure("Docker did not create an evidence container")
    archive = destination / "rootfs.tar"
    try:
        _run(
            [
                "docker",
                "cp",
                f"{container}:{BUILD_EVIDENCE_PATH}/.",
                str(destination),
            ]
        )
        _run(["docker", "export", "--output", str(archive), container], timeout=900)
        secret_bytes = secret.encode("utf-8")
        with archive.open("rb") as handle:
            tail = b""
            while block := handle.read(1024 * 1024):
                if secret_bytes in tail + block:
                    raise BuildTransactionFailure("build secret entered runtime filesystem")
                tail = (tail + block)[-max(len(secret_bytes) - 1, 0):]
    finally:
        _run(["docker", "rm", "--force", container], check=False)
        archive.unlink(missing_ok=True)
    required = {
        "build-context-received.json",
        "builder-toolchain.json",
        "build-contract.json",
        "buildkit-probe-consumed.json",
        "native-extensions.json",
        "pyswisseph-linux-source-build.json",
    }
    missing = sorted(name for name in required if not (destination / name).is_file())
    if missing:
        raise BuildTransactionFailure(f"runtime image omitted build evidence: {missing}")
    return {
        name: json.loads((destination / name).read_text(encoding="utf-8"))
        for name in sorted(required)
    }


def _identity_delta(before: SourceTreeIdentity, after: SourceTreeIdentity) -> str:
    added = sorted(set(after.paths) - set(before.paths))
    removed = sorted(set(before.paths) - set(after.paths))
    return (
        f"paths_added={added}, paths_removed={removed}, "
        f"content_changed={before.content_sha256 != after.content_sha256}, "
        f"index_changed={before.index_sha256 != after.index_sha256}"
    )


def _read_evidence(directory: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads((directory / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildTransactionFailure(f"cannot read {name}: {error}") from None
    if not isinstance(value, dict):
        raise BuildTransactionFailure(f"{name} is not an object")
    return value


def compare_reproducibility_builds(
    candidate_dir: Path, comparison_dir: Path
) -> dict[str, Any]:
    candidate = _read_evidence(candidate_dir, "build-transaction.json")
    comparison = _read_evidence(comparison_dir, "build-transaction.json")
    if candidate.get("purpose") != "release-candidate" or comparison.get(
        "purpose"
    ) != "reproducibility-comparison":
        raise BuildTransactionFailure(
            "reproducibility comparison requires candidate then comparison purposes"
        )
    equal_axes = (
        "source_revision",
        "platform",
        "publication_status",
        "materialized_context_identity_sha256",
        "buildkit_context_identity_sha256",
    )
    drift = [axis for axis in equal_axes if candidate.get(axis) != comparison.get(axis)]
    if drift:
        raise BuildTransactionFailure(
            "reproducibility build inputs differ: " + ", ".join(drift)
        )
    candidate_toolchain = _read_evidence(candidate_dir, "builder-toolchain.json")
    comparison_toolchain = _read_evidence(comparison_dir, "builder-toolchain.json")
    if candidate_toolchain != comparison_toolchain:
        raise BuildTransactionFailure(
            "builder toolchain receipts differ; comparison is inconclusive and "
            "native-byte drift must not be attributed to the compiler"
        )
    candidate_native = _read_evidence(candidate_dir, "native-extensions.json")
    comparison_native = _read_evidence(comparison_dir, "native-extensions.json")
    candidate_artifacts = {
        item["path"]: item
        for item in candidate_native.get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    comparison_artifacts = {
        item["path"]: item
        for item in comparison_native.get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if candidate_artifacts.keys() != comparison_artifacts.keys():
        raise BuildTransactionFailure("native extension populations differ")
    changed = sorted(
        path
        for path in candidate_artifacts
        if candidate_artifacts[path] != comparison_artifacts[path]
    )
    source_built = sorted(
        path
        for path, item in candidate_artifacts.items()
        if item.get("origin_class") == "source_built_native"
    )
    if not source_built:
        raise BuildTransactionFailure("comparison has no source-built native extension")
    if changed:
        classes = {
            path: candidate_artifacts[path].get("origin_class") for path in changed
        }
        raise BuildTransactionFailure(
            "same-toolchain native extension bytes differ: "
            + json.dumps(classes, sort_keys=True)
        )
    return {
        "schema_version": "native-extension-reproducibility-comparison-v1",
        "status": "byte_identical_same_toolchain_scoped",
        "source_revision": candidate["source_revision"],
        "platform": candidate["platform"],
        "source_built_native_paths": source_built,
        "acquired_native_wheel_paths": sorted(
            path
            for path, item in candidate_artifacts.items()
            if item.get("origin_class") == "acquired_native_wheel"
        ),
        "native_artifact_count": len(candidate_artifacts),
        "base_image_elf_scope": "not_compared_here_bound_by_pinned_base_digest",
    }


def build_image(
    *,
    source_root: Path,
    image: str,
    platform: str,
    purpose: str,
    publication_receipt: Path | None,
    evidence_dir: Path,
    require_clean: bool = True,
) -> dict[str, Any]:
    source_root = source_root.resolve()
    evidence_dir = external_output_path(
        evidence_dir,
        source_root=source_root,
        role="build transaction evidence",
    )
    if evidence_dir.exists():
        raise BuildTransactionFailure("build evidence destination already exists")
    require_clean = clean_checkout_required(
        purpose=purpose,
        requested=require_clean,
    )
    revision = _clean_revision(source_root, require_clean)
    publication_status = publication_status_for_build(
        purpose=purpose,
        publication_receipt=publication_receipt,
        revision=revision,
    )
    before = observe_source_tree(source_root)
    maintained_paths = maintained_source_paths(source_root)
    started = time.time()
    secret = "armillary-build-probe-" + secrets.token_urlsafe(24)
    secret_sha256 = hashlib.sha256(secret.encode()).hexdigest()
    builder_name: str | None = None
    builder_inspection = "default diagnostic builder"
    with tempfile.TemporaryDirectory(prefix="armillary-build-transaction-") as raw:
        temporary = Path(raw)
        snapshot = _git_snapshot(source_root, revision, temporary / "snapshot")
        context = temporary / "context"
        materialize_context(
            snapshot,
            context,
            control_files={
                CONTEXT_DISCRIMINATION_PATH: CONTEXT_DISCRIMINATION_BYTES
            },
        )
        expected_context = context_manifest(context)
        secret_path = temporary / "build-secret"
        secret_path.write_text(secret, encoding="utf-8")
        command = [
            "docker",
            "buildx",
            "build",
            "--load",
            "--progress=plain",
            "--platform",
            platform,
            "--file",
            "deploy/Dockerfile",
            "--tag",
            image,
            "--build-arg",
            f"VCS_REF={revision}",
            "--build-arg",
            f"PUBLICATION_STATUS={publication_status}",
            "--secret",
            f"id=private_alpha_probe,src={secret_path}",
        ]
        if purpose in {"release-candidate", "reproducibility-comparison"}:
            builder_name = "armillary-release-" + secrets.token_hex(8)
            _run(
                [
                    "docker", "buildx", "create", "--name", builder_name,
                    "--driver", "docker-container",
                ],
                timeout=300,
            )
            try:
                builder_inspection = _run(
                    ["docker", "buildx", "inspect", "--bootstrap", builder_name],
                    timeout=300,
                ).stdout
            except Exception:
                _run(
                    ["docker", "buildx", "rm", "--force", builder_name],
                    timeout=300,
                    check=False,
                )
                raise
            command.extend(["--builder", builder_name, "--no-cache"])
        command.append(".")
        try:
            with _BoundedSourceWatcher(source_root, maintained_paths) as watcher:
                completed = _run(command, cwd=context)
        finally:
            if builder_name is not None:
                removed = _run(
                    ["docker", "buildx", "rm", "--force", builder_name],
                    timeout=300,
                    check=False,
                )
                if removed.returncode:
                    raise BuildTransactionFailure(
                        f"cannot remove isolated builder {builder_name}"
                    )
        if secret in completed.stdout or secret in completed.stderr:
            raise BuildTransactionFailure("build log disclosed the secret canary")
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "build.stdout.txt").write_text(
            completed.stdout, encoding="utf-8"
        )
        (evidence_dir / "build.stderr.txt").write_text(
            completed.stderr, encoding="utf-8"
        )
        evidence = _extract_build_evidence_and_scan_secret(
            image=image, destination=evidence_dir, secret=secret
        )
        try:
            image_inspection = json.loads(
                _run(["docker", "image", "inspect", image]).stdout
            )[0]
            image_id = image_inspection["Id"]
            image_labels = image_inspection["Config"]["Labels"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise BuildTransactionFailure(
                "cannot bind transaction to the built runtime image"
            ) from error
    observed_context = evidence["build-context-received.json"]
    assert isinstance(observed_context, dict)
    assert_context_receipts_match(expected_context, observed_context)
    contract = evidence["build-contract.json"]
    toolchain = evidence["builder-toolchain.json"]
    if not isinstance(contract, dict) or not isinstance(toolchain, dict):
        raise BuildTransactionFailure("runtime build evidence has invalid object types")
    assert_embedded_contract_consistent(
        contract=contract,
        context=observed_context,
        toolchain=toolchain,
        revision=revision,
        platform=platform,
    )
    probe = evidence["buildkit-probe-consumed.json"]
    if not isinstance(probe, dict) or probe != {
        "consumed": True,
        "nonempty": True,
        "secret_sha256": secret_sha256,
    }:
        raise BuildTransactionFailure("build secret receipt mismatch")
    after = observe_source_tree(source_root)
    if before != after:
        raise BuildTransactionFailure(
            "source identity changed during build: " + _identity_delta(before, after)
        )
    if watcher.changed:
        raise BuildTransactionFailure(
            "bounded source watcher observed a transient source-path change"
        )
    if (
        not isinstance(image_id, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        or image_labels.get("org.opencontainers.image.revision") != revision
        or image_labels.get("org.projectarmillary.publication.status")
        != publication_status
    ):
        raise BuildTransactionFailure(
            "built image identity or labels are not transaction-bound"
        )
    receipt = {
        "schema_version": "release-build-transaction-v1",
        "source_revision": revision,
        "image": image,
        "image_id": image_id,
        "platform": platform,
        "purpose": purpose,
        "publication_status": publication_status,
        "materialized_context_identity_sha256": expected_context["identity_sha256"],
        "buildkit_context_identity_sha256": observed_context.get("identity_sha256"),
        "secret_sha256": secret_sha256,
        "secret_present_in_log_or_runtime": False,
        "source_identity_unchanged": True,
        "builder_isolation": {
            "mode": (
                "ephemeral_docker_container_builder_no_cache"
                if builder_name is not None
                else "diagnostic_default_builder"
            ),
            "builder_name": builder_name,
            "inspection": builder_inspection,
        },
        "source_watcher": {
            "mode": "bounded_polling_plus_exact_postflight",
            "interval_seconds": watcher.interval,
            "transient_change_observed": watcher.changed,
            "limitation": "a create-and-delete shorter than the polling interval may escape",
        },
        "started_unix": started,
        "finished_unix": time.time(),
        "command": [
            "docker", "buildx", "build", "--load", "--progress=plain",
            "--platform", platform, "--file", "deploy/Dockerfile", "--tag", image,
            "--build-arg", f"VCS_REF={revision}", "--build-arg",
            f"PUBLICATION_STATUS={publication_status}", "--secret",
            "id=private_alpha_probe,src=<redacted>",
            *(
                ["--builder", "<ephemeral>", "--no-cache"]
                if purpose != "diagnostic"
                else []
            ),
            ".",
        ],
    }
    (evidence_dir / "build-transaction.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--purpose", choices=BUILD_PURPOSES, required=True)
    parser.add_argument("--publication-receipt", type=Path)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--compare-to-evidence-dir", type=Path)
    args = parser.parse_args()
    try:
        receipt = build_image(
            source_root=args.source_root,
            image=args.image,
            platform=args.platform,
            purpose=args.purpose,
            publication_receipt=args.publication_receipt,
            evidence_dir=args.evidence_dir,
            require_clean=args.require_clean,
        )
        if args.compare_to_evidence_dir is not None:
            if args.purpose != "reproducibility-comparison":
                raise BuildTransactionFailure(
                    "--compare-to-evidence-dir requires reproducibility-comparison"
                )
            comparison = compare_reproducibility_builds(
                args.compare_to_evidence_dir.resolve(), args.evidence_dir.resolve()
            )
            (args.evidence_dir / "reproducibility-comparison.json").write_text(
                json.dumps(comparison, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    except (BuildTransactionFailure, OSError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"RELEASE BUILD FAILED: {error}", file=sys.stderr)
        return 1
    print(
        "RELEASE BUILD TRANSACTION COMPLETE "
        f"revision={receipt['source_revision']} purpose={receipt['purpose']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
