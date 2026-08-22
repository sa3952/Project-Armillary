# Third-Party Notices

## Swiss Ephemeris and pyswisseph

This application uses `pyswisseph 2.10.3.2`, a Python extension incorporating
Swiss Ephemeris.

Swiss Ephemeris:

- Copyright (C) 1997-2021 Astrodienst AG, Switzerland.
- Authors: Dieter Koch and Alois Treindl.
- Distributed under a dual-license model. This project uses the GNU Affero
  General Public License route.
- The original copyright and license notice is preserved inside the exact
  `pyswisseph` source distribution under `third_party/pyswisseph/`.

The authors and copyright holder have no control over this derived service.
Their names are included only as part of the required copyright notice and
must not be interpreted as endorsement.

## GeoNames cities500

The bundled global place-search catalog contains a snapshot derived from
GeoNames `cities500`.

- Source: https://download.geonames.org/export/dump/cities500.zip
- License: Creative Commons Attribution 4.0 International (CC BY 4.0),
  https://creativecommons.org/licenses/by/4.0/
- Attribution: GeoNames, https://www.geonames.org/

GeoNames provides the data as-is without a warranty of accuracy, timeliness,
or completeness. Coordinates exposed by this application are representative
place points and are not asserted to be a birth address.

## Taiwan Ministry of the Interior place names

The bundled Taiwan overlay contains administrative-area and settlement place
names published by the Taiwan Ministry of the Interior.

- Administrative-area dataset: https://data.gov.tw/dataset/40281
- Settlement dataset: https://data.gov.tw/dataset/53677
- License: Taiwan Government Data Open License 1.0,
  https://data.gov.tw/license
- Attribution: Department of Land Administration, Ministry of the Interior,
  Taiwan.

The overlay remains a separately attributed dataset. It does not imply that
the Ministry of the Interior endorses this application.

## Where the paths in this notice point

Every repository path named in this document — including
`third_party/pyswisseph/` above — is relative to the **Corresponding Source
tree** of this service, not to the running container image. The image ships
this notice and the licence text, but it does not carry the lockfiles, the
source manifest, or the vendored upstream archives; those live in the source
distribution.

## Obtaining the Corresponding Source

The designated publication location for an eligible hosted release is:

    https://github.com/sa3952/Project-Armillary

Release engineering must verify that the exact revision is readable there
anonymously before treating a deployment as eligible. This maintained candidate
does not itself prove that the repository or the matching revision is public.

**Do not guess which revision a given deployment was built from.** The service
reports it. The release identity returned by the running service, and the
`public_source_revision` field of the frontend release manifest, each name a
40-character commit. Read that exact tree at:

    https://github.com/sa3952/Project-Armillary/tree/<public_source_revision>

The revision matters because this notice ships inside an image that does not
carry the lockfiles, the source manifest, or the vendored upstream archives.
Those live only in the source tree above, and they differ between revisions.

This section previously said the public location was pending, and deliberately
named no URL rather than name one that did not resolve. The location above was
published and confirmed anonymously reachable before this paragraph replaced
it.

## The Debian base of the runtime image

The hosted service runs in a container built on `python:3.13-slim-trixie`,
pinned by digest in `deploy/Dockerfile`. That base carries Debian packages,
many of them licensed under the GPL or LGPL.

This section is disclosure, not a source offer, and the distinction is
deliberate. The GPL's source obligation is triggered by conveying a binary to
someone. This image is conveyed to no one: it is not published to any
registry, and people who use the service receive HTTP responses, not the
image. What network use does trigger is AGPL section 13, which covers this
application and pyswisseph — and that obligation is discharged by the
repository named above.

Should that ever change — if the image were pushed to a registry, handed to
anyone, or run by a third party — a source route for the Debian components
would be required, and this notice would have to say so. Until then, the
upstream sources for those packages are the ones Debian itself publishes, for
the exact versions recorded in `deploy/sbom.cyclonedx.json`.

## Other dependencies

Exact production and build dependencies are pinned with hashes in:

- `deploy/requirements.lock`
- `deploy/build-requirements.lock`

`third_party/SOURCE_MANIFEST.json` records source archives, hashes, licenses,
and upstream locations. A release must not be published if this inventory or
its accompanying source bundle is incomplete.
