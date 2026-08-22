"""Refuse persistent diagnostic/release output inside a maintained source tree."""
from __future__ import annotations

from pathlib import Path


def external_output_path(path: Path, *, source_root: Path, role: str) -> Path:
    output = path.resolve()
    root = source_root.resolve()
    if output == root or root in output.parents:
        raise ValueError(f"{role} must be outside the source repository")
    return output
