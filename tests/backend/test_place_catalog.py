"""Reproducibility, priority, and fail-closed tests for the place catalog."""

from __future__ import annotations

import csv
import json
import sqlite3
import zipfile

from fastapi.testclient import TestClient

from app.core.place_catalog import PlaceCatalog
from app.main import create_app
from app.settings import AppProfile, AppSettings
from scripts.validation.build_place_catalog import build_catalog


TAIWAN_FIELDS = [
    "Type",
    "PlaceName",
    "ChinesePhonetic",
    "CommonPhonetic",
    "AnotherName",
    "County",
    "CountyCode",
    "Town",
    "TownCode",
    "Village",
    "PlaceMean",
    "Longitude",
    "Latitude",
]


def _write_taiwan_csv(path, row):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TAIWAN_FIELDS)
        writer.writeheader()
        writer.writerow(row)
        writer.writerow(row)  # exact source duplicate must be deterministic


def _build_fixture_catalog(tmp_path):
    geonames = tmp_path / "cities500.zip"
    fields = [
        "1668399",
        "Taichung",
        "Taichung",
        "臺中,台中市",
        "24.1469",
        "120.6839",
        "P",
        "PPLA",
        "TW",
        "",
        "04",
        "",
        "",
        "",
        "2800000",
        "",
        "80",
        "Asia/Taipei",
        "2026-07-29",
    ]
    with zipfile.ZipFile(geonames, "w") as archive:
        archive.writestr("cities500.txt", "\t".join(fields) + "\n")

    taiwan_row = {
        "Type": "聚落",
        "PlaceName": "臺中",
        "ChinesePhonetic": "Taizhong",
        "CommonPhonetic": "Taichung",
        "AnotherName": "台中",
        "County": "臺中市",
        "CountyCode": "66000",
        "Town": "中區",
        "TownCode": "66000010",
        "Village": "中區",
        "PlaceMean": "",
        "Longitude": "120.6800",
        "Latitude": "24.1400",
    }
    admin = tmp_path / "admin.csv"
    settlement = tmp_path / "settlement.csv"
    _write_taiwan_csv(admin, taiwan_row)
    _write_taiwan_csv(settlement, taiwan_row)
    output = tmp_path / "places.sqlite3"
    metadata = build_catalog(
        geonames_zip=geonames,
        taiwan_admin_csv=admin,
        taiwan_settlement_csv=settlement,
        output=output,
        source_date="2026-07-29",
    )
    return output, metadata


def test_builder_deduplicates_only_exact_same_source_rows_and_keeps_overlays(
    tmp_path,
):
    output, metadata = _build_fixture_catalog(tmp_path)

    assert metadata["row_counts"] == {
        "geonames_cities500": 1,
        "taiwan_moi_administrative": 1,
        "taiwan_moi_settlement": 1,
    }
    results = PlaceCatalog(output).search(
        query="臺中",
        country_code="TW",
        limit=10,
    )["results"]
    assert [item["source"] for item in results] == [
        "taiwan_moi_place_names",
        "taiwan_moi_place_names",
        "geonames_cities500",
    ]
    assert len({item["source_record_id"] for item in results}) == 3


def test_runtime_catalog_connection_is_read_only(tmp_path):
    output, _metadata = _build_fixture_catalog(tmp_path)
    catalog = PlaceCatalog(output)
    connection = catalog._connect()
    try:
        try:
            connection.execute("CREATE TABLE forbidden(value TEXT)")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower()
        else:
            raise AssertionError("immutable place catalog accepted a write")
    finally:
        connection.close()


def test_bundled_catalog_ranks_major_city_before_incidental_admin_matches():
    results = PlaceCatalog().search(
        query="臺中",
        country_code="TW",
        limit=5,
    )["results"]

    assert results[0]["source"] == "geonames_cities500"
    assert results[0]["name"] == "Taichung"
    assert any(
        item["source"] == "taiwan_moi_place_names"
        for item in results[1:]
    )


def test_missing_catalog_fails_closed_without_runtime_download(tmp_path):
    application = create_app(
        AppSettings(
            profile=AppProfile.PRIVATE_ALPHA,
            place_catalog_path=str(tmp_path / "missing.sqlite3"),
        )
    )

    with TestClient(application) as client:
        response = client.post(
            "/api/places/search",
            json={"query": "Taipei"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "place_catalog_unavailable"}
    }


def test_hosted_place_search_events_and_validation_do_not_echo_query():
    events = []
    application = create_app(
        AppSettings(profile=AppProfile.PRIVATE_ALPHA),
        event_emitter=lambda event: events.append(event) or True,
    )
    secret_query = "PRIVATE-BIRTHPLACE-QUERY-DO-NOT-LOG"

    with TestClient(application) as client:
        successful = client.post(
            "/api/places/search",
            json={"query": secret_query},
        )
        rejected = client.post(
            "/api/places/search",
            json={"query": secret_query * 10},
        )

    assert successful.status_code == 200
    assert rejected.status_code == 422
    assert secret_query not in json.dumps(
        events,
        ensure_ascii=False,
    )
    assert secret_query not in rejected.text
    assert all("body" not in event for event in events)
    assert events
    assert all(
        event["route"] == "/api/places/search"
        for event in events
    )


def test_search_expression_bounds_prefix_breadth_and_token_count():
    """A 1-char prefix term is the dominant cost multiplier; many of them
    multiply it.  Measured before this bound: 'a' ~260 ms and 50 single-char
    tokens ~3.7 s, against 'taipei' ~7 ms on the same catalog.  Request-count
    rate limiting cannot bound a ~400x per-request spread, so the expression
    itself is bounded."""
    from app.core.place_catalog import MAX_SEARCH_TOKENS, _fts_query

    # A single character stays an exact term: no prefix expansion.
    assert _fts_query("a") == '"a"'
    # Two or more characters keep prefix expansion, so ordinary typing works.
    assert _fts_query("ta") == '"ta"*'
    assert _fts_query("taipei") == '"taipei"*'
    assert _fts_query("臺中") == '"臺中"*'

    # The token count is capped, so a 100-character body cannot request an
    # unbounded number of prefix intersections.
    many = _fts_query(" ".join("abcdefghij"))
    assert many.count("*") <= MAX_SEARCH_TOKENS
    assert len(many.split()) == MAX_SEARCH_TOKENS

    # The worst previously observed input shape is now bounded and contains no
    # prefix wildcard at all.
    worst = _fts_query("a " * 50)
    assert "*" not in worst
    assert len(worst.split()) <= MAX_SEARCH_TOKENS

    # Tokenisation still strips every FTS5 metacharacter before quoting.
    assert _fts_query('taipei" OR place_search MATCH "x') == '"taipei"* "or"* "place_search"* "match"* "x"'


def test_search_installs_a_statement_budget_because_sqlite_has_no_timeout():
    """`sqlite3.connect(timeout=...)` bounds lock acquisition, not statement
    runtime.  A progress handler is the only mechanism that can abort a running
    statement, so it is the fail-closed backstop for a query shape the token
    bounds do not anticipate."""
    import sqlite3

    from app.core import place_catalog as module

    installed: list[int] = []
    real_connect = module.sqlite3.connect

    class _RecordingConnection:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def set_progress_handler(self, handler, n):
            installed.append(n)
            return self._wrapped.set_progress_handler(handler, n)

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def __setattr__(self, name, value):
            if name == "_wrapped":
                object.__setattr__(self, name, value)
            else:
                setattr(self._wrapped, name, value)

    def _wrapping_connect(*args, **kwargs):
        return _RecordingConnection(real_connect(*args, **kwargs))

    module.sqlite3.connect = _wrapping_connect  # type: ignore[assignment]
    try:
        module.PlaceCatalog().search(query="taipei", country_code=None, limit=5)
    finally:
        module.sqlite3.connect = real_connect  # type: ignore[assignment]

    assert installed, "no statement budget was installed for a catalog search"
    # The first install arms the budget; the trailing (None, 0) is the teardown
    # that keeps the handler from outliving this search.
    assert installed[0] > 0
    assert installed[-1] == 0
    assert module.SEARCH_STATEMENT_BUDGET_SECONDS > 0
    assert hasattr(sqlite3.Connection, "set_progress_handler")
