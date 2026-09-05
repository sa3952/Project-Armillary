"""Read-only bundled place catalog; no runtime geocoder or third-party request."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import unicodedata


DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[2]
    / "place_data"
    / "places.sqlite3"
)

# A single-character FTS5 prefix term matches a large fraction of a
# 200k-row catalog, and cost grows with the number of such terms: one
# 1-char prefix term measured ~260 ms while 50 of them measured ~3.7 s,
# against a 1.0-CPU container.  Request-count rate limiting cannot bound
# that, because a realistic term ("taipei") measured ~7 ms — a ~400x
# spread.  Two deterministic bounds are applied before the query runs.
MAX_SEARCH_TOKENS = 6
MIN_PREFIX_TOKEN_LENGTH = 2

# FTS5 的 bm25() 欄位權重。四個欄位依序為 name、aliases、admin1、admin2。
#
# 為什麼需要權重：`place_search` 是 contentless FTS5 表，bm25 只看索引，而 bm25
# 本質上偏好較短的文件。GeoNames 給倫敦的 alternate names 有數百個語言變體，
# 使 London(GB) 的文件長度遠大於美國那些同名小鎮，於是倫敦的 bm25 分數反而更差。
# 提高 name 欄的權重，讓「查詢字串命中的是地名本身」這件事主導相關度，
# 而不是讓「別名寫得少」變成優勢。
BM25_COLUMN_WEIGHTS = (8.0, 2.0, 1.0, 1.0)

# 排序法的具名版本。排序規則的變更會直接改變使用者選到哪一個地點、
# 進而改變整張星盤，因此必須是可具名、可比對、可回報的。
RANKING_METHOD_NAME = "exact_name_then_population_then_weighted_bm25_v2"
_BM25_SQL = ", ".join(str(value) for value in BM25_COLUMN_WEIGHTS)
RANKING_STEPS = (
    (
        "CASE WHEN match_tier = 'exact_name' THEN 0 ELSE 1 END ASC",
        "1. is_exact_name (exact_name → 0, otherwise → 1) ASC",
    ),
    ("population DESC", "2. population DESC"),
    (
        "CASE match_tier WHEN 'exact_name' THEN 0 "
        "WHEN 'name_prefix' THEN 1 ELSE 2 END ASC",
        "3. match_tier (exact_name → 0, name_prefix → 1, other_field → 2) ASC",
    ),
    (
        "bm25_rank ASC",
        f"4. bm25(name×{BM25_COLUMN_WEIGHTS[0]}, "
        f"aliases×{BM25_COLUMN_WEIGHTS[1]}, admin1×{BM25_COLUMN_WEIGHTS[2]}, "
        f"admin2×{BM25_COLUMN_WEIGHTS[3]}) ASC",
    ),
    ("source_priority ASC", "5. source_priority ASC"),
    ("display_name ASC", "6. display_name ASC"),
)
_ORDER_BY_SQL = ",\n                    ".join(
    sql for sql, _description in RANKING_STEPS
)
# Backstop for a query shape the token bounds do not anticipate.  SQLite
# has no statement timeout; the connection `timeout` argument bounds lock
# acquisition only.  A progress handler is the one mechanism that can
# abort an already-running statement.
SEARCH_STATEMENT_BUDGET_SECONDS = 2.0
_PROGRESS_HANDLER_INSTRUCTION_INTERVAL = 10_000


class PlaceCatalogUnavailableError(RuntimeError):
    code = "place_catalog_unavailable"


def normalize_search_text(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value).casefold().split()
    )


def _word_tokens(value: str) -> list[str]:
    r"""Return Unicode words without splitting casefold-introduced marks.

    Python ``\w`` excludes combining marks.  That made U+0130 casefold to
    ``i`` + COMBINING DOT ABOVE and then split ``İstanbul`` into two MATCH
    terms.  FTS5's unicode61 tokenizer treats marks as part of the word and
    removes the diacritic, so mirror that boundary while still discarding all
    FTS syntax characters before quoting terms.
    """

    normalized = normalize_search_text(value)
    token_text = "".join(
        character
        if character == "_"
        or unicodedata.category(character)[0] in {"L", "M", "N"}
        else " "
        for character in normalized
    )
    return token_text.split()


def place_name_match_key(value: str) -> str:
    """Return the punctuation/diacritic-insensitive key used for result tiers.

    FTS5 ``unicode61 remove_diacritics 2`` treats punctuation as word
    boundaries and removes combining marks.  Tier classification must mirror
    those semantics on both the stored name and the bounded query; otherwise a
    successful MATCH can be mislabeled as ``other_field``.
    """

    decomposed = unicodedata.normalize("NFD", normalize_search_text(value))
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character)[0] != "M"
    )
    return " ".join(_word_tokens(without_marks))


def _fts_query(value: str) -> tuple[str, dict]:
    """Build a bounded FTS5 MATCH expression, and report what the bound removed.

    Tokenisation keeps only word characters, so no FTS5 metacharacter can
    reach the expression.  Bounding happens here rather than at the HTTP
    boundary because the cost lives in prefix-term breadth, not in the
    length of the submitted string.

    The token bound used to be applied silently.  For a place picker that is a
    correctness problem, not merely a UX one: a query whose distinguishing word
    sits past the limit is answered as though that word had never been typed,
    and the user may accept a coordinate for somewhere else entirely.
    The bound is therefore kept - it is a real cost
    control - but the caller now receives the dropped tokens so the response
    can say so.
    """

    tokens = _word_tokens(value)
    bounded = tokens[:MAX_SEARCH_TOKENS]
    dropped = tokens[MAX_SEARCH_TOKENS:]
    terms = []
    for token in bounded:
        # Short tokens stay exact.  A 1-character prefix term is the single
        # largest cost multiplier and is almost never the user's intent.
        # FTS5 removes diacritics before matching.  Measure the same effective
        # token rather than the submitted code-point count, or combining marks
        # can disguise a one-character prefix scan.
        effective_token = place_name_match_key(token)
        if len(effective_token) >= MIN_PREFIX_TOKEN_LENGTH:
            terms.append(f'"{token}"*')
        else:
            terms.append(f'"{token}"')
    receipt = {
        "token_count": len(tokens),
        "max_search_tokens": MAX_SEARCH_TOKENS,
        "truncated": bool(dropped),
        "tokens_used": bounded,
        "tokens_ignored": dropped,
        "reason": (
            "token_limit_protects_against_broad_prefix_term_cost"
            if dropped
            else None
        ),
    }
    return " ".join(terms), receipt


class PlaceCatalog:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else DEFAULT_CATALOG_PATH

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
                timeout=1.0,
            )
        except sqlite3.Error as exc:
            raise PlaceCatalogUnavailableError(
                "bundled place catalog is unavailable"
            ) from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.create_function(
            "place_name_match_key",
            1,
            place_name_match_key,
            deterministic=True,
        )
        return connection

    def metadata(self, connection: sqlite3.Connection) -> dict:
        try:
            rows = connection.execute(
                "SELECT key, value_json FROM catalog_metadata"
            ).fetchall()
        except sqlite3.Error as exc:
            raise PlaceCatalogUnavailableError(
                "bundled place catalog metadata is unavailable"
            ) from exc
        return {
            row["key"]: json.loads(row["value_json"])
            for row in rows
        }

    def search(
        self,
        *,
        query: str,
        country_code: str | None,
        limit: int,
    ) -> dict:
        match_query, query_receipt = _fts_query(query)
        if not match_query:
            return {
                "results": [],
                "catalog": {},
                "query": query_receipt,
                "execution": {
                    "catalog_mode": "bundled_read_only_sqlite",
                    "runtime_outbound": False,
                },
            }

        # Tier classification must describe the same sanitized, bounded token
        # sequence that reached FTS5.  Comparing the raw normalized string
        # instead made ``London!!!`` execute as ``london`` yet report
        # ``other_field``; a truncated query had the same receipt/SQL drift.
        normalized_query = place_name_match_key(
            " ".join(query_receipt["tokens_used"])
        )
        connection = self._connect()
        deadline = time.monotonic() + SEARCH_STATEMENT_BUDGET_SECONDS
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0,
            _PROGRESS_HANDLER_INSTRUCTION_INTERVAL,
        )
        try:
            metadata = self.metadata(connection)
            # The match key was computed in the projection twice and in the
            # ordering twice more: four Python calls for every FTS hit, and a
            # common prefix matches tens of thousands of rows.  Under the GIL
            # that made concurrent searches exceed the statement budget and
            # refuse.  The key is computed once per row here; the ranking steps
            # and their declared descriptions are unchanged.
            rows = connection.execute(
                f"""
                WITH matched AS MATERIALIZED (
                    SELECT
                        p.source,
                        p.source_record_id,
                        p.name,
                        p.display_name,
                        p.country_code,
                        p.admin1,
                        p.admin2,
                        p.latitude,
                        p.longitude,
                        p.timezone,
                        p.location_precision,
                        p.population,
                        p.source_priority,
                        place_name_match_key(p.name) AS name_key,
                        bm25(place_search, {_BM25_SQL}) AS bm25_rank
                    FROM place_search
                    JOIN places AS p ON p.rowid = place_search.rowid
                    WHERE place_search MATCH :match
                      AND (:country IS NULL OR p.country_code = :country)
                )
                SELECT
                    *,
                    CASE
                        WHEN name_key = :query THEN 'exact_name'
                        WHEN substr(name_key, 1, :query_length)
                             = :query THEN 'name_prefix'
                        ELSE 'other_field'
                    END AS match_tier
                FROM matched
                ORDER BY
                    {_ORDER_BY_SQL}
                LIMIT :limit
                """,
                {
                    "match": match_query,
                    "country": country_code,
                    "query": normalized_query,
                    # 以 substr 而非 LIKE 做前綴比對：查詢字串是使用者輸入，
                    # LIKE 會把其中的 % 與 _ 當成萬用字元，需要額外的跳脫處理；
                    # substr 完全沒有萬用字元語意，不存在該類問題。
                    "query_length": len(normalized_query),
                    "limit": limit,
                },
            ).fetchall()
        except sqlite3.Error as exc:
            raise PlaceCatalogUnavailableError(
                "bundled place catalog query failed"
            ) from exc
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()

        return {
            "results": [
                {
                    "source": row["source"],
                    "source_record_id": row["source_record_id"],
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "country_code": row["country_code"],
                    "admin1": row["admin1"],
                    "admin2": row["admin2"],
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                    "timezone": row["timezone"],
                    "location_precision": row["location_precision"],
                    "population": row["population"],
                    # 排序層級一併回傳，讓呼叫端能看出某一筆是「地名
                    # 本身相符」還是「只有別名或行政區欄位相符」，不必反推。
                    "match_tier": row["match_tier"],
                    "coordinate_semantics": (
                        "dataset_representative_point_not_birth_address"
                    ),
                }
                for row in rows
            ],
            "catalog": metadata,
            "query": query_receipt,
            "ranking": {
                "method": RANKING_METHOD_NAME,
                # 這份清單必須與 ORDER BY 逐項對應。把三層的 match_tier 寫成
                # 單一個第一順位是錯的：實際 SQL 是先只分「是否 exact_name」，
                # 人口排在其後，完整的三層細分再排在人口**之後**。
                # 排序結果本身是確定的，可能出錯的是這份
                # 自述——而自述錯誤等於可追溯性宣稱不實。
                "order": [description for _sql, description in RANKING_STEPS],
                "why_population_precedes_the_detailed_tier": (
                    "中文查詢命中的是別名欄而非地名欄，故臺北市落在 other_field；"
                    "若三層細分排在人口之前，人口 0 的『臺北小別墅』（name_prefix）"
                    "會壓過人口 787 萬的臺北市。"
                ),
                "population_semantics": (
                    "geonames_settlement_population_zero_for_taiwan_moi_place_names"
                ),
            },
            "execution": {
                "catalog_mode": "bundled_read_only_sqlite",
                "runtime_outbound": False,
            },
        }
