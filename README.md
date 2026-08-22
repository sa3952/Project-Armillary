# Classical Astrology Data Service

This file is maintained at `publication/public_overlay/README.md` in the private engineering
source. Release engineering exports it with `scripts/publication/export_public_source.py` under the exact
`publication/publication_manifest.json` allowlist. Update the maintained overlay source and
rebuild a clean candidate; do not edit generated candidates. The private lifecycle contract is
not part of this public tree, but the release gate requires its shared-claim mappings to pass.

This tree is the maintained publication candidate for the Corresponding Source
of the Classical Astrology Data Service. It becomes the source for a hosted
release only when an exact public revision is bound to the deployed image and
is anonymously reachable. The intended service is an invited Private Alpha and is not
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
- a Docker builder capable of producing the release target `linux/amd64`;
- no production credential is required for a local build.

Use the canonical build transaction owner. It materializes the governed closed
context outside the source tree, creates and consumes a non-empty BuildKit
secret, observes the context BuildKit actually received, captures the builder
toolchain, and extracts the evidence from the exact runtime image:

```bash
build_root="$(mktemp -d /tmp/classical-astrology-build.XXXXXX)"
python -m scripts.verification.build_release_image \
  --image classical-astrology-data:local \
  --platform linux/amd64 \
  --purpose diagnostic \
  --evidence-dir "$build_root/evidence" \
  --require-clean
```

`diagnostic` builds are labelled `provisional_unpublished`. A release-candidate
build instead requires `--purpose release-candidate` and a verified
`--publication-receipt`; the build owner rejects an operator-supplied claim that
is not backed by that receipt.

`deploy/compose.yaml` is intentionally runtime-only and contains no `build:` key. Do not replace
the image reference with a Compose `build:` section or point Compose at the raw checkout; doing so
would create an ungoverned second build path.

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
  -f deploy/compose.frontend-release.yaml \
  config
```

This validates the exported composition only. The public source set deliberately
does not ship the private operator staging overlay, and the base composition
publishes no host port, so this command does not create a browser-reachable
`127.0.0.1` service. A real hosted run requires the separately governed private
operator overlay, an authenticated HTTPS reverse proxy, and the host controls
described in `docs/DEPLOYMENT_SECURITY.md`.

## Tests

The source tree preserves the focused hosted, calculation, privacy dependency,
frontend, runtime, and container tests used for this release. Run:

```bash
python -m pytest tests
frontend_tests=(frontend/tests/*.test.cjs)
test -e "${frontend_tests[0]}" || { echo "frontend test universe is empty" >&2; exit 1; }
node --test "${frontend_tests[@]}"
python -m scripts.verification.verify_privacy_dependencies --check
python -m scripts.verification.verify_ephemeris_integrity --check
```

Container and supply-chain verification require Docker and the scanner tools
documented by their command help.

## Source correspondence

Before a hosted release is eligible, release engineering must publish and bind:

- an exact Git revision;
- a source archive;
- a third-party source archive and manifest;
- an SBOM and dependency inventory;
- the deployed container image digest and build receipt;
- the frontend artifact digest, its file-hash manifest, and its exact public
  source revision;
- a combined runtime receipt binding the backend image and frontend release.

For an eligible release, the public source revision must be the source used to
build the deployed image. This candidate text is not evidence that publication
or deployment has already occurred.
Development conversations, private operational records, credentials, logs,
user data, and unrelated local-app packaging are not part of this hosted
Corresponding Source.

## Security and privacy

Read `SECURITY.md`, `docs/PRIVACY_ARCHITECTURE.md`,
`docs/PRIVACY_THREAT_MODEL.md`, and `docs/KNOWN_LIMITATIONS.md` before relying
on the service's privacy properties. Source availability enables review; it is
not by itself proof that the software is secure.
