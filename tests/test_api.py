"""Tests for the Peatus.ee API client."""

from __future__ import annotations

import pytest

from custom_components.peatus.api import PeatusApi, _classify_mode


@pytest.mark.parametrize(
    ("mode", "long_name", "expected"),
    [
        # Tallinn publishes trolleybus routes as buses, marked only in the name.
        ("BUS", "Mustamäe - Kopli (trollibuss)", "trolleybus"),
        ("BUS", "Keskuse - Balti jaam (trollibuss)", "trolleybus"),
        ("BUS", "MUSTAMÄE - KOPLI (TROLLIBUSS)", "trolleybus"),
        # Ordinary buses stay buses.
        ("BUS", "Vana-Pääsküla - Viru - Viimsi", "bus"),
        ("BUS", None, "bus"),
        # Other modes are never reclassified.
        ("TRAM", "Tondi - Kadriorg", "tram"),
        ("RAIL", "Tallinn - Tartu", "rail"),
        (None, "Something (trollibuss)", None),
    ],
)
def test_classify_mode(mode, long_name, expected) -> None:
    """Trolleybus routes are separated out of the buses by their long name."""
    assert _classify_mode(mode, long_name) == expected


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [("TRAM", "tram"), ("RAIL", "rail"), (None, None)],
)
def test_parse_stop_lowercases_vehicle_mode(raw_mode, expected) -> None:
    """The feed's upper case vehicleMode is normalised on the way in."""
    stop = PeatusApi._parse_stop(
        {"gtfsId": "estonia:1292", "name": "Balti jaam", "vehicleMode": raw_mode}
    )
    assert stop.vehicle_mode == expected
