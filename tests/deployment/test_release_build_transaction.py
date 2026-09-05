from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import re
import time

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


ROOT = Path(__file__).resolve().parents[2]


def _transaction_module():
    return importlib.import_module("scripts.verification.build_release_image")


def test_context_receipt_distinguishes_path_type_executable_link_size_and_bytes(tmp_path):
    module = _transaction_module()
    root = tmp_path / "context"
    root.mkdir()
    (root / "empty").mkdir(mode=0o750)
    payload = root / "payload.txt"
    payload.write_text("payload\n", encoding="utf-8")
    payload.chmod(0o640)
    (root / "alias").symlink_to("payload.txt")

    receipt = module.context_manifest(root)
    by_path = {entry["path"]: entry for entry in receipt["entries"]}

    assert by_path["empty"]["type"] == "directory"
    assert by_path["empty"]["executable"] is True
    assert by_path["payload.txt"] == {
        "path": "payload.txt",
        "type": "file",
        "executable": False,
        "size_bytes": 8,
        "sha256": "d4e4877bac978b7952f0d544fc52ebff5411d351d129f1f056fa43f11da9af2b",
        "symlink_target": None,
    }
    assert by_path["alias"]["type"] == "symlink"
    assert by_path["alias"]["symlink_target"] == "payload.txt"


def test_both_sides_of_the_context_comparison_have_one_producer(tmp_path):
    """Host and in-image context receipts import the same observer."""
    module = _transaction_module()
    root = tmp_path / "context"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "payload.txt").write_text("payload\n", encoding="utf-8")
    script = root / "nested" / "runnable.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)

    spec = importlib.util.spec_from_file_location(
        "capture_build_evidence_copy",
        PROJECT_ROOT / "scripts" / "verification" / "capture_build_evidence.py",
    )
    assert spec is not None and spec.loader is not None
    in_image = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(in_image)

    module.assert_context_receipts_match(
        module.context_manifest(root),
        in_image.context_manifest(root),
    )

    # And they must stay one producer, not two that happen to agree today.
    installed = importlib.import_module("scripts.verification.capture_build_evidence")
    assert module.context_manifest is installed.context_manifest


@pytest.mark.parametrize("mutation", ("extra", "missing", "renamed", "changed"))
def test_buildkit_context_comparison_rejects_adjacent_manifest_drift(mutation):
    module = _transaction_module()
    expected = {
        "schema_version": "build-context-identity-v1",
        "entries": [
            {
                "path": "ordinary.txt",
                "type": "file",
                "executable": False,
                "size_bytes": 2,
                "sha256": "a" * 64,
                "symlink_target": None,
            }
        ],
    }
    observed = json.loads(json.dumps(expected))
    if mutation == "extra":
        observed["entries"].append({**observed["entries"][0], "path": ".env"})
    elif mutation == "missing":
        observed["entries"] = []
    elif mutation == "renamed":
        observed["entries"][0]["path"] = "renamed.txt"
    else:
        observed["entries"][0]["sha256"] = "b" * 64

    with pytest.raises(module.BuildTransactionFailure, match="BuildKit context"):
        module.assert_context_receipts_match(expected, observed)

    module.assert_context_receipts_match(expected, expected)

    with pytest.raises(module.BuildTransactionFailure, match="entries are invalid"):
        module.assert_context_receipts_match(
            {"entries": "not-a-list"},
            observed,
        )


def test_a_context_mismatch_names_the_field_that_differs_not_only_the_paths():
    """A context mismatch names fields and values, not only affected paths."""
    module = _transaction_module()
    expected = {
        "entries": [
            {"path": "a", "mode": "0644", "type": "file", "sha256": "a" * 64},
            {"path": "b", "mode": "0755", "type": "directory"},
        ]
    }
    observed = {
        "entries": [
            {"path": "a", "mode": "0664", "type": "file", "sha256": "a" * 64},
            {"path": "b", "mode": "0775", "type": "directory"},
        ]
    }

    with pytest.raises(module.BuildTransactionFailure) as failure:
        module.assert_context_receipts_match(expected, observed)

    message = str(failure.value)
    assert "differing_fields=['mode']" in message
    assert "changed_count=2" in message
    assert "'0644'" in message and "'0664'" in message


def test_embedded_build_contract_binds_context_and_toolchain():
    module = _transaction_module()
    from scripts.verification.capture_build_evidence import canonical_json_bytes

    assert canonical_json_bytes({"z": "é", "a": 1}) == (
        b'{"a":1,"z":"\\u00e9"}'
    )
    revision = "a" * 40
    platform = "linux/amd64"
    context = {"identity_sha256": "b" * 64}
    toolchain = {"schema_version": "builder-toolchain-receipt-v1"}
    toolchain_identity = __import__("hashlib").sha256(
        canonical_json_bytes(toolchain)
    ).hexdigest()
    contract = {
        "schema_version": "build-contract-v1",
        "source_revision": revision,
        "target_platform": platform,
        "build_context_identity_sha256": "b" * 64,
        "toolchain_identity_sha256": toolchain_identity,
    }

    module.assert_embedded_contract_consistent(
        contract=contract,
        context=context,
        toolchain=toolchain,
        revision=revision,
        platform=platform,
    )
    for key in (
        "source_revision",
        "target_platform",
        "build_context_identity_sha256",
        "toolchain_identity_sha256",
    ):
        changed = dict(contract)
        changed[key] = "wrong"
        with pytest.raises(
            module.BuildTransactionFailure,
            match="inconsistent with embedded evidence",
        ):
            module.assert_embedded_contract_consistent(
                contract=changed,
                context=context,
                toolchain=toolchain,
                revision=revision,
                platform=platform,
            )


def test_release_build_requires_verified_publication_receipt(tmp_path):
    module = _transaction_module()

    assert module.publication_status_for_build(
        purpose="diagnostic", publication_receipt=None, revision="a" * 40
    ) == "provisional_unpublished"
    with pytest.raises(module.BuildTransactionFailure, match="publication receipt"):
        module.publication_status_for_build(
            purpose="release-candidate",
            publication_receipt=None,
            revision="a" * 40,
        )
    provisional = tmp_path / "SOURCE_EXPORT.json"
    provisional.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "public_revision_intentionally_out_of_band",
                "private_source_revision": "a" * 40,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.BuildTransactionFailure, match="key set"):
        module.publication_status_for_build(
            purpose="release-candidate",
            publication_receipt=provisional,
            revision="a" * 40,
        )


def test_release_build_cannot_disable_clean_checkout():
    module = _transaction_module()

    assert module.clean_checkout_required(
        purpose="diagnostic", requested=False
    ) is False
    assert module.clean_checkout_required(
        purpose="diagnostic", requested=True
    ) is True
    assert module.clean_checkout_required(
        purpose="release-candidate", requested=False
    ) is True


def test_only_canonical_owner_executes_release_capable_docker_build():
    owner = "scripts/verification/build_release_image.py"
    offenders = []
    for path in (ROOT / "scripts").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if re.search(
            r'["\']docker["\']\s*,\s*["\']buildx["\']\s*,\s*["\']build["\']',
            text,
        ) or re.search(r'["\']docker["\']\s*,\s*["\']build["\']', text):
            if relative != owner:
                offenders.append(relative)
    assert offenders == []


def test_toolchain_receipt_is_a_populated_object(monkeypatch):
    producer = importlib.import_module(
        "scripts.verification.capture_build_evidence"
    )
    monkeypatch.setattr(
        producer,
        "_command",
        lambda *arguments: {
            "argv": list(arguments),
            "status": "evaluated",
            "returncode": 0,
            "stdout": "controlled\n",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        producer,
        "_executable",
        lambda name: {"name": name, "status": "evaluated", "sha256": "a" * 64},
    )
    monkeypatch.setattr(producer, "_os_release", lambda: {"ID": "controlled"})
    monkeypatch.setattr(
        producer.importlib.metadata,
        "distributions",
        lambda: (),
    )

    receipt = producer.toolchain_receipt(target_platform="linux/amd64")

    assert receipt["schema_version"] == "builder-toolchain-receipt-v1"
    assert receipt["TARGETPLATFORM"] == "linux/amd64"
    assert receipt["tools"]["gcc"]["identity"]["sha256"] == "a" * 64
    assert receipt["uname"]["argv"] == ["uname", "-srm"]


def test_builder_side_context_receipt_observes_actual_filesystem_axes(tmp_path):
    producer = importlib.import_module(
        "scripts.verification.capture_build_evidence"
    )
    root = tmp_path / "mounted-context"
    root.mkdir()
    payload = root / "payload.txt"
    payload.write_text("builder-visible\n", encoding="utf-8")
    payload.chmod(0o640)
    (root / "alias").symlink_to("payload.txt")

    receipt = producer.context_manifest(root)
    by_path = {entry["path"]: entry for entry in receipt["entries"]}

    assert receipt["producer"] == "builder_mount_observation"
    # Git tracks one permission bit, not the umask the checkout ran under, so
    # recording the full mode made this evidence differ between two correct
    # environments.  The executable bit is the part that is actually versioned.
    assert by_path["payload.txt"]["executable"] is False
    assert by_path["payload.txt"]["size_bytes"] == 16
    assert by_path["payload.txt"]["sha256"] == (
        "0a0ab8c4079c94bccbcf7faa554c90530fa25aa241f1cca486c8d7e97ea03e2c"
    )
    assert by_path["alias"]["type"] == "symlink"
    assert by_path["alias"]["symlink_target"] == "payload.txt"


def test_builder_side_native_receipt_classifies_and_requires_source_build(tmp_path):
    producer = importlib.import_module(
        "scripts.verification.capture_build_evidence"
    )
    venv = tmp_path / "venv"
    site = venv / "lib" / "python" / "site-packages"
    site.mkdir(parents=True)
    (site / "swisseph.cpython-test.so").write_bytes(b"source-built")
    core = site / "pydantic_core"
    core.mkdir()
    (core / "_pydantic_core.cpython-test.so").write_bytes(b"wheel")

    receipt = producer.native_extension_receipt(venv)
    by_path = {entry["path"]: entry for entry in receipt["artifacts"]}
    assert by_path[
        "/opt/venv/lib/python/site-packages/swisseph.cpython-test.so"
    ]["origin_class"] == "source_built_native"
    assert by_path[
        "/opt/venv/lib/python/site-packages/pydantic_core/"
        "_pydantic_core.cpython-test.so"
    ]["origin_class"] == "acquired_native_wheel"

    (site / "swisseph.cpython-test.so").unlink()
    with pytest.raises(RuntimeError, match="source-built swisseph"):
        producer.native_extension_receipt(venv)


def test_gate_d_binds_installed_native_bytes_to_builder_receipt():
    verifier = importlib.import_module(
        "scripts.verification.verify_container_runtime"
    )
    receipt = {
        "schema_version": "builder-native-extensions-v1",
        "artifacts": [
            {
                "path": "/opt/venv/swisseph.cpython-314.so",
                "origin_class": "source_built_native",
                "sha256": "a" * 64,
            }
        ],
    }
    verifier._assert_native_extension_receipt_matches_image(
        receipt,
        {"/opt/venv/swisseph.cpython-314.so": "a" * 64},
    )
    with pytest.raises(verifier.GateFailure, match="differ"):
        verifier._assert_native_extension_receipt_matches_image(
            receipt,
            {"/opt/venv/swisseph.cpython-314.so": "b" * 64},
        )
    with pytest.raises(verifier.GateFailure, match="paths"):
        verifier._assert_native_extension_receipt_matches_image(receipt, {})


def test_bounded_source_watcher_observes_a_sustained_source_write(tmp_path):
    module = _transaction_module()
    source = tmp_path / "source.py"
    source.write_text("before\n", encoding="utf-8")
    with module._BoundedSourceWatcher(
        tmp_path, ("source.py",), interval=0.01
    ) as watcher:
        source.write_text("after\n", encoding="utf-8")
        time.sleep(0.04)
    assert watcher.changed is True
