"""Read-only bundled place catalog; no runtime geocoder or third-party request."""

from __future__ import annotations

import json
from pathlib import Path
import re
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


def _fts_query(value: str) -> str:
    """Build a bounded FTS5 MATCH expression.

    Tokenisation keeps only word characters, so no FTS5 metacharacter can
    reach the expression.  Bounding happens here rather than at the HTTP
    boundary because the cost lives in prefix-term breadth, not in the
    length of the submitted string.
    """

    tokens = re.findall(r"[\w]+", normalize_search_text(value))
    bounded = tokens[:MAX_SEARCH_TOKENS]
    terms = []
    for token in bounded:
        # Short tokens stay exact.  A 1-character prefix term is the single
        # largest cost multiplier and is almost never the user's intent.
        if len(token) >= MIN_PREFIX_TOKEN_LENGTH:
            terms.append(f'"{token}"*')
        else:
            terms.append(f'"{token}"')
    return " ".join(terms)


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
        match_query = _fts_query(query)
        if not match_query:
            return {
                "results": [],
                "catalog": {},
                "execution": {
                    "catalog_mode": "bundled_read_only_sqlite",
                    "runtime_outbound": False,
                },
            }

        connection = self._connect()
        deadline = time.monotonic() + SEARCH_STATEMENT_BUDGET_SECONDS
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0,
            _PROGRESS_HANDLER_INSTRUCTION_INTERVAL,
        )
        try:
            metadata = self.metadata(connection)
            rows = connection.execute(
                """
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
                    p.population
                FROM place_search
                JOIN places AS p ON p.rowid = place_search.rowid
                WHERE place_search MATCH ?
                  AND (? IS NULL OR p.country_code = ?)
                ORDER BY
                    CASE
                        WHEN p.normalized_name = ? THEN 0
                        WHEN p.population >= 50000 THEN 1
                        ELSE 2
                    END ASC,
                    bm25(place_search) ASC,
                    p.source_priority ASC,
                    p.population DESC,
                    p.display_name ASC
                LIMIT ?
                """,
                (
                    match_query,
                    country_code,
                    country_code,
                    normalize_search_text(query),
                    limit,
                ),
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
                    "coordinate_semantics": (
                        "dataset_representative_point_not_birth_address"
                    ),
                }
                for row in rows
            ],
            "catalog": metadata,
            "execution": {
                "catalog_mode": "bundled_read_only_sqlite",
                "runtime_outbound": False,
            },
        }
