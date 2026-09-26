"""Fixtures for the Peatus.ee integration tests."""

from __future__ import annotations

import pytest

from custom_components.peatus.api import Departure, Itinerary, Leg, Stop

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


#: Every journey fixture hangs off the same epoch the departure ones do.
BASE = 1789506000


def make_leg(
    mode: str = "bus",
    start: int = 0,
    duration: int = 600,
    route: str | None = "18",
    **kwargs,
) -> Leg:
    """Build a Leg starting ``start`` seconds after the fixed epoch."""
    defaults = {
        "distance": 4200,
        "from_name": "Vana-Pääsküla",
        "from_stop_id": "estonia:952",
        "from_stop_code": "04401-1",
        "to_name": "Järve",
        "to_stop_id": "estonia:1053",
        "to_stop_code": "06801-1",
        "route_long_name": "Viru keskus - Urda",
        "headsign": "Viru keskus",
        "trip_id": "estonia:17206",
        "color": "#de2c42",
        "text_color": "#ffffff",
        "realtime": False,
    }
    # A leg that is not a ride carries none of the service details, which is
    # what a card keys on to tell a walk from a bus.
    if route is None:
        defaults |= {
            "route_long_name": None,
            "headsign": None,
            "trip_id": None,
            "color": None,
            "text_color": None,
            "from_stop_id": None,
            "from_stop_code": None,
            "to_stop_id": None,
            "to_stop_code": None,
        }
    return Leg(
        mode=mode,
        start_timestamp=BASE + start,
        end_timestamp=BASE + start + duration,
        duration=duration,
        route_short_name=route,
        **{**defaults, **kwargs},
    )


def make_itinerary(legs: list[Leg] | None = None, **kwargs) -> Itinerary:
    """Build an Itinerary that its legs tile exactly."""
    if legs is None:
        legs = [
            make_leg(mode="walk", start=0, duration=300, route=None),
            make_leg(mode="bus", start=300, duration=900),
            make_leg(mode="walk", start=1200, duration=240, route=None),
        ]
    start = min(leg.start_timestamp for leg in legs)
    end = max(leg.end_timestamp for leg in legs)
    defaults = {
        "start_timestamp": start,
        "end_timestamp": end,
        "duration": end - start,
        "walk_seconds": sum(leg.duration for leg in legs if leg.mode == "walk"),
        "wait_seconds": sum(leg.duration for leg in legs if leg.mode == "wait"),
        "walk_distance": 796,
    }
    return Itinerary(legs=legs, **{**defaults, **kwargs})
