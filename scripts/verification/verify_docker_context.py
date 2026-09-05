#!/usr/bin/env python3
"""Verify the closed production Docker build-context inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAXIMUM_SIZE_BYTES = 200 * 1024 * 1024
# Names excluded from the runtime trees even though the tree is allowed.
RUNTIME_EXCLUDED_DOCUMENT_NAMES = frozenset({"readme.md"})

ALLOWED_TREES = (
    Path("backend/app"),
    Path("backend/ephe"),
    Path("backend/place_data"),
)
ALLOWED_FILES = (
    Path(".dockerignore"),
    Path("LICENSE"),
    Path("deploy/Dockerfile"),
    Path("deploy/requirements.lock"),
    Path("deploy/build-requirements.lock"),
    Path("deploy/ephemeris.sha256"),
    Path("deploy/frontend-contract.json"),
    Path("deploy/entrypoint.sh"),
    Path("deploy/container_healthcheck.py"),
    Path("third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz"),
    Path("scripts/verification/verify_ephemeris_integrity.py"),
    Path("scripts/verification/verify_place_catalog.py"),
    Path("scripts/verification/capture_build_evidence.py"),
    Path("scripts/publication/verify_linux_source_build.py"),
)
ALTERNATIVE_ALLOWED_FILES = (
    (
        Path("publication/public_overlay/THIRD_PARTY_NOTICES.md"),
        Path("THIRD_PARTY_NOTICES.md"),
    ),
)
OPTIONAL_CONTROL_FILES = (Path("build-context-probe-8f38f069.txt"),)
# Local artifacts that the operating system or interpreter writes into a
# working tree, are gitignored, and can never reach a release context because
# that context is materialized from `git archive`.  Keep this a closed set of
# exact names/suffixes; it is not a general dot-path exemption.
IGNORABLE_LOCAL_ARTIFACT_NAMES = frozenset({".DS_Store"})
IGNORABLE_LOCAL_ARTIFACT_SUFFIXES = frozenset({".pyc", ".pyo"})
FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".build",
        "dist",
        ".venv",
        ".hypothesis",
        ".claude",
        ".codex",
        "__pycache__",
        ".pytest_cache",
        "tests",
    }
)
REQUIRED_DOCKERIGNORE_RULES = frozenset({
    "**",
    "!backend/app/**",
    "!backend/ephe/**",
    "!backend/place_data/**",
    "!deploy/Dockerfile",
    "!deploy/requirements.lock",
    "!third_party/pyswisseph/pyswisseph-2.10.3.2.tar.gz",
    "**/.env",
    "**/.env.*",
    "**/*.key",
    "**/*.pem",
    "**/*secret*",
    "**/*token*",
})


class DockerContextFailure(RuntimeError):
    pass


def verify_dockerignore_policy(text: str) -> None:
    rules = tuple(
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    missing = sorted(REQUIRED_DOCKERIGNORE_RULES - set(rules))
    unsafe_reincludes = sorted(
        rule for rule in rules
        if rule.startswith("!")
        and any(token in rule.casefold() for token in (".env", "secret", "token", ".key", ".pem"))
    )
    if not rules or rules[0] != "**" or missing or unsafe_reincludes:
        raise DockerContextFailure(
            ".dockerignore must deny by default, retain required build inputs, "
            f"and exclude secrets: missing={missing}, unsafe={unsafe_reincludes}"
        )


def context_file_paths(root: Path) -> list[Path]:
    paths: set[Path] = set()
    for relative in ALLOWED_FILES:
        target = root / relative
        if not target.is_file() or target.is_symlink():
            raise DockerContextFailure(
                f"required regular non-symlink input missing: {relative}"
            )
        paths.add(relative)
    for alternatives in ALTERNATIVE_ALLOWED_FILES:
        present = [
            relative
            for relative in alternatives
            if (root / relative).is_file()
            and not (root / relative).is_symlink()
        ]
        if len(present) != 1:
            raise DockerContextFailure(
                "exactly one role-specific notice is required: "
                + ", ".join(path.as_posix() for path in alternatives)
            )
        paths.add(present[0])
    for tree in ALLOWED_TREES:
        target = root / tree
        if not target.is_dir() or target.is_symlink():
            raise DockerContextFailure(
                f"required regular directory missing: {tree}"
            )
        for child in target.rglob("*"):
            relative = child.relative_to(root)
            if child.is_symlink():
                raise DockerContextFailure(
                    f"symlink forbidden in Docker context: {relative}"
                )
            # Local build and desktop artifacts are not context inputs, and
            # this skip has to precede the hidden-path guard below or it is
            # unreachable for the dot-prefixed names it exists to cover.  The
            # release context is materialized from `git archive`, so an
            # untracked file can never reach it, and `.dockerignore` excludes
            # these names from a raw `docker build .` as well: skipping them
            # keeps the closed inventory identical.  Every other dot path still
            # raises — this is a closed set of names, not a relaxation of the
            # hidden-path rule.
            if child.name in IGNORABLE_LOCAL_ARTIFACT_NAMES or (
                child.is_file() and child.suffix in IGNORABLE_LOCAL_ARTIFACT_SUFFIXES
            ):
                continue
            if any(part.startswith(".") for part in relative.parts):
                raise DockerContextFailure(
                    f"hidden path forbidden in Docker runtime tree: {relative}"
                )
            if child.is_file():
                if any(part in FORBIDDEN_PARTS for part in relative.parts):
                    continue
                # Ruled by Sebastian 2026-08-07: developer documentation is
                # not a runtime input and does not belong in the image.
                # Attribution required by the bundled datasets lives in
                # THIRD_PARTY_NOTICES.md, which ships separately at /app.
                if child.name.casefold() in RUNTIME_EXCLUDED_DOCUMENT_NAMES:
                    continue
                paths.add(relative)
    for relative in OPTIONAL_CONTROL_FILES:
        target = root / relative
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise DockerContextFailure(
                    f"invalid context control file: {relative}"
                )
            paths.add(relative)
    return sorted(paths, key=lambda item: item.as_posix())


def dockerfile_copy_source_strings(dockerfile: str) -> set[str]:
    """Return local COPY/ADD sources from Dockerfile logical lines."""
    logical_lines: list[str] = []
    continued = ""
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line or (line.startswith("#") and not continued):
            continue
        continued += (" " if continued else "") + line
        if continued.endswith("\\"):
            continued = continued[:-1].rstrip()
            continue
        logical_lines.append(continued)
        continued = ""
    if continued:
        raise DockerContextFailure("unterminated Dockerfile line continuation")

    sources: set[str] = set()
    for line in logical_lines:
        match = re.match(r"^(COPY|ADD)\s+(.+)$", line, re.IGNORECASE)
        if match is None:
            continue
        instruction, arguments = match.groups()
        options: list[str] = []
        while arguments.startswith("--"):
            option_match = re.match(r"^(--[^\s]+)\s+(.+)$", arguments)
            if option_match is None:
                raise DockerContextFailure(
                    f"invalid Dockerfile {instruction} options: {line}"
                )
            option, arguments = option_match.groups()
            options.append(option)
        if any(
            option == "--from" or option.startswith("--from=")
            for option in options
        ):
            continue
        try:
            if arguments.startswith("["):
                values = json.loads(arguments)
                if (
                    not isinstance(values, list)
                    or len(values) < 2
                    or not all(isinstance(value, str) for value in values)
                ):
                    raise ValueError("expected a JSON string array")
            else:
                values = shlex.split(arguments, posix=True)
                if len(values) < 2:
                    raise ValueError("expected at least one source and a destination")
        except (json.JSONDecodeError, ValueError) as error:
            raise DockerContextFailure(
                f"invalid Dockerfile JSON {instruction}: {line}"
            ) from error
        for source in values[:-1]:
            if instruction.upper() == "ADD" and re.match(
                r"^(?:https?|git)://", source
            ):
                continue
            sources.add(source)
    return sources


def _dockerfile_copy_sources(root: Path) -> set[Path]:
    dockerfile = (root / "deploy/Dockerfile").read_text(encoding="utf-8")
    return {Path(source) for source in dockerfile_copy_source_strings(dockerfile)}


def verify(root: Path = PROJECT_ROOT) -> dict:
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")
    verify_dockerignore_policy(dockerignore)

    paths = context_file_paths(root)
    path_set = set(paths)
    missing_copy_sources = sorted(
        source
        for source in _dockerfile_copy_sources(root)
        if source not in path_set
        and not any(path.is_relative_to(source) for path in path_set)
    )
    if missing_copy_sources:
        raise DockerContextFailure(
            "Dockerfile COPY source absent from closed context: "
            + ", ".join(path.as_posix() for path in missing_copy_sources)
        )

    forbidden = [
        path.as_posix()
        for path in paths
        if any(part in FORBIDDEN_PARTS for part in path.parts)
    ]
    total_size = sum((root / path).stat().st_size for path in paths)
    if forbidden:
        raise DockerContextFailure(
            f"forbidden paths entered Docker context: {forbidden}"
        )
    if total_size > MAXIMUM_SIZE_BYTES:
        raise DockerContextFailure(
            f"Docker context is {total_size} bytes; cap is {MAXIMUM_SIZE_BYTES}"
        )
    return {
        "schema_version": "docker-context-verification-v1",
        "policy": "closed_required_paths",
        "file_count": len(paths),
        "context_size_bytes": total_size,
        "maximum_size_bytes": MAXIMUM_SIZE_BYTES,
        "forbidden_paths_present": forbidden,
    }


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def materialize_context(
    source_root: Path,
    destination: Path,
    *,
    control_files: dict[Path, bytes] | None = None,
) -> dict:
    """Create the only supported Docker context from the governed allowlist."""
    source_root = source_root.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise DockerContextFailure(
            f"closed context destination already exists: {destination}"
        )
    if destination.is_relative_to(source_root):
        raise DockerContextFailure(
            "closed context destination must be outside the source tree"
        )

    source_receipt = verify(source_root)
    source_paths = context_file_paths(source_root)
    destination.mkdir(parents=True)
    for relative in source_paths:
        source = source_root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    for relative, content in (control_files or {}).items():
        if relative not in OPTIONAL_CONTROL_FILES:
            raise DockerContextFailure(
                f"undeclared context control file: {relative}"
            )
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    destination_receipt = verify(destination)
    expected_paths = context_file_paths(destination)
    source_identity = {
        relative.as_posix(): _file_identity(source_root / relative)
        for relative in source_paths
    }
    destination_identity = {
        relative.as_posix(): _file_identity(destination / relative)
        for relative in expected_paths
        if relative not in OPTIONAL_CONTROL_FILES
    }
    if destination_identity != source_identity:
        raise DockerContextFailure(
            "materialized Docker context is not byte-for-byte identical "
            "to the governed source inventory"
        )
    return {
        **destination_receipt,
        "producer": "materialize_context",
        "source_file_count": source_receipt["file_count"],
        "materialized_paths": [path.as_posix() for path in expected_paths],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--materialize", type=Path)
    args = parser.parse_args()
    try:
        if args.materialize is not None:
            receipt = materialize_context(PROJECT_ROOT, args.materialize)
        else:
            receipt = verify()
    except (DockerContextFailure, OSError) as exc:
        print(f"DOCKER CONTEXT FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "DOCKER CONTEXT "
        f"{'MATERIALIZED' if args.materialize is not None else 'VERIFIED'} "
        f"files={receipt['file_count']} "
        f"bytes={receipt['context_size_bytes']} "
        f"cap={receipt['maximum_size_bytes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
