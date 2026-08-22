#!/usr/bin/env python3
"""Build the immutable GeoNames + Taiwan MOI place-search SQLite catalog."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import zipfile

# The backend package is not installed by the documented producer command,
# which runs from repository root without an explicit PYTHONPATH. Add the
# repository-relative backend package root, then
# import the *same* runtime function under its one canonical module name; using
# both ``app.*`` and ``backend.app.*`` makes mypy see one source file twice.
_BACKEND_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PACKAGE_ROOT))
normalize_search_text = importlib.import_module(
    "app.core.place_catalog"
).normalize_search_text


GEONAMES_SOURCE_URL = "https://download.geonames.org/export/dump/cities500.zip"
TAIWAN_ADMIN_SOURCE_URL = (
    "https://opdadm.moi.gov.tw/api/v1/no-auth/resource/api/dataset/"
    "C8DB1578-7554-4DBA-82AD-AD0105992C68/resource/"
    "83911F2D-F552-4E6B-99D0-6267FC9CD0E5/download"
)
TAIWAN_SETTLEMENT_SOURCE_URL = (
    "https://opdadm.moi.gov.tw/api/v1/no-auth/resource/api/dataset/"
    "4A5AB0C9-0395-4B04-AE50-02624075516F/resource/"
    "AE5B85B6-0895-4D32-8027-1713F018A649/download"
)
GENERATOR_VERSION = "place-catalog-builder-v1"
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_LINE_CHARS = 1_000_000
MAX_SOURCE_ROWS = 10_000_000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024 * 1024


def _require_bounded_input(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"input must be a regular non-symlink file: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds bounded byte limit: {path.name}")


def _taiwan_character_variants(value: str) -> str:
    """Index both common 臺/台 spellings without a runtime conversion service."""

    return " ".join(
        dict.fromkeys(
            (
                value,
                value.replace("台", "臺"),
                value.replace("臺", "台"),
            )
        )
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        CREATE TABLE places (
            source TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            country_code TEXT NOT NULL,
            admin1 TEXT,
            admin2 TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            timezone TEXT NOT NULL,
            location_precision TEXT NOT NULL,
            population INTEGER NOT NULL,
            source_priority INTEGER NOT NULL,
            UNIQUE(source, source_record_id)
        );
        CREATE VIRTUAL TABLE place_search USING fts5(
            name,
            aliases,
            admin1,
            admin2,
            content='',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE catalog_metadata (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );
        """
    )


def _insert(connection: sqlite3.Connection, record: dict) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO places (
            source, source_record_id, name, normalized_name, display_name,
            country_code, admin1, admin2, latitude, longitude, timezone,
            location_precision, population, source_priority
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["source"],
            record["source_record_id"],
            record["name"],
            normalize_search_text(record["name"]),
            record["display_name"],
            record["country_code"],
            record["admin1"],
            record["admin2"],
            record["latitude"],
            record["longitude"],
            record["timezone"],
            record["location_precision"],
            record["population"],
            record["source_priority"],
        ),
    )
    if cursor.rowcount == 0:
        return False
    rowid = cursor.lastrowid
    connection.execute(
        """
        INSERT INTO place_search(rowid, name, aliases, admin1, admin2)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            rowid,
            normalize_search_text(
                _taiwan_character_variants(
                    f"{record['name']} {record['display_name']}"
                )
            ),
            normalize_search_text(
                _taiwan_character_variants(record["aliases"])
            ),
            normalize_search_text(record["admin1"] or ""),
            normalize_search_text(record["admin2"] or ""),
        ),
    )
    return True


def _geonames_lines(path: Path):
    _require_bounded_input(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [info for info in archive.infolist() if info.filename.endswith(".txt")]
            if len(members) != 1:
                raise ValueError(
                    "GeoNames archive must contain exactly one text file"
                )
            member = members[0]
            if member.file_size > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("GeoNames member exceeds bounded uncompressed limit")
            with archive.open(member) as raw:
                for line in io.TextIOWrapper(raw, encoding="utf-8"):
                    if len(line) > MAX_LINE_CHARS:
                        raise ValueError("GeoNames row exceeds bounded line limit")
                    yield line
    else:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if len(line) > MAX_LINE_CHARS:
                    raise ValueError("GeoNames row exceeds bounded line limit")
                yield line


def import_geonames(
    connection: sqlite3.Connection,
    path: Path,
) -> int:
    count = 0
    for line in _geonames_lines(path):
        if count >= MAX_SOURCE_ROWS:
            raise ValueError("GeoNames input exceeds bounded row limit")
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 19:
            raise ValueError("invalid GeoNames cities500 row")
        (
            geonameid,
            name,
            ascii_name,
            alternate_names,
            latitude,
            longitude,
            _feature_class,
            _feature_code,
            country_code,
            _cc2,
            admin1,
            admin2,
            _admin3,
            _admin4,
            population,
            _elevation,
            _dem,
            timezone,
            _modified,
        ) = fields[:19]
        aliases = " ".join(
            item
            for item in (ascii_name, alternate_names.replace(",", " "))
            if item
        )
        display_parts = [name]
        if admin1:
            display_parts.append(admin1)
        display_parts.append(country_code)
        inserted = _insert(
            connection,
            {
                "source": "geonames_cities500",
                "source_record_id": geonameid,
                "name": name,
                "display_name": ", ".join(display_parts),
                "aliases": aliases,
                "country_code": country_code,
                "admin1": admin1 or None,
                "admin2": admin2 or None,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "timezone": timezone,
                "location_precision": "place_representative_point",
                "population": int(population or 0),
                "source_priority": 10,
            },
        )
        count += int(inserted)
    return count


def _taiwan_record_id(kind: str, row: dict) -> str:
    stable = "\x1f".join(
        str(row.get(key) or "")
        for key in (
            "Type",
            "PlaceName",
            "CountyCode",
            "TownCode",
            "Village",
            "Longitude",
            "Latitude",
        )
    ).encode("utf-8")
    return f"tw-moi-{kind}:{hashlib.sha256(stable).hexdigest()[:20]}"


def import_taiwan(
    connection: sqlite3.Connection,
    path: Path,
    *,
    kind: str,
    precision: str,
    source_priority: int,
) -> int:
    _require_bounded_input(path)
    count = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if reader.line_num > MAX_SOURCE_ROWS + 1:
                raise ValueError("Taiwan input exceeds bounded row limit")
            if not row.get("Longitude") or not row.get("Latitude"):
                continue
            aliases = " ".join(
                str(row.get(key) or "")
                for key in (
                    "ChinesePhonetic",
                    "CommonPhonetic",
                    "AnotherName",
                )
            )
            display_parts = [
                row["PlaceName"],
                row.get("Town") or "",
                row.get("County") or "",
            ]
            inserted = _insert(
                connection,
                {
                    "source": "taiwan_moi_place_names",
                    "source_record_id": _taiwan_record_id(kind, row),
                    "name": row["PlaceName"],
                    "display_name": ", ".join(
                        item for item in display_parts if item
                    ),
                    "aliases": aliases,
                    "country_code": "TW",
                    "admin1": row.get("County") or None,
                    "admin2": row.get("Town") or None,
                    "latitude": float(row["Latitude"]),
                    "longitude": float(row["Longitude"]),
                    "timezone": "Asia/Taipei",
                    "location_precision": precision,
                    "population": 0,
                    "source_priority": source_priority,
                },
            )
            count += int(inserted)
    return count


def build_catalog(
    *,
    geonames_zip: Path,
    taiwan_admin_csv: Path,
    taiwan_settlement_csv: Path,
    output: Path,
    source_date: str,
) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    working_output = output.with_name(f".{output.name}.building")
    if working_output.exists():
        working_output.unlink()
    connection = sqlite3.connect(working_output)
    try:
        _schema(connection)
        counts = {
            "geonames_cities500": import_geonames(
                connection,
                geonames_zip,
            ),
            "taiwan_moi_administrative": import_taiwan(
                connection,
                taiwan_admin_csv,
                kind="administrative",
                precision="administrative_area_representative_point",
                source_priority=1,
            ),
            "taiwan_moi_settlement": import_taiwan(
                connection,
                taiwan_settlement_csv,
                kind="settlement",
                precision="settlement_representative_point",
                source_priority=0,
            ),
        }
        metadata = {
            "schema_version": 1,
            "source_snapshot_date": source_date,
            "row_counts": counts,
            "licenses": {
                "geonames_cities500": "CC-BY-4.0",
                "taiwan_moi_place_names": (
                    "Taiwan Government Data Open License 1.0"
                ),
            },
            "sources": {
                "geonames_cities500": GEONAMES_SOURCE_URL,
                "taiwan_moi_administrative": TAIWAN_ADMIN_SOURCE_URL,
                "taiwan_moi_settlement": TAIWAN_SETTLEMENT_SOURCE_URL,
            },
            "source_sha256": {
                "geonames_cities500": sha256_file(geonames_zip),
                "taiwan_moi_administrative": sha256_file(
                    taiwan_admin_csv
                ),
                "taiwan_moi_settlement": sha256_file(
                    taiwan_settlement_csv
                ),
            },
            "runtime_outbound": False,
        }
        connection.executemany(
            """
            INSERT INTO catalog_metadata(key, value_json)
            VALUES (?, ?)
            """,
            [
                (
                    key,
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for key, value in metadata.items()
            ],
        )
        connection.commit()
        connection.execute("VACUUM")
        if working_output.stat().st_size > MAX_OUTPUT_BYTES:
            raise ValueError("place catalog exceeds bounded output limit")
    except Exception:
        connection.close()
        working_output.unlink(missing_ok=True)
        raise
    finally:
        if working_output.exists():
            connection.close()
    os.replace(working_output, output)
    return metadata


def build_artifact_manifest(
    metadata: dict,
    *,
    catalog_path: Path,
    source_date: str,
) -> dict:
    return {
        "artifact_id": "offline-place-catalog-v1",
        "classification": "generated_runtime_dataset",
        "producer": "scripts/validation/build_place_catalog.py",
        "generator_version": GENERATOR_VERSION,
        "mutable": False,
        "distribution": {
            "private_git": "direct",
            "docker": "same_exact_file",
            "publication": "same_exact_file",
            "ci": "verify_manifest_hash_rows_and_integrity",
            "local_runtime": "same_exact_file",
        },
        "release_policy": "low_frequency_intentional_dataset_release",
        "rebuild": {
            "command": (
                "python -m scripts.validation.build_place_catalog "
                "--geonames-zip <cities500.zip> "
                "--taiwan-admin-csv <taiwan-admin.csv> "
                "--taiwan-settlement-csv <taiwan-settlement.csv> "
                "--output backend/place_data/places.sqlite3 "
                "--manifest backend/place_data/catalog_manifest.json "
                f"--source-date {source_date}"
            ),
            "exact_inputs_required": True,
            "input_identity": "source_sha256",
        },
        "catalog": {
            "filename": catalog_path.name,
            "sha256": sha256_file(catalog_path),
            "size_bytes": catalog_path.stat().st_size,
        },
        "licenses": metadata["licenses"],
        "row_counts": metadata["row_counts"],
        "runtime_policy": {
            "catalog_mode": "bundled_read_only_sqlite",
            "runtime_outbound": metadata["runtime_outbound"],
            "user_data_written": False,
        },
        "schema_version": metadata["schema_version"],
        "source_sha256": metadata["source_sha256"],
        "source_snapshot_date": metadata["source_snapshot_date"],
        "sources": metadata["sources"],
    }


def write_artifact_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_name(f".{path.name}.building")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geonames-zip", type=Path, required=True)
    parser.add_argument("--taiwan-admin-csv", type=Path, required=True)
    parser.add_argument("--taiwan-settlement-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="default: catalog_manifest.json beside --output",
    )
    parser.add_argument("--source-date", required=True)
    args = parser.parse_args()
    metadata = build_catalog(
        geonames_zip=args.geonames_zip,
        taiwan_admin_csv=args.taiwan_admin_csv,
        taiwan_settlement_csv=args.taiwan_settlement_csv,
        output=args.output,
        source_date=args.source_date,
    )
    manifest_path = args.manifest or (
        args.output.parent / "catalog_manifest.json"
    )
    manifest = build_artifact_manifest(
        metadata,
        catalog_path=args.output,
        source_date=args.source_date,
    )
    write_artifact_manifest(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
