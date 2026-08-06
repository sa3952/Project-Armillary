# Offline place catalog

`places.sqlite3` is a generated, immutable runtime catalog. It contains:

- GeoNames `cities500` as the global base;
- Taiwan Ministry of the Interior administrative-area and settlement place
  names as a higher-priority Taiwan overlay.

Ranking is deterministic: an exact normalized place name comes first, then a
major populated city, then the remaining full-text matches with Taiwan source
priority and population as tie-breakers. The index includes both common
`臺`/`台` spellings. This keeps fine-grained Taiwan matches available without
letting incidental county-name matches hide the main city.

The application opens this file with SQLite `mode=ro&immutable=1`. It stores no
requests, searches, birth data, accounts, or runtime cache. The search endpoint
is same-origin `POST /api/places/search`; it performs no runtime outbound
request.

Coordinates are dataset representative points, not verified birth addresses.
Results retain `source`, `source_record_id`, `location_precision`, coordinates,
and timezone so the user can inspect or correct the resolution.

Rebuild from pinned source snapshots:

```bash
python -m scripts.validation.build_place_catalog \
  --geonames-zip /path/to/cities500.zip \
  --taiwan-admin-csv /path/to/taiwan-admin.csv \
  --taiwan-settlement-csv /path/to/taiwan-settlement.csv \
  --output backend/place_data/places.sqlite3 \
  --manifest backend/place_data/catalog_manifest.json \
  --source-date 2026-07-29
```

See `catalog_manifest.json` and the distributed `THIRD_PARTY_NOTICES.md` for
source hashes, attribution, licensing, and limitations.

Dataset updates are low-frequency, intentional releases. The catalog, manifest,
source hashes, row counts, license notices, Docker context, publication candidate,
and CI verification must change as one reviewed unit; Git LFS and release-artifact
indirection are not the current policy.
