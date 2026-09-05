# Offline place catalog

`places.sqlite3`is an immutable generated runtime catalog：GeoNames`cities500`provides the global
base and Taiwan Ministry of the Interior data provides a higher-priority overlay. Deterministic
ranking prefers exact normalized names、major populated cities、Taiwan source priority and population.
Both`臺／台`forms are indexed. For every top-level Taiwan county/city, the
producer derives one picker-only administrative representative point from the
coordinate-bearing MOI child records. It is explicitly not a city hall,
geometric centroid, address, or asserted birth location.

The application opens SQLite with`mode=ro&immutable=1`. `POST /api/places/search`is same-origin and
makes no outbound request；the catalog stores no query、birth data、account or runtime cache. Results
retain source record、representative coordinates、timezone and precision so users can correct them；
representative points are not verified addresses.

Rebuild only from pinned snapshots：

```bash
python -m scripts.validation.build_place_catalog \
  --geonames-zip /path/to/cities500.zip \
  --taiwan-admin-csv /path/to/taiwan-admin.csv \
  --taiwan-settlement-csv /path/to/taiwan-settlement.csv \
  --output backend/place_data/places.sqlite3 \
  --manifest backend/place_data/catalog_manifest.json \
  --source-date YYYY-MM-DD
```

Catalog、manifest、hashes、row counts、notices、Docker context and publication candidate change as one
reviewed release unit. Source identity and licenses come from the manifest and distributed notices.
