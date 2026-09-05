"""Exact maintained-source identity shared by delivery and mutation gates."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile


@dataclass(frozen=True)
class SourceTreeIdentity:
    paths: tuple[str, ...]
    content_sha256: str
    index_sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def observe_publication_tree(root: Path) -> SourceTreeIdentity:
    manifest_path = root / "PUBLICATION_FILES.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read publication identity: {error}") from None
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("hash_algorithm") != "sha256"
        or not isinstance(entries, list)
        or not entries
    ):
        raise RuntimeError("invalid publication identity")
    expected: dict[str, tuple[int, str]] = {}
    for entry in entries:
        relative = Path(str(entry.get("path", ""))) if isinstance(entry, dict) else Path()
        size = entry.get("size_bytes") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        rendered = relative.as_posix()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or rendered in expected
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError("invalid publication file entry")
        expected[rendered] = (size, digest)
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if (
            path.is_file()
            and not path.is_symlink()
            and path != manifest_path
            and path.relative_to(root).parts[0] != ".git"
        )
    }
    if set(actual) != set(expected):
        raise RuntimeError("publication file set changed")
    content = hashlib.sha256()
    for relative_name in sorted(expected):
        target = actual[relative_name]
        size, digest = expected[relative_name]
        observed = hashlib.sha256(target.read_bytes()).hexdigest()
        if target.stat().st_size != size or observed != digest:
            raise RuntimeError(f"publication file changed: {relative_name}")
        content.update(relative_name.encode("utf-8"))
        content.update(b"\0")
        content.update(bytes.fromhex(observed))
    paths = tuple(sorted({*actual, manifest_path.name}))
    return SourceTreeIdentity(
        paths,
        content.hexdigest(),
        hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )


def observe_source_tree(root: Path) -> SourceTreeIdentity:
    if not (root / ".git").exists():
        return observe_publication_tree(root)
    indexed_paths = maintained_source_paths(root)
    paths = tuple(
        relative
        for relative in indexed_paths
        if (root / relative).exists() or (root / relative).is_symlink()
    )
    content = hashlib.sha256()
    for relative in paths:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError(f"unsafe maintained source path: {relative!r}")
        source = root / relative_path
        mode = source.lstat().st_mode
        content.update(relative.encode("utf-8"))
        content.update(b"\0")
        if stat.S_ISLNK(mode):
            content.update(b"symlink\0")
            content.update(os.readlink(source).encode("utf-8"))
        elif stat.S_ISREG(mode):
            content.update(b"executable\0" if mode & 0o111 else b"regular\0")
            content.update(source.read_bytes())
        else:
            raise RuntimeError(f"unsupported maintained source type: {relative!r}")
        content.update(b"\n")
    index = hashlib.sha256(_git(root, "ls-files", "-s", "-z")).hexdigest()
    return SourceTreeIdentity(paths, content.hexdigest(), index)


def materialize_git_snapshot(root: Path, revision: str, destination: Path) -> Path:
    destination.mkdir(parents=True)
    process = subprocess.Popen(
        ["git", "archive", "--format=tar", revision],
        cwd=root,
        stdout=subprocess.PIPE,
    )
    if process.stdout is None:
        raise RuntimeError("cannot read Git archive")
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            archive.extractall(destination, filter="data")
    finally:
        process.stdout.close()
    if process.wait() != 0:
        raise RuntimeError("cannot materialize exact Git snapshot")
    return destination
