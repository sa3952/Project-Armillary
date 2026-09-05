"""Dependency-light fixtures shared by private and exported backend tests."""

from __future__ import annotations

import socket
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def minimal_chart_payload(
    *,
    year: int = 2000,
    month: int = 1,
    day: int = 1,
    hour: int = 12,
    minute: int = 0,
    latitude: float = 25.033,
    longitude: float = 121.5654,
    altitude_m: float = 10,
    timezone: dict | None = None,
) -> dict:
    return {
        "datetime": {
            "year": year, "month": month, "day": day,
            "hour": hour, "minute": minute, "second": 0,
        },
        "timezone": timezone or {
            "mode": "iana", "iana_name": "Asia/Taipei", "fold": 0,
        },
        "location": {
            "latitude": latitude, "longitude": longitude,
            "altitude_m": altitude_m,
        },
    }


def chart_payload(
    *,
    precision: str = "exact",
    atmosphere: dict | None = None,
    mode: dict | None = None,
    options: dict | None = None,
    **values,
) -> dict:
    payload = minimal_chart_payload(**{
        key: values.pop(key)
        for key in tuple(values)
        if key in {
            "year", "month", "day", "hour", "minute", "latitude",
            "longitude", "altitude_m", "timezone",
        }
    })
    payload.update({
        "birth_time_precision": precision,
        "options": {"house_system": "W", **(options or {}), **values},
    })
    if atmosphere is not None:
        payload["atmosphere"] = atmosphere
    if mode is not None:
        payload["computation_mode"] = mode
    return payload


def unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_request(
    url: str,
    body: bytes | None = None,
    *,
    content_type: str | None = None,
    timeout: float = 20,
) -> tuple[int, bytes]:
    media_type = content_type or ("application/json" if body is not None else None)
    headers = {"Content-Type": media_type} if media_type else {}
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
