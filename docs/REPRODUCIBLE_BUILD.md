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
