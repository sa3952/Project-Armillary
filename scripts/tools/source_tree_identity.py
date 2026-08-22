"""Exact maintained-source identity shared by delivery and mutation gates."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class SourceTreeIdentity:
    paths: tuple[str, ...]
    content_sha256: str
    index_sha256: str


def _git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def maintained_source_paths(root: Path) -> tuple[str, ...]:
    output = _git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    paths = tuple(
        sorted(raw.decode("utf-8") for raw in output.split(b"\0") if raw)
    )
    if not paths:
        raise RuntimeError("maintained source universe is empty")
    return paths


def observe_source_tree(root: Path) -> SourceTreeIdentity:
    paths = maintained_source_paths(root)
    content = hashlib.sha256()
    for relative in paths:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"unsafe maintained source path: {relative!r}")
        source = root / path
        content.update(relative.encode("utf-8"))
        content.update(b"\0")
        if source.is_symlink():
            content.update(b"symlink\0")
            content.update(os.readlink(source).encode("utf-8"))
        else:
            content.update(source.read_bytes())
        content.update(b"\n")
    index = hashlib.sha256(_git(root, "ls-files", "-s", "-z")).hexdigest()
    return SourceTreeIdentity(paths, content.hexdigest(), index)
