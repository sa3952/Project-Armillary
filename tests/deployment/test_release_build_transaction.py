from __future__ import annotations

import importlib
import json
from pathlib import Path
import re
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _transaction_module():
    return importlib.import_module("scripts.verification.build_release_image")


def test_context_receipt_distinguishes_path_type_mode_link_size_and_bytes(tmp_path):
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
    assert by_path["empty"]["mode"] == "0750"
    assert by_path["payload.txt"] == {
        "path": "payload.txt",
        "type": "file",
        "mode": "0640",
        "size_bytes": 8,
        "sha256": "d4e4877bac978b7952f0d544fc52ebff5411d351d129f1f056fa43f11da9af2b",
        "symlink_target": None,
    }
    assert by_path["alias"]["type"] == "symlink"
    assert by_path["alias"]["symlink_target"] == "payload.txt"


@pytest.mark.parametrize("mutation", ("extra", "missing", "renamed", "changed"))
def test_buildkit_context_comparison_rejects_adjacent_manifest_drift(mutation):
    module = _transaction_module()
    expected = {
        "schema_version": "build-context-identity-v1",
        "entries": [
            {
                "path": "ordinary.txt",
                "type": "file",
                "mode": "0644",
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


def test_embedded_build_contract_binds_context_and_toolchain():
    module = _transaction_module()
    revision = "a" * 40
    platform = "linux/amd64"
    context = {"identity_sha256": "b" * 64}
    toolchain = {"schema_version": "builder-toolchain-receipt-v1"}
    toolchain_identity = __import__("hashlib").sha256(
        json.dumps(toolchain, sort_keys=True, separators=(",", ":")).encode()
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


def test_release_and_comparison_builds_require_verified_publication_receipt(tmp_path):
    module = _transaction_module()

    assert module.publication_status_for_build(
        purpose="diagnostic", publication_receipt=None, revision="a" * 40
    ) == "provisional_unpublished"
    for purpose in ("release-candidate", "reproducibility-comparison"):
        with pytest.raises(module.BuildTransactionFailure, match="publication receipt"):
            module.publication_status_for_build(
                purpose=purpose,
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


def test_release_and_comparison_builds_cannot_disable_clean_checkout():
    module = _transaction_module()

    assert module.clean_checkout_required(
        purpose="diagnostic", requested=False
    ) is False
    assert module.clean_checkout_required(
        purpose="diagnostic", requested=True
    ) is True
    for purpose in ("release-candidate", "reproducibility-comparison"):
        assert module.clean_checkout_required(
            purpose=purpose, requested=False
        ) is True


def test_dockerfile_fail_closed_checks_do_not_depend_on_python_assert():
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(r"\bassert\b", dockerfile) is None
    assert "capture_build_evidence.py" in dockerfile
    assert "--context-root /build-context" in dockerfile
    assert "buildkit-probe-consumed.json" in dockerfile
    assert "COPY --from=builder /release" in dockerfile
    assert "FROM scratch AS release-evidence" not in dockerfile


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


def test_release_builds_use_ephemeral_builder_and_no_cache():
    owner = (
        ROOT / "scripts" / "verification" / "build_release_image.py"
    ).read_text(encoding="utf-8")
    assert '"buildx", "create"' in owner
    assert '"--driver", "docker-container"' in owner
    assert '"--builder", builder_name, "--no-cache"' in owner
    assert '"buildx", "rm", "--force", builder_name' in owner


def test_public_and_private_release_consumers_name_canonical_orchestration():
    private_overlay = ROOT / "publication" / "public_overlay"
    if private_overlay.is_dir():
        consumers = (
            ROOT / "README.md",
            private_overlay / "README.md",
            ROOT / ".github" / "workflows" / "private-engineering-ci.yml",
            private_overlay / ".github" / "workflows" / "production-amd64.yml",
        )
    else:
        consumers = (
            ROOT / "README.md",
            ROOT / ".github" / "workflows" / "production-amd64.yml",
        )
    for path in consumers:
        text = path.read_text(encoding="utf-8")
        assert "scripts.verification.build_release_image" in text, path
        assert "docker build " not in text, path

    if private_overlay.is_dir():
        private_workflow = (
            ROOT / ".github" / "workflows" / "private-engineering-ci.yml"
        ).read_text(encoding="utf-8")
        assert (
            "--container-only \\\n"
            "            --worker-resilience \\\n"
            "            --soak-requests 250 \\\n"
            "            --quiet"
        ) in private_workflow


def test_build_evidence_owner_records_required_toolchain_axes():
    producer = (
        ROOT / "scripts" / "verification" / "capture_build_evidence.py"
    ).read_text(encoding="utf-8")
    for required in (
        "dpkg-query",
        "gcc",
        "ld",
        "libc",
        "os-release",
        "TARGETPLATFORM",
        "source_revision",
    ):
        assert required in producer


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
    assert by_path["payload.txt"]["mode"] == "0640"
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


def test_reproducibility_comparison_separates_toolchain_and_native_drift(tmp_path):
    module = _transaction_module()
    candidate = tmp_path / "candidate"
    comparison = tmp_path / "comparison"
    candidate.mkdir()
    comparison.mkdir()
    common_transaction = {
        "schema_version": "release-build-transaction-v1",
        "source_revision": "a" * 40,
        "platform": "linux/amd64",
        "publication_status": "published_anonymously_reachable",
        "materialized_context_identity_sha256": "b" * 64,
        "buildkit_context_identity_sha256": "b" * 64,
    }
    native = {
        "schema_version": "builder-native-extensions-v1",
        "artifacts": [
            {
                "path": "/opt/venv/site-packages/swisseph.so",
                "origin_class": "source_built_native",
                "size_bytes": 10,
                "sha256": "c" * 64,
            },
            {
                "path": "/opt/venv/site-packages/pydantic_core.so",
                "origin_class": "acquired_native_wheel",
                "size_bytes": 20,
                "sha256": "d" * 64,
            },
        ],
    }
    for directory, purpose in (
        (candidate, "release-candidate"),
        (comparison, "reproducibility-comparison"),
    ):
        (directory / "build-transaction.json").write_text(
            json.dumps({**common_transaction, "purpose": purpose}), encoding="utf-8"
        )
        (directory / "builder-toolchain.json").write_text(
            json.dumps({"toolchain": "same"}), encoding="utf-8"
        )
        (directory / "native-extensions.json").write_text(
            json.dumps(native), encoding="utf-8"
        )

    result = module.compare_reproducibility_builds(candidate, comparison)
    assert result["status"] == "byte_identical_same_toolchain_scoped"
    assert result["source_built_native_paths"] == [
        "/opt/venv/site-packages/swisseph.so"
    ]

    (comparison / "builder-toolchain.json").write_text(
        json.dumps({"toolchain": "different"}), encoding="utf-8"
    )
    with pytest.raises(module.BuildTransactionFailure, match="inconclusive"):
        module.compare_reproducibility_builds(candidate, comparison)
    (comparison / "builder-toolchain.json").write_text(
        json.dumps({"toolchain": "same"}), encoding="utf-8"
    )
    changed = json.loads(json.dumps(native))
    changed["artifacts"][0]["sha256"] = "e" * 64
    (comparison / "native-extensions.json").write_text(
        json.dumps(changed), encoding="utf-8"
    )
    with pytest.raises(module.BuildTransactionFailure, match="same-toolchain"):
        module.compare_reproducibility_builds(candidate, comparison)


def test_runtime_verifier_extracts_evidence_from_image_without_another_build():
    verifier = (
        ROOT / "scripts" / "verification" / "verify_container_runtime.py"
    ).read_text(encoding="utf-8")
    function = verifier.split("def _exported_release_evidence", 1)[1].split(
        "\ndef ", 1
    )[0]
    assert "_image_evidence_json" in function
    assert "_build_image" not in function
    assert "build-context-received.json" in function
    assert "builder-toolchain.json" in function
