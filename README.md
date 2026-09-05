# Classical Astrology Data Service

Corresponding Source for the invited Private Alpha；not professional medical、legal、financial or
psychological advice. Canonical public repository：<https://github.com/sa3952/Project-Armillary>.
The exact backend and frontend revisions only become deployment facts when anonymously reachable and
bound by the combined release receipt.

## License

AGPL-3.0-only. See`LICENSE`and`THIRD_PARTY_NOTICES.md`forSwiss Ephemeris／pyswisseph and data sources.

## Build

Requirements：Docker withBuildKit／Compose and alinux/amd64builder. Use the sole build owner；do not add
aCompose`build:`path or build the raw checkout.

```bash
build_root="$(mktemp -d /tmp/armillary-build.XXXXXX)"
python -m scripts.verification.build_release_image \
  --image classical-astrology-data:local --platform linux/amd64 \
  --purpose diagnostic --evidence-dir "$build_root/evidence" --require-clean
```

`diagnostic`is provisional. `release-candidate`also requires a verified publication receipt. The
runtime image excludes tests、docs、Git、source archives and frontend assets.

Build the separately mounted frontend from an exact public revision：

```bash
python -m scripts.deployment.frontend_release build \
  --source-root "$PWD" --output-parent /SAFE/OUTSIDE/SOURCE/frontend-releases \
  --public-source-revision "$(git rev-parse HEAD)"
```

`deploy/compose.yaml`is runtime-only. A hosted composition supplies receipt-derived frontend digest、
backend image ID and combined release ID；the public tree does not include the private host overlay or
publish a browser-reachable port.

## Tests

```bash
python -m pytest tests
node --test frontend/tests/*.test.cjs
python -m scripts.verification.verify_privacy_dependencies --check
python -m scripts.verification.verify_ephemeris_integrity --check
```

Docker／scanner acceptance remains tool- and artifact-specific.

## Source correspondence

An eligible release binds exact public Git and source archive、third-party source archive、SBOM、image
and build receipt、frontend manifest／digest and combined identity. Backend and frontend public revisions
may differ after a frontend-only release；both remain anonymously reachable. Private operations、history、
credentials、logs and user data are outside Corresponding Source.

Read`SECURITY.md`、`docs/PRIVACY_ARCHITECTURE.md`、`docs/PRIVACY_THREAT_MODEL.md`and
`docs/KNOWN_LIMITATIONS.md`. Source availability permits review；it does not prove security.
