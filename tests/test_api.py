"""Tests for the Peatus.ee API client."""

from __future__ import annotations

import pytest

from custom_components.peatus.api import _classify_mode


@pytest.mark.parametrize(
    ("mode", "long_name", "expected"),
    [
        # Tallinn publishes trolleybus routes as buses, marked only in the name.
        ("BUS", "Mustamäe - Kopli (trollibuss)", "TROLLEYBUS"),
        ("BUS", "Keskuse - Balti jaam (trollibuss)", "TROLLEYBUS"),
        ("BUS", "MUSTAMÄE - KOPLI (TROLLIBUSS)", "TROLLEYBUS"),
        # Ordinary buses stay buses.
        ("BUS", "Vana-Pääsküla - Viru - Viimsi", "BUS"),
        ("BUS", None, "BUS"),
        # Other modes are never reclassified.
        ("TRAM", "Tondi - Kadriorg", "TRAM"),
        ("RAIL", "Tallinn - Tartu", "RAIL"),
        (None, "Something (trollibuss)", None),
    ],
)
def test_classify_mode(mode, long_name, expected) -> None:
    """Trolleybus routes are separated out of the buses by their long name."""
    assert _classify_mode(mode, long_name) == expected
