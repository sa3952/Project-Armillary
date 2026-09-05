#!/usr/bin/env python3
"""Build and verify immutable frontend release directories."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from scripts.tools.source_tree_identity import sha256_file as _sha256

try:
    # Prefer the canonical app package; clean public checkouts use the fallback.
    from app.frontend_release import (
        combined_release_id as _runtime_combined_release_id,
        artifact_digest as _runtime_artifact_digest,
        canonical_json_bytes,
        exact_contract as _runtime_exact_contract,
        FORMAL_LEGAL_FIELDS,
        load_publication_evidence,
        REVISION_RE,
        SOURCE_URL_PREFIX,
        verify_release as _runtime_verify_release,
    )
    from app.frontend_assets import discover_source_assets
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    from app.frontend_release import (
        combined_release_id as _runtime_combined_release_id,
        artifact_digest as _runtime_artifact_digest,
        canonical_json_bytes,
        exact_contract as _runtime_exact_contract,
        FORMAL_LEGAL_FIELDS,
        load_publication_evidence,
        REVISION_RE,
        SOURCE_URL_PREFIX,
        verify_release as _runtime_verify_release,
    )
    from app.frontend_assets import discover_source_assets


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "frontend-release.json"
FORMAL_LEGAL_CONTRACT = Path("docs/publication/formal_legal_release_copy.json")


def validate_publication_receipt(
    path: Path,
    *,
    expected_revision: str,
) -> dict[str, str]:
    """Consume evidence of publication instead of an operator assertion."""
    return load_publication_evidence(path, expected_revision=expected_revision)


def formal_legal_release_fields(
    source_root: Path,
    publication_evidence: dict[str, str] | None,
) -> dict[str, str] | None:
    """Bind release-owned legal fields to verified publication evidence."""
    if publication_evidence is None:
        return None
    path = source_root / FORMAL_LEGAL_CONTRACT
    if not path.is_file() or path.is_symlink():
        raise ValueError("formal legal release contract is missing or unsafe")
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"formal legal release contract is unreadable: {error}") from None
    machine_fields = contract.get("machine_owned_release_fields")
    if (
        contract.get("schema_version") != "formal-legal-release-copy-v1"
        or contract.get("status") != "current_release_copy"
        or contract.get("publication_disposition") != "publication_input"
        or not isinstance(machine_fields, dict)
        or set(machine_fields) != FORMAL_LEGAL_FIELDS
        or any(
            not isinstance(item, dict)
            or item.get("owner") != "verified publication/frontend release producer"
            or item.get("source_value") is not None
            for item in machine_fields.values()
        )
    ):
        raise ValueError("formal legal release contract machine ownership is invalid")
    return {
        "effective_date": publication_evidence["effective_date"],
        "release_version": publication_evidence["release_version"],
        "public_revision": publication_evidence["public_source_revision"],
        "source_archive_url": publication_evidence["source_archive_url"],
        "source_archive_sha256": publication_evidence["source_archive_sha256"],
    }


def _make_tree_removable(root: Path) -> None:
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
    ):
        os.chmod(directory, 0o755)
    os.chmod(root, 0o755)


def runtime_asset_names(source_root: Path = ROOT) -> set[str]:
    return set(discover_source_assets(source_root / "frontend"))


def exact_contract(source_root: Path = ROOT) -> dict[str, str | int]:
    return _runtime_exact_contract(source_root)


def _manifest_payload(
    *,
    revision: str,
    contract: dict[str, str | int],
    files: dict[str, dict[str, object]],
    publication_evidence: dict[str, str] | None,
    legal_release_fields: dict[str, str] | None,
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "artifact_type": "classical-astrology-frontend-release",
        "frontend_public_source_revision": revision,
        "source_url": f"{SOURCE_URL_PREFIX}{revision}",
        # Only a verified publication receipt may make an immutable public claim.
        "source_url_status": (
            "published_and_expected_anonymously_reachable"
            if publication_evidence is not None
            else "provisional_pending_publication"
        ),
        "source_publication": publication_evidence,
        "formal_legal_release_fields": legal_release_fields,
        "required_contracts": {
            "api_schema_version": contract["api_schema_version"],
            "dossier_version": contract["dossier_version"],
            "export_contract_version": contract["export_contract_version"],
        },
        "files": files,
    }


def build_release(
    *,
    source_root: Path,
    output_parent: Path,
    public_source_revision: str,
    require_clean_revision: bool = True,
    publication_receipt: Path | None = None,
) -> dict[str, object]:
    source_root = source_root.resolve()
    output_parent = Path(os.path.abspath(output_parent))
    if not REVISION_RE.fullmatch(public_source_revision):
        raise ValueError("frontend public source revision must be 40 lowercase hex")
    publication_evidence = (
        validate_publication_receipt(
            publication_receipt,
            expected_revision=public_source_revision,
        )
        if publication_receipt is not None
        else None
    )
    legal_release_fields = formal_legal_release_fields(
        source_root,
        publication_evidence,
    )
    if require_clean_revision:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=source_root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        if status:
            raise RuntimeError("frontend release requires a clean source tree")
        if revision != public_source_revision:
            raise RuntimeError(
                "frontend release revision differs from source HEAD"
            )
    frontend = source_root / "frontend"
    if not frontend.is_dir() or frontend.is_symlink():
        raise ValueError("frontend source must be a regular directory")
    assets = runtime_asset_names(source_root)
    contract = exact_contract(source_root)
    output_parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".frontend-release-", dir=output_parent))
    published = False
    try:
        files: dict[str, dict[str, object]] = {}
        for name in sorted(assets):
            source = frontend / name
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"required frontend asset is missing or unsafe: {name}")
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if require_clean_revision:
                committed = subprocess.run(
                    ["git", "show", f"{public_source_revision}:frontend/{name}"],
                    cwd=source_root,
                    check=True,
                    capture_output=True,
                ).stdout
                target.write_bytes(committed)
            else:
                shutil.copyfile(source, target)
            os.chmod(target, 0o444)
            files[name] = {
                "sha256": _sha256(target),
                "size_bytes": target.stat().st_size,
            }
        payload = _manifest_payload(
            revision=public_source_revision,
            contract=contract,
            files=files,
            publication_evidence=publication_evidence,
            legal_release_fields=legal_release_fields,
        )
        digest = _runtime_artifact_digest(payload)
        manifest = {**payload, "artifact_digest": digest}
        manifest_path = temporary / MANIFEST_NAME
        manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        os.chmod(manifest_path, 0o444)
        for directory in sorted(
            (path for path in temporary.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            os.chmod(directory, 0o555)
        os.chmod(temporary, 0o555)
        destination = output_parent / digest
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"frontend release already exists: {destination}")
        os.replace(temporary, destination)
        published = True
        verified = verify_release(destination, authority_root=source_root)
        return {**verified, "release_directory": str(destination)}
    finally:
        if not published and temporary.exists():
            _make_tree_removable(temporary)
            shutil.rmtree(temporary)


def verify_release(
    release_directory: Path,
    *,
    authority_root: Path = ROOT,
) -> dict[str, object]:
    return _runtime_verify_release(
        release_directory,
        authority_root=authority_root,
    )



combined_release_id = _runtime_combined_release_id



def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--output-parent", type=Path, required=True)
    build.add_argument("--public-source-revision", required=True)
    build.add_argument(
        "--publication-receipt",
        type=Path,
        help=(
            "verified Corresponding Source publication receipt for this exact "
            "revision; omission leaves the immutable manifest provisional"
        ),
    )
    verify = commands.add_parser("verify")
    verify.add_argument("--release-directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build_release(
            source_root=args.source_root,
            output_parent=args.output_parent,
            public_source_revision=args.public_source_revision,
            publication_receipt=args.publication_receipt,
        )
    else:
        result = verify_release(args.release_directory)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
