#!/usr/bin/env python3
"""Aggregate privacy-safe NGINX 429/503 outcome lines, then truncate raw."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat


LINE = re.compile(
    r"^(?P<hour>\d{4}-\d{2}-\d{2}T\d{2}) "
    r"(?P<route>chart|places|static) (?P<status>429|503)$"
)
MAX_RAW_BYTES = 1024 * 1024


def _regular_nonsymlink(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("outcome path must be a regular non-symlink file")
    return metadata


def parse_lines(raw: bytes) -> dict[tuple[str, str, str], int]:
    if len(raw) > MAX_RAW_BYTES:
        raise ValueError("outcome log exceeds bounded input size")
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("outcome log is not ASCII") from error
    counts: dict[tuple[str, str, str], int] = {}
    for line in lines:
        match = LINE.fullmatch(line)
        if match is None:
            raise ValueError("outcome log contains a non-allowlisted line")
        key = (match["hour"], match["route"], match["status"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def _load_existing(path: Path) -> dict[tuple[str, str, str], int]:
    if not path.exists():
        return {}
    _regular_nonsymlink(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "private-alpha-nginx-outcomes-v1":
        raise ValueError("unsupported aggregate schema")
    result: dict[tuple[str, str, str], int] = {}
    for item in payload.get("counts", []):
        if (
            not isinstance(item, dict)
            or set(item) != {"hour", "route_class", "status", "count"}
            or LINE.fullmatch(
                f"{item.get('hour')} {item.get('route_class')} {item.get('status')}"
            ) is None
            or type(item.get("count")) is not int
            or item["count"] < 1
        ):
            raise ValueError("aggregate contains an invalid count")
        result[(item["hour"], item["route_class"], item["status"])] = item["count"]
    return result


def _payload(counts: dict[tuple[str, str, str], int], *, now: datetime) -> bytes:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=30)
    retained = {
        key: value
        for key, value in counts.items()
        if datetime.strptime(key[0], "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        >= cutoff
    }
    value = {
        "schema_version": "private-alpha-nginx-outcomes-v1",
        "retention_days": 30,
        "privacy_fields": ["hour", "route_class", "status", "count"],
        "counts": [
            {"hour": h, "route_class": r, "status": s, "count": count}
            for (h, r, s), count in sorted(retained.items())
        ],
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


@contextmanager
def _lock(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def aggregate(raw_path: Path, output_path: Path, *, now: datetime) -> dict[str, int]:
    raw_path = raw_path.resolve(strict=True)
    output_path = output_path.absolute()
    if raw_path.is_symlink() or output_path.is_symlink():
        raise ValueError("aggregate paths must not be symlinks")
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _lock(output_path.with_suffix(output_path.suffix + ".lock")):
        before = _regular_nonsymlink(raw_path)
        raw = raw_path.read_bytes()
        observed = _regular_nonsymlink(raw_path)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns
        ):
            raise RuntimeError("outcome log changed during read")
        incoming = parse_lines(raw)
        merged = _load_existing(output_path)
        for key, count in incoming.items():
            merged[key] = merged.get(key, 0) + count
        payload = _payload(merged, now=now)
        temporary = output_path.with_name(output_path.name + ".partial")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
        descriptor = os.open(raw_path, os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
        os.close(descriptor)
    return {"raw_lines_consumed": len(raw.splitlines()), "aggregate_keys": len(merged)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, default=Path("/var/log/nginx/private-alpha-outcomes.log"))
    parser.add_argument("--output", type=Path, default=Path("/var/lib/private-alpha/operational/nginx-outcomes.json"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"mode": "plan", "raw": str(args.raw), "output": str(args.output)}))
        return
    print(json.dumps(aggregate(args.raw, args.output, now=datetime.now(timezone.utc)), sort_keys=True))


if __name__ == "__main__":
    main()
