# Third-party notices

## Swiss Ephemeris and pyswisseph

This application uses`pyswisseph 2.10.3.2`, incorporating Swiss Ephemeris：Copyright © 1997–2021
Astrodienst AG, Switzerland；authors Dieter Koch and Alois Treindl. Swiss Ephemeris is dual-licensed；
this project uses theGNU AGPLroute. Exact source and original notices are under
`third_party/pyswisseph/`in the Corresponding Source tree. Attribution does not imply endorsement.

The extension also compiles the bundled **Swephelp** helper sources. Swephelp states that it is not part of
Swiss Ephemeris itself and identifies Stanislas Marquis as its author. Its original README and GPL-2.0 license
are inside `third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz`; the publication exporter also preserves them
beside that exact sdist as `SWEPHELP_README.txt` and `SWEPHELP_LICENSE`.

## Place data

- GeoNames`cities500`：<https://download.geonames.org/export/dump/cities500.zip>，
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，attribution toGeoNames.
- Taiwan Ministry of the Interior administrative and settlement names：
  <https://data.gov.tw/dataset/40281>、<https://data.gov.tw/dataset/53677>，
  [Taiwan Government Data Open License 1.0](https://data.gov.tw/license)，attribution to the
  Department of Land Administration.

Coordinates are representative points, not verified addresses；neither provider endorses this service.

## Source location

Named paths are relative to theCorresponding Sourcetree, not the runtime image. For an eligible hosted
release, use the exact 40-character revision reported by the service at：

```text
https://github.com/sa3952/Project-Armillary/tree/<public_source_revision>
```

Release engineering verifies anonymous reachability. Do not infer a deployed revision from the latest
branch. The runtime image contains notices and license text but not locks、manifests or source archives.

## Debian base and other dependencies

The image uses the digest-pinned Debian-based Python image named in`deploy/Dockerfile`. It is not
currently conveyed through a registry or to a third party；this notice is not a Debian source offer. If
that changes, Debian source-fulfilment must be reassessed before conveyance. Exact Python production／
build locks and **third_party/SOURCE_MANIFEST.json** record versions、hashes、licenses and upstream source；
a release fails when this inventory or source bundle is incomplete.
