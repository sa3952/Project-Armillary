"""Reproducibility, priority, and fail-closed tests for the place catalog."""

from __future__ import annotations

import csv
import json
import sqlite3
import zipfile

import pytest
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


def test_catalog_builder_and_runtime_share_one_text_normalizer():
    """QA06-E-002: index and query normalization may not drift independently."""

    from app.core.place_catalog import normalize_search_text
    from scripts.validation import build_place_catalog

    assert build_place_catalog.normalize_search_text is normalize_search_text


def test_catalog_builder_rejects_oversized_source_before_parsing(tmp_path, monkeypatch):
    from scripts.validation import build_place_catalog

    source = tmp_path / "oversized.txt"
    source.write_bytes(b"12345")
    monkeypatch.setattr(build_place_catalog, "MAX_INPUT_BYTES", 4)

    with pytest.raises(ValueError, match="bounded byte limit"):
        list(build_place_catalog._geonames_lines(source))


def test_catalog_builder_rejects_zip_uncompressed_expansion(tmp_path, monkeypatch):
    from scripts.validation import build_place_catalog

    source = tmp_path / "expanded.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cities500.txt", "x" * 64)
    monkeypatch.setattr(build_place_catalog, "MAX_UNCOMPRESSED_BYTES", 32)

    with pytest.raises(ValueError, match="uncompressed"):
        list(build_place_catalog._geonames_lines(source))


@pytest.mark.parametrize(
    ("stored_name", "query"),
    [
        ("Saint-Denis", "Saint-Denis"),
        ("Stoke-on-Trent", "Stoke-on-Trent"),
        ("Xi'an", "Xi'an"),
        ("İstanbul", "istanbul"),
        ("São Paulo", "Sao Paulo"),
    ],
)
def test_builder_and_runtime_classify_punctuation_and_diacritic_names_exactly(
    tmp_path, stored_name, query
):
    """QA-RT-06-001/002: MATCH and tiering share one behavioural key."""

    geonames = tmp_path / "cities500.zip"
    fields = [
        "9000001", stored_name, stored_name, "", "10", "20", "P", "PPL",
        "ZZ", "", "01", "", "", "", "1000", "", "0", "Etc/UTC",
        "2026-08-06",
    ]
    with zipfile.ZipFile(geonames, "w") as archive:
        archive.writestr("cities500.txt", "\t".join(fields) + "\n")

    taiwan_row = {
        field: "" for field in TAIWAN_FIELDS
    }
    taiwan_row.update(
        Type="聚落", PlaceName="測試", County="測試縣", CountyCode="00000",
        Town="測試區", TownCode="00000000", Village="測試里",
        Longitude="120", Latitude="24",
    )
    admin = tmp_path / "admin.csv"
    settlement = tmp_path / "settlement.csv"
    _write_taiwan_csv(admin, taiwan_row)
    _write_taiwan_csv(settlement, taiwan_row)
    output = tmp_path / "places.sqlite3"
    build_catalog(
        geonames_zip=geonames,
        taiwan_admin_csv=admin,
        taiwan_settlement_csv=settlement,
        output=output,
        source_date="2026-08-06",
    )

    results = PlaceCatalog(output).search(
        query=query, country_code="ZZ", limit=5
    )["results"]
    assert results[0]["name"] == stored_name
    assert results[0]["match_tier"] == "exact_name"


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


# 真實語料排序測試（AUD-BASIC-COVERAGE-2026-08-03 §3.3 的回歸看守）。
#
# 舊排序把 bm25 放在 population 之前。`place_search` 是 contentless FTS5 表，
# bm25 偏好短文件，而 GeoNames 給大城市的 alternate names 有數百個語言變體，
# 於是「別名寫得少的偏僻小鎮」在相關度上贏過同名的大城市：實測輸入 London
# 得到的前五筆全是美國小鎮，倫敦排第六；巴黎與約克甚至進不了前六。
#
# 這些案例刻意選成「答案不會因資料集更新而改變」的類型：倫敦、巴黎、東京、
# 約克在其同名地點中的人口優勢是數十倍到數千倍，不是幾個百分點。
@pytest.mark.parametrize(
    "query,expected_display_name",
    [
        ("London", "London, ENG, GB"),
        ("Paris", "Paris, 11, FR"),
        ("York", "York, ENG, GB"),
        ("Tokyo", "Tokyo, 40, JP"),
        ("Cambridge", "Cambridge, ENG, GB"),
        ("Springfield", "Springfield, MO, US"),
        ("Taipei", "Taipei, 04, TW"),
    ],
)
def test_identically_named_places_rank_the_largest_first(
    query, expected_display_name
):
    results = PlaceCatalog().search(query=query, country_code=None, limit=5)[
        "results"
    ]
    assert results, query
    assert results[0]["display_name"] == expected_display_name
    assert results[0]["match_tier"] == "exact_name"
    # 同名者之間必須是人口遞減，否則代表 population 又被排到某個東西後面了。
    exact = [item for item in results if item["match_tier"] == "exact_name"]
    populations = [item["population"] for item in exact]
    assert populations == sorted(populations, reverse=True), query


@pytest.mark.parametrize(
    "query,expected_name",
    [
        ("臺北", "Taipei"),
        ("台中", "Taichung"),
        ("高雄", "Kaohsiung"),
    ],
)
def test_chinese_query_finds_the_major_city_ahead_of_zero_population_places(
    query, expected_name
):
    """中文查詢命中的是別名欄，不是地名欄，故不會落在 exact_name 層。

    這是 `population DESC` 必須排在 `match_tier` 的細分層**之前**的原因：
    只按層級排，臺北市那些人口為 0 的內政部地名（例如「臺北小別墅」因為
    名稱以「臺北」開頭而落在 name_prefix 層）會壓過人口 787 萬的臺北市本身。
    """

    results = PlaceCatalog().search(query=query, country_code=None, limit=5)[
        "results"
    ]
    assert results[0]["name"] == expected_name
    assert results[0]["population"] > 1_000_000


def test_ranking_receipt_names_the_method_and_its_order():
    """排序規則會改變使用者選到哪一個地點，進而改變整張星盤，故必須可具名回報。"""

    payload = PlaceCatalog().search(query="London", country_code=None, limit=1)
    ranking = payload["ranking"]
    assert ranking["method"] == "exact_name_then_population_then_weighted_bm25_v2"
    assert any("match_tier" in step for step in ranking["order"])
    assert any("population" in step for step in ranking["order"])


def test_query_containing_like_wildcards_is_not_treated_as_a_pattern():
    """前綴比對用 substr 而非 LIKE，故 % 與 _ 沒有萬用字元語意。

    若改用 LIKE 而忘了跳脫，輸入「%」會讓每一筆都落進 name_prefix 層，
    整個排序層級失效——而且不會有任何錯誤訊息。
    """

    catalog = PlaceCatalog()
    punctuated = catalog.search(
        query="Lon%", country_code=None, limit=5
    )["results"]
    plain = catalog.search(query="Lon", country_code=None, limit=5)["results"]
    percent_only = catalog.search(
        query="%", country_code=None, limit=5
    )["results"]

    # '%' is discarded before MATCH and tier classification.  It neither
    # broadens the query nor changes the result set; by itself it matches none.
    assert punctuated == plain
    assert percent_only == []


@pytest.mark.parametrize(
    "query",
    [
        "London!!!",
        "\uff2c\uff4f\uff4e\uff44\uff4f\uff4e",  # NFKC full-width spelling
    ],
)
def test_match_tier_uses_the_same_normalized_tokens_as_the_fts_query(query):
    """QA06-E-001: ranking must classify the query SQLite actually executed.

    Punctuation and FTS syntax are deliberately stripped before MATCH.  If the
    tier classifier instead compares the untouched input, an exact city name is
    falsely reported as ``other_field`` even though FTS executed ``london``.
    Existing expression tests separately prove no operator reaches MATCH.
    """

    results = PlaceCatalog().search(
        query=query,
        country_code="GB",
        limit=5,
    )["results"]

    assert results
    assert results[0]["display_name"] == "London, ENG, GB"
    assert results[0]["match_tier"] == "exact_name"


@pytest.mark.parametrize(
    "query,expected_display_name",
    [
        ("São Paulo", "São Paulo, 27, BR"),
        ("Sao Paulo", "São Paulo, 27, BR"),
        ("İstanbul", "Istanbul, 34, TR"),
        ("istanbul", "Istanbul, 34, TR"),
        ("臺北", "Taipei, 04, TW"),
        ("台北", "Taipei, 04, TW"),
    ],
)
def test_unicode_diacritic_and_taiwan_variants_reach_the_expected_major_city(
    query, expected_display_name
):
    """QA06-E-003: casefold-introduced combining marks stay in one FTS token."""

    results = PlaceCatalog().search(
        query=query,
        country_code=None,
        limit=5,
    )["results"]

    assert results
    assert results[0]["display_name"] == expected_display_name


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

    def expression(value):
        return _fts_query(value)[0]

    # A single character stays an exact term: no prefix expansion.
    assert expression("a") == '"a"'
    # Two or more characters keep prefix expansion, so ordinary typing works.
    assert expression("ta") == '"ta"*'
    assert expression("taipei") == '"taipei"*'
    assert expression("臺中") == '"臺中"*'

    # The token count is capped, so a 100-character body cannot request an
    # unbounded number of prefix intersections.
    many = expression(" ".join("abcdefghij"))
    assert many.count("*") <= MAX_SEARCH_TOKENS
    assert len(many.split()) == MAX_SEARCH_TOKENS

    # The worst previously observed input shape is now bounded and contains no
    # prefix wildcard at all.
    worst = expression("a " * 50)
    assert "*" not in worst
    assert len(worst.split()) <= MAX_SEARCH_TOKENS

    # Tokenisation still strips every FTS5 metacharacter before quoting.
    assert expression('taipei" OR place_search MATCH "x') == '"taipei"* "or"* "place_search"* "match"* "x"'


def test_prefix_cost_guard_uses_fts_effective_length_after_diacritic_removal():
    from app.core.place_catalog import _fts_query

    disguised_one_character = "e" + "\u0301" * 99
    expression, _receipt = _fts_query(disguised_one_character)

    assert not expression.endswith('"*')
    assert _fts_query("臺中")[0] == '"臺中"*'


def test_token_truncation_is_reported_instead_of_silently_dropping_the_query():
    """超過 token 上限的查詢字被丟掉時，必須說出來。

    地名選擇器裡這是正確性問題而不只是體驗問題：若查詢中真正有鑑別度的那個字
    落在上限之外，回應會像那個字從來沒被輸入過一樣，而使用者可能就接受了
    另一個地方的座標（RT-BACKEND-9-E-007）。
    """

    from app.core.place_catalog import _fts_query

    expression, receipt = _fts_query(
        "London London London London London London Taichung"
    )
    assert receipt["truncated"] is True
    assert receipt["token_count"] == 7
    assert receipt["tokens_ignored"] == ["taichung"]
    assert "taichung" not in expression.lower()
    assert receipt["reason"] is not None

    _expression, ordinary = _fts_query("Taichung")
    assert ordinary["truncated"] is False
    assert ordinary["tokens_ignored"] == []
    assert ordinary["reason"] is None


def test_search_response_carries_the_truncation_receipt():
    payload = PlaceCatalog().search(
        query="a b c d e f Taichung", country_code=None, limit=5
    )
    assert payload["query"]["truncated"] is True
    assert payload["query"]["tokens_ignored"] == ["taichung"]


def test_ranking_receipt_matches_the_order_the_sql_actually_uses():
    """自述錯誤等於可追溯性宣稱不實（RT-BACKEND-9-E-008）。

    實際 SQL 先只分「是否 exact_name」，人口排其後，完整三層細分排在人口之後。
    """

    order = PlaceCatalog().search(query="London", country_code=None, limit=1)[
        "ranking"
    ]["order"]
    assert len(order) == 6
    assert "is_exact_name" in order[0]
    assert "population DESC" in order[1]
    assert "match_tier" in order[2]
    assert "bm25" in order[3]
    assert "source_priority" in order[4]
    assert "display_name" in order[5]


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
