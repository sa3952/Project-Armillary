# Build Correspondence and Reproducibility

The immediate release requirement is a repeatable, source-corresponding
container build:

- exact public Git revision;
- pinned Python 3.13.14 base image digest;
- hashed Python build and runtime dependencies;
- source-built `pyswisseph 2.10.3.2`;
- ephemeris file hashes;
- recorded architecture and ELF machine type;
- container inventory, SBOM, scanner evidence, and image digest.

This project does not yet claim bit-for-bit reproducible container images
across independent builders. Timestamps, package repository state, and build
platform differences remain possible sources of byte-level variance.

## Supported dependency reconstruction modes

The publication candidate supports two fresh-environment modes. Neither mode
copies an existing virtual environment.

Both modes are defined for `linux/amd64` and use this digest-pinned builder,
which contains the C compiler required to build `pyswisseph` from its retained
source distribution:

```text
python:3.13.14-trixie@sha256:153e964bee18ef816ff55c8b026a345c62d4ccf05ad119ce5d7c10dee79574d7
```

- `online-clean`: a new CPython 3.13 virtual environment installs the exact
  hash-pinned development lock from an HTTPS package index.
- `offline candidate-only`: the consumer has no network. It installs PEP 517
  bootstrap and dependency wheels only when their hashes occur in the same
  committed locks, while building `pyswisseph` from the retained sdist.

All exact production, build, and development sdists remain in
`third_party/sources/` for inspection. Some upstream Rust-backed sdists are
not self-contained and attempt to fetch crates or Git dependencies during a
literal all-sdist build; the verified wheel index is the supported offline
path for those packages. `third_party/SOURCE_MANIFEST.json` records the exact
roles, archive hashes, index target, and which package is source-built.

The generated `docs/DEPENDENCY_LICENSES.md` is a human view of that manifest.
Manual edits are rejected by candidate verification.

From the candidate root, replay either supported mode without copying an
existing virtual environment:

```bash
bash scripts/publication/verify_candidate_clean.sh \
  --root . \
  --reconstruction-mode offline-source-only

bash scripts/publication/verify_candidate_clean.sh \
  --root . \
  --reconstruction-mode online-clean
```

The verifier passes `--platform linux/amd64` to Docker explicitly. A different
`--builder-image` is an investigative override, not evidence for the documented
reconstruction contract.

The runtime Compose files do not build images. The only candidate build context is the closed
materialized output of `scripts.verification.verify_docker_context`; a raw-checkout Compose build
is outside the supported and governed path.
