#!/usr/bin/env python3
"""Verify the immutable place catalog against its tracked manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "backend" / "place_data"
MANIFEST_PATH = DATA_DIR / "catalog_manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected_distribution = {
        "private_git": "direct",
        "docker": "same_exact_file",
        "publication": "same_exact_file",
        "ci": "verify_manifest_hash_rows_and_integrity",
        "local_runtime": "same_exact_file",
    }
    if (
        manifest.get("artifact_id") != "offline-place-catalog-v1"
        or manifest.get("classification") != "generated_runtime_dataset"
        or manifest.get("producer") != "scripts/validation/build_place_catalog.py"
        or manifest.get("generator_version") != "place-catalog-builder-v1"
        or manifest.get("mutable") is not False
        or manifest.get("distribution") != expected_distribution
        or manifest.get("release_policy")
        != "low_frequency_intentional_dataset_release"
        or manifest.get("rebuild", {}).get("exact_inputs_required") is not True
    ):
        raise SystemExit("place catalog governance manifest is incomplete")
    catalog_path = DATA_DIR / manifest["catalog"]["filename"]
    if not catalog_path.is_file():
        raise SystemExit(f"missing place catalog: {catalog_path}")
    if catalog_path.stat().st_size != manifest["catalog"]["size_bytes"]:
        raise SystemExit("place catalog size does not match manifest")
    if _sha256(catalog_path) != manifest["catalog"]["sha256"]:
        raise SystemExit("place catalog SHA-256 does not match manifest")
    for suffix in ("-journal", "-wal", "-shm"):
        if Path(f"{catalog_path}{suffix}").exists():
            raise SystemExit(f"mutable SQLite sidecar exists: {catalog_path}{suffix}")

    connection = sqlite3.connect(
        f"{catalog_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("place catalog SQLite integrity_check failed")
        counts = {
            row["source"]: row["count"]
            for row in connection.execute(
                """
                SELECT
                    CASE
                        WHEN source = 'geonames_cities500'
                            THEN 'geonames_cities500'
                        WHEN source_record_id LIKE 'tw-moi-administrative:%'
                            THEN 'taiwan_moi_administrative'
                        WHEN source_record_id LIKE 'tw-moi-settlement:%'
                            THEN 'taiwan_moi_settlement'
                    END AS source,
                    COUNT(*) AS count
                FROM places
                GROUP BY 1
                """
            )
        }
        if counts != manifest["row_counts"]:
            raise SystemExit(
                f"place catalog row counts differ: {counts!r}"
            )
        runtime_outbound = json.loads(
            connection.execute(
                """
                SELECT value_json FROM catalog_metadata
                WHERE key = 'runtime_outbound'
                """
            ).fetchone()[0]
        )
        if runtime_outbound is not False:
            raise SystemExit("place catalog claims runtime outbound")
        taiwan_hit = connection.execute(
            """
            SELECT p.source
            FROM place_search
            JOIN places AS p ON p.rowid = place_search.rowid
            WHERE place_search MATCH '"臺中"*'
              AND p.country_code = 'TW'
            ORDER BY p.source_priority ASC
            LIMIT 1
            """
        ).fetchone()
        if taiwan_hit is None or taiwan_hit["source"] != (
            "taiwan_moi_place_names"
        ):
            raise SystemExit("Taiwan overlay priority canary failed")
    finally:
        connection.close()

    print("PLACE CATALOG VERIFIED")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    verify()


if __name__ == "__main__":
    main()
