"""Tests for the Peatus.ee sensors."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import peatus
from custom_components.peatus.const import (
    CONF_DESTINATION_ID,
    CONF_DESTINATION_NAME,
    CONF_MODES,
    CONF_STOP_CODE,
    CONF_STOP_DESC,
    CONF_STOP_ID,
    CONF_STOP_MODE,
    CONF_STOP_NAME,
    DOMAIN,
    NUM_DEPARTURES,
)
from homeassistant.const import CONF_SCAN_INTERVAL, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .conftest import make_departure


@pytest.fixture(name="api")
def api_fixture():
    """Patch the API client used by the coordinator."""
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        client = mock.return_value
        client.async_get_departures = AsyncMock(
            return_value=[make_departure(60 * i) for i in range(1, 6)]
        )
        client.async_get_pattern_codes_to = AsyncMock(return_value={"p1"})
        yield client


def build_entry(modes: list[str] | None = None, **overrides) -> MockConfigEntry:
    """Create a config entry for the Viru tram stop."""
    data = {
        CONF_STOP_ID: "estonia:1292",
        CONF_STOP_NAME: "Viru",
        CONF_STOP_CODE: "12102-1",
        CONF_STOP_MODE: "TRAM",
        CONF_STOP_DESC: "Vabaduse väljaku suunas",
        CONF_DESTINATION_ID: None,
        CONF_DESTINATION_NAME: None,
    }
    data.update(overrides)
    return MockConfigEntry(
        domain=DOMAIN,
        title="Viru (12102-1)",
        data=data,
        options={CONF_MODES: modes or ["TRAM"], CONF_SCAN_INTERVAL: 1},
        unique_id="estonia:1292|any",
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up a config entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_entities_created(hass: HomeAssistant, api) -> None:
    """One 'next' sensor plus ten numbered departure sensors are created."""
    await setup_entry(hass, build_entry())

    states = hass.states.async_entity_ids("sensor")
    assert len(states) == NUM_DEPARTURES + 1
    assert "sensor.peatus_viru_next_departure" in states
    assert "sensor.peatus_viru_departure_10" in states


async def test_device_info(hass: HomeAssistant, api) -> None:
    """The device card identifies the stop and links to it on peatus.ee."""
    entry = build_entry()
    await setup_entry(hass, entry)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    assert device.name == "Viru"
    assert device.manufacturer == "peatus.ee"
    assert device.model == "Tram stop Viru"
    assert device.model_id == "Vabaduse väljaku suunas"
    # A stop is not hardware: no firmware version, no serial number.
    assert device.sw_version is None
    assert device.serial_number is None
    # The colon in a GTFS ID must be percent-encoded for the URL to resolve.
    assert device.configuration_url == "https://web.peatus.ee/pysakit/estonia%3A1292"


async def test_friendly_names_use_the_stop_only(hass: HomeAssistant, api) -> None:
    """Friendly names read "<stop> Departure n" without the stop code."""
    await setup_entry(hass, build_entry())

    names = {
        hass.states.get(f"sensor.peatus_viru_departure_{i}").attributes["friendly_name"]
        for i in (1, 10)
    }
    assert names == {"Viru Departure 1", "Viru Departure 10"}
    assert (
        hass.states.get("sensor.peatus_viru_next_departure").attributes["friendly_name"]
        == "Viru Next departure"
    )


async def test_device_model_names_a_route_when_filtered(
    hass: HomeAssistant, api
) -> None:
    """A destination-filtered board is a route, not a stop."""
    entry = build_entry(
        **{
            CONF_DESTINATION_ID: "estonia:1256",
            CONF_DESTINATION_NAME: "Vana-Lõuna",
        }
    )
    await setup_entry(hass, entry)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device.model == "Tram route Viru → Vana-Lõuna"
    # The description still belongs to the departure stop.
    assert device.model_id == "Vabaduse väljaku suunas"


async def test_friendly_names_include_destination(hass: HomeAssistant, api) -> None:
    """A destination-filtered board names the direction it covers."""
    await setup_entry(
        hass,
        build_entry(
            **{
                CONF_DESTINATION_ID: "estonia:1256",
                CONF_DESTINATION_NAME: "Vana-Lõuna",
            }
        ),
    )

    state = hass.states.get("sensor.peatus_viru_vana_louna_departure_1")
    assert state.attributes["friendly_name"] == "Viru → Vana-Lõuna Departure 1"


async def test_entity_ids_include_destination(hass: HomeAssistant, api) -> None:
    """A destination-filtered board carries the destination in the entity ID."""
    await setup_entry(
        hass,
        build_entry(
            **{
                CONF_DESTINATION_ID: "estonia:1256",
                # Diacritics must survive slugification.
                CONF_DESTINATION_NAME: "Vana-Lõuna",
            }
        ),
    )

    ids = hass.states.async_entity_ids("sensor")
    assert "sensor.peatus_viru_vana_louna_next_departure" in ids
    assert "sensor.peatus_viru_vana_louna_departure_1" in ids
    assert "sensor.peatus_viru_vana_louna_departure_10" in ids


async def test_state_is_timestamp_with_attributes(hass: HomeAssistant, api) -> None:
    """The state is the departure timestamp and carries route attributes."""
    await setup_entry(hass, build_entry())

    state = hass.states.get("sensor.peatus_viru_next_departure")
    assert state is not None
    expected = dt_util.utc_from_timestamp(1789506000 + 60)
    assert state.state == expected.isoformat()
    assert state.attributes["device_class"] == "timestamp"
    assert state.attributes["route"] == "T3"
    assert state.attributes["headsign"] == "Tondi"
    assert state.attributes["mode"] == "TRAM"
    assert state.attributes["realtime"] is True
    assert state.attributes["stop"] == "Viru"
    assert state.attributes["stop_code"] == "12102-1"
    # Kept deliberately: identifies the scheduled trip behind this departure.
    assert state.attributes["trip_id"] == "estonia:60"

    # The "next" sensor mirrors the first numbered sensor.
    first = hass.states.get("sensor.peatus_viru_departure_1")
    assert first is not None
    assert first.state == state.state


async def test_missing_departures_are_unknown(hass: HomeAssistant, api) -> None:
    """Sensors beyond the available departures report unknown, not an error."""
    await setup_entry(hass, build_entry())

    # Only five departures were returned by the mocked API.
    assert hass.states.get("sensor.peatus_viru_departure_5").state != STATE_UNKNOWN
    state = hass.states.get("sensor.peatus_viru_departure_6")
    assert state is not None
    assert state.state == STATE_UNKNOWN
    assert "route" not in state.attributes
    # Stop identity is still exposed so templates can rely on it.
    assert state.attributes["stop"] == "Viru"


async def test_departure_icons_follow_vehicle_mode(hass: HomeAssistant, api) -> None:
    """Each numbered departure is iconed by the mode of that departure."""
    api.async_get_departures.return_value = [
        make_departure(60, mode="BUS"),
        make_departure(120, mode="TROLLEYBUS"),
        make_departure(180, mode="TRAM"),
        make_departure(240, mode="RAIL"),
        make_departure(300, mode="FERRY"),
    ]
    await setup_entry(
        hass, build_entry(modes=["BUS", "TROLLEYBUS", "TRAM", "RAIL", "FERRY"])
    )

    icons = {
        i: hass.states.get(f"sensor.peatus_viru_departure_{i}").attributes.get("icon")
        for i in range(1, 6)
    }
    assert icons == {
        1: "mdi:bus",
        # Trolleybuses share the bus icon: MDI has no trolleybus glyph.
        2: "mdi:bus",
        3: "mdi:tram",
        4: "mdi:train",
        5: "mdi:ferry",
    }


async def test_next_departure_uses_static_icon(hass: HomeAssistant, api) -> None:
    """The next-departure sensor leaves the icon to icons.json (mdi:clock-out)."""
    await setup_entry(hass, build_entry())

    state = hass.states.get("sensor.peatus_viru_next_departure")
    # No icon on the state means the frontend applies the icons.json default.
    assert "icon" not in state.attributes

    icons = json.loads((Path(peatus.__file__).parent / "icons.json").read_text())[
        "entity"
    ]["sensor"]
    assert icons["next_departure"]["default"] == "mdi:clock-out"
    assert icons["departure"]["default"] == "mdi:timetable"


async def test_unknown_departure_has_no_icon(hass: HomeAssistant, api) -> None:
    """With no departure there is no mode, so the icons.json default applies."""
    await setup_entry(hass, build_entry())

    # The mocked API returns five departures, so the sixth sensor is empty.
    state = hass.states.get("sensor.peatus_viru_departure_6")
    assert state.state == STATE_UNKNOWN
    assert "icon" not in state.attributes


async def test_unload_entry(hass: HomeAssistant, api) -> None:
    """The entry unloads cleanly."""
    entry = build_entry()
    await setup_entry(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.peatus_viru_next_departure").state == "unavailable"
