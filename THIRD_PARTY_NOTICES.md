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

## Other dependencies

Exact production and build dependencies are pinned with hashes in:

- `deploy/requirements.lock`
- `deploy/build-requirements.lock`

`third_party/SOURCE_MANIFEST.json` records source archives, hashes, licenses,
and upstream locations. A release must not be published if this inventory or
its accompanying source bundle is incomplete.
