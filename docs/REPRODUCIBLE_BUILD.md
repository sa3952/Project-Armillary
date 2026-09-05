# Build correspondence and reproducibility

The release contract is source-corresponding, not bit-for-bit reproducible across independent builders.
It binds exact public revision、digest-pinnedlinux/amd64Python base、hashed dependencies、source-built
pyswisseph、ephemeris hashes、ELF architecture、inventory、SBOM、scanner evidence and image digest.
Timestamps、package repositories、toolchains and platforms may change whole-file bytes.

## Reconstruction

Both supported modes create a fresh environment and use the digest-pinned builder recorded in
`deploy/Dockerfile`：

- `online-clean`installs the exact hashed development lock fromHTTPS.
- `offline-source-only`uses only candidate sdists and lock-authorized wheel indexes；pyswisseph is still
  built from retained source. Some upstream Rust sdists are not self-contained, so an all-sdist build is
  not claimed.

**third_party/SOURCE_MANIFEST.json** binds roles、archive／wheel hashes and source-built status；generated
**docs/DEPENDENCY_LICENSES.md** must match it.

```bash
bash scripts/publication/verify_candidate_clean.sh --root . \
  --reconstruction-mode offline-source-only
bash scripts/publication/verify_candidate_clean.sh --root . \
  --reconstruction-mode online-clean
```

The verifier fixes`linux/amd64`; another builder is diagnostic only. RuntimeCompose does not build. The
sole candidate context is the closed materialized output of
`scripts.verification.verify_docker_context`；a raw-checkout／Compose build is unsupported.

## What this tree does not carry, and why

Two kinds of thing are deliberately absent, so that their absence reads as a decision rather than an
omission.

**Operator state.** The edge configuration and the site template are here; the renderer that fills in
a domain, the TLS preparation, the credential tooling and the host hardening for one particular
machine are not. Those configure sshd, ufw, sysctl, journald and certbot — generally available
programs used unmodified — and what they hold is one operator's identity, not knowledge needed to
build or modify this work. Bring your own.

**Our own quality apparatus.** The tests published here are the ones that verify a claim this
project makes in public: privacy logging, the runtime contract, resource bounds, the release
transaction, the hosted profile, and the browser-side privacy lifecycle and export serializers.
Ordinary functional and coverage tests, their fixtures, and lint configuration are not published.
They are not needed to generate, install, run or modify the work, and publishing them would say
nothing a reader could check.
