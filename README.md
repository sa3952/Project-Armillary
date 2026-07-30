# Classical Astrology Data Service

This file is maintained at `publication/public_overlay/README.md` in the private engineering
source. Release engineering exports it with `scripts/publication/export_public_source.py` under the exact
`publication/publication_manifest.json` allowlist. Update the maintained overlay source and
rebuild a clean candidate; do not edit generated candidates. The private lifecycle contract is
not part of this public tree, but the release gate requires its shared-claim mappings to pass.

This repository contains the Corresponding Source for the hosted Classical
Astrology Data Service. The service is an invited Private Alpha and is not
professional medical, legal, financial, psychological, or other professional
advice.

Canonical public repository:
<https://github.com/sa3952/Project-Armillary>

## License

The hosted application is licensed under the GNU Affero General Public License
version 3 only (`AGPL-3.0-only`). It incorporates `pyswisseph` and Swiss
Ephemeris. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.

## Build and run

Requirements:

- Docker with BuildKit and Compose;
- a Linux `amd64` or `arm64` host for the validated container paths;
- no production credential is required for a local build.

Create a non-empty build probe outside the repository, then build:

```bash
mkdir -p /tmp/classical-astrology-build
printf 'local-build-probe\n' > /tmp/classical-astrology-build/probe.txt
DOCKER_BUILDKIT=1 docker build \
  --secret id=private_alpha_probe,src=/tmp/classical-astrology-build/probe.txt \
  --build-arg VCS_REF="$(git rev-parse HEAD)" \
  --tag classical-astrology-data:local \
  --file deploy/Dockerfile .
```

The production image intentionally contains application runtime files, not
tests, documentation, Git metadata, or third-party source archives.

For a local hosted-profile check:

```bash
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --publish 127.0.0.1:8000:8000 \
  classical-astrology-data:local
```

Open `http://127.0.0.1:8000/`. Production deployment requires an authenticated
HTTPS reverse proxy and additional host controls described in
`docs/DEPLOYMENT_SECURITY.md`.

## Tests

The source tree preserves the focused hosted, calculation, privacy dependency,
frontend, runtime, and container tests used for this release. Run:

```bash
python -m pytest tests
node --test frontend/tests/*.test.cjs
python -m scripts.verification.verify_privacy_dependencies --check
python -m scripts.verification.verify_ephemeris_integrity --check
```

Container and supply-chain verification require Docker and the scanner tools
documented by their command help.

## Source correspondence

Each release publishes:

- an exact Git revision;
- a source archive;
- a third-party source archive and manifest;
- an SBOM and dependency inventory;
- the deployed container image digest and build receipt.

The public source revision is the source used to build the deployed image.
Development conversations, private operational records, credentials, logs,
user data, and unrelated local-app packaging are not part of this hosted
Corresponding Source.

## Security and privacy

Read `SECURITY.md`, `docs/PRIVACY_ARCHITECTURE.md`,
`docs/PRIVACY_THREAT_MODEL.md`, and `docs/KNOWN_LIMITATIONS.md` before relying
on the service's privacy properties. Source availability enables review; it is
not by itself proof that the software is secure.
