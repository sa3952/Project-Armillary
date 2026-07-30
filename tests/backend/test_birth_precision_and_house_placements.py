"""Focused boundary tests for approximate time and planet-in-house rules."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.core.birth_time_sensitivity as sensitivity_module
from app.core.house_placements import compute_planet_house_placements
from app.core.trace import Trace
from app.main import app


client = TestClient(app)


def _context(**overrides):
    mode = {
        "center": "geocentric",
        "ecliptic_frame": "of_date",
        "nutation": True,
        **overrides,
    }
    return SimpleNamespace(mode=SimpleNamespace(**mode))


def _houses():
    return {
        "system_code": "W",
        "system_name": "Whole Sign",
        "cusps": [float(value) for value in range(0, 360, 30)],
    }


@pytest.mark.parametrize(
    ("longitude", "expected_house"),
    [
        (359.999999, 12),
        (0.0, 1),
        (29.999999, 1),
        (30.0, 2),
        (330.0, 12),
    ],
)
def test_planet_house_intervals_are_half_open_at_exact_cusps(
    longitude,
    expected_house,
):
    receipt = compute_planet_house_placements(
        [
            {
                "key": "sun",
                "name": "太陽",
                "longitude": longitude,
            }
        ],
        _houses(),
        _context(),
        Trace(),
    )

    assert receipt["execution_status"] == "computed"
    assert receipt["placements"][0]["house"] == expected_house
    assert receipt["placements"][0]["on_cusp"] is (
        longitude in {0.0, 30.0, 330.0}
    )


def test_approximate_hour_reuses_midpoint_instead_of_running_five_extra_probes(
    monkeypatch,
):
    calls = 0
    original = sensitivity_module.compute_bodies

    def recording_compute_bodies(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        sensitivity_module,
        "compute_bodies",
        recording_compute_bodies,
    )
    payload = {
        "birth_time_precision": "approximate_hour",
        "datetime": {
            "year": 2000,
            "month": 1,
            "day": 1,
            "hour": 13,
            "minute": 0,
            "second": 0,
        },
        "timezone": {
            "mode": "iana",
            "iana_name": "Asia/Taipei",
        },
        "location": {
            "latitude": 25.033,
            "longitude": 121.5654,
            "altitude_m": 10,
        },
        "options": {},
    }

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    assert calls == 4
