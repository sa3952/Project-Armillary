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

Materialize the governed closed context outside the source tree, create a
non-empty BuildKit probe, then build only from that context:

```bash
build_root="$(mktemp -d /tmp/classical-astrology-build.XXXXXX)"
printf 'local-build-probe\n' > "$build_root/probe.txt"
python -m scripts.verification.verify_docker_context \
  --materialize "$build_root/context"
DOCKER_BUILDKIT=1 docker build \
  --secret id=private_alpha_probe,src="$build_root/probe.txt" \
  --build-arg VCS_REF="$(git rev-parse HEAD)" \
  --tag classical-astrology-data:local \
  --file "$build_root/context/deploy/Dockerfile" \
  "$build_root/context"
```

The production image intentionally contains application runtime files, not
tests, documentation, Git metadata, third-party source archives, or frontend
assets. Build the immutable frontend release separately from this same clean
exact revision:

```bash
public_revision="$(git rev-parse HEAD)"
python -m scripts.deployment.frontend_release build \
  --source-root "$PWD" \
  --output-parent /SAFE/OUTSIDE/SOURCE/frontend-releases \
  --public-source-revision "$public_revision"
```

The command prints the artifact digest and release directory. Do not edit that
directory after publication.

For a local hosted-profile check, use the base runtime configuration, hosted
overlay, and the external frontend overlay together. Set each value from the
verified image/release receipts; `COMBINED_RELEASE_ID` is produced by the
deployment tooling and is not an arbitrary label:

```bash
FRONTEND_RELEASE_DIR=/absolute/path/to/frontend-releases/ARTIFACT_DIGEST \
FRONTEND_RELEASE_DIGEST=ARTIFACT_DIGEST \
BACKEND_IMAGE_ID=sha256:FULL_IMAGE_ID \
COMBINED_RELEASE_ID=FULL_COMBINED_RELEASE_ID \
docker compose \
  -f deploy/compose.yaml \
  -f deploy/staging/compose.staging.yaml \
  -f deploy/compose.frontend-release.yaml \
  up --force-recreate
```

Open `http://127.0.0.1:8124/`. Production deployment requires an authenticated
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
- the deployed container image digest and build receipt;
- the frontend artifact digest, its file-hash manifest, and its exact public
  source revision;
- a combined runtime receipt binding the backend image and frontend release.

The public source revision is the source used to build the deployed image.
Development conversations, private operational records, credentials, logs,
user data, and unrelated local-app packaging are not part of this hosted
Corresponding Source.

## Security and privacy

Read `SECURITY.md`, `docs/PRIVACY_ARCHITECTURE.md`,
`docs/PRIVACY_THREAT_MODEL.md`, and `docs/KNOWN_LIMITATIONS.md` before relying
on the service's privacy properties. Source availability enables review; it is
not by itself proof that the software is secure.
