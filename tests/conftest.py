"""Fixtures for the Peatus.ee integration tests."""

from __future__ import annotations

import pytest

from custom_components.peatus.api import Departure, Stop

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of custom integrations in all tests."""
    return


def make_stop(gtfs_id: str, name: str, **kwargs) -> Stop:
    """Build a Stop for tests."""
    defaults = {
        "code": "12102-1",
        "desc": "Rong Balti jaama suunas",
        "zone_id": "Harju1",
        "vehicle_mode": "tram",
        "lat": 59.4,
        "lon": 24.7,
        "routes": ["T3", "T4"],
    }
    return Stop(gtfs_id=gtfs_id, name=name, **{**defaults, **kwargs})


def make_departure(
    offset: int,
    mode: str = "tram",
    pattern: str = "p1",
    long_name: str = "Tondi - Kadriorg",
    route: str = "T3",
) -> Departure:
    """Build a Departure ``offset`` seconds after a fixed epoch."""
    base = 1789506000
    return Departure(
        timestamp=base + offset,
        scheduled_timestamp=base + offset,
        realtime=True,
        realtime_state="UPDATED",
        delay_seconds=0,
        headsign="Tondi",
        route_short_name=route,
        route_long_name=long_name,
        mode=mode,
        trip_id=f"estonia:{offset}",
        pattern_code=pattern,
    )
