"""Tests for the sensors of a journey board."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import peatus
from custom_components.peatus.const import (
    BOARD_JOURNEY,
    CONF_BOARD,
    CONF_DESTINATION_ENTITY,
    CONF_DESTINATION_ID,
    CONF_DESTINATION_NAME,
    CONF_MODES,
    CONF_ORIGIN_ENTITY,
    CONF_ORIGIN_NAME,
    CONF_ROUTES,
    CONF_STOP_CODE,
    CONF_STOP_DESC,
    CONF_STOP_ID,
    CONF_STOP_MODE,
    CONF_STOP_NAME,
    DOMAIN,
    NUM_ITINERARIES,
)
from homeassistant.const import CONF_SCAN_INTERVAL, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .conftest import BASE, make_departure, make_itinerary, make_leg

ORIGIN = "person.romi"
DESTINATION = "zone.work"


@pytest.fixture(name="api")
def api_fixture():
    """Patch the API client used by the journey coordinator."""
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        client = mock.return_value
        client.async_plan = AsyncMock(return_value=[make_itinerary() for _ in range(3)])
        client.async_plan_walk = AsyncMock(
            return_value=make_itinerary(
                [make_leg(mode="walk", start=0, duration=6480, route=None)]
            )
        )
        client.async_plan_bicycle = AsyncMock(
            return_value=make_itinerary(
                [
                    make_leg(mode="walk", start=0, duration=36, route=None),
                    make_leg(mode="bicycle", start=36, duration=1842, route=None),
                ]
            )
        )
        yield client


def build_entry(**overrides) -> MockConfigEntry:
    """Create a journey board between two places."""
    data = {
        CONF_BOARD: BOARD_JOURNEY,
        CONF_ORIGIN_ENTITY: ORIGIN,
        CONF_ORIGIN_NAME: "Home",
        CONF_DESTINATION_ENTITY: DESTINATION,
        CONF_DESTINATION_NAME: "Work",
    }
    data.update(overrides)
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home → Work",
        data=data,
        options={CONF_MODES: ["bus"], CONF_ROUTES: [], CONF_SCAN_INTERVAL: 5},
        unique_id=f"{BOARD_JOURNEY}:{ORIGIN}|{DESTINATION}",
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Place both ends, then add and set up a config entry."""
    hass.states.async_set(ORIGIN, "home", {"latitude": 59.35, "longitude": 24.63})
    hass.states.async_set(DESTINATION, "1", {"latitude": 59.39, "longitude": 24.72})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_journey_entities_created(hass: HomeAssistant, api) -> None:
    """A journey board has one sensor per itinerary, plus the two fixed plans."""
    await setup_entry(hass, build_entry())

    entity_ids = hass.states.async_entity_ids("sensor")
    assert len(entity_ids) == NUM_ITINERARIES + 2
    assert "sensor.peatus_home_work_journey_1" in entity_ids
    assert f"sensor.peatus_home_work_journey_{NUM_ITINERARIES}" in entity_ids
    assert "sensor.peatus_home_work_walk" in entity_ids
    assert "sensor.peatus_home_work_ride" in entity_ids


async def test_there_is_no_next_journey_sensor(hass: HomeAssistant, api) -> None:
    """Consecutive journeys can use different lines, so "next" would mislead."""
    await setup_entry(hass, build_entry())

    assert "sensor.peatus_home_work_next_journey" not in hass.states.async_entity_ids(
        "sensor"
    )


async def test_state_is_when_to_leave(hass: HomeAssistant, api) -> None:
    """A journey's state is the moment the traveller has to set off."""
    await setup_entry(hass, build_entry())

    state = hass.states.get("sensor.peatus_home_work_journey_1")
    assert state.state == dt_util.utc_from_timestamp(BASE).isoformat()
    assert state.attributes["device_class"] == "timestamp"
    # Also an attribute, so a card can read the start the same way on the
    # walking and cycling sensors, whose state is a duration instead.
    assert state.attributes["start_time"] == state.state


async def test_fixed_plans_are_measured_in_minutes(hass: HomeAssistant, api) -> None:
    """Walking and cycling start whenever you do, so their state is a duration."""
    await setup_entry(hass, build_entry())

    walk = hass.states.get("sensor.peatus_home_work_walk")
    assert walk.state == "108"
    assert walk.attributes["device_class"] == "duration"
    assert walk.attributes["unit_of_measurement"] == "min"

    ride = hass.states.get("sensor.peatus_home_work_ride")
    assert ride.state == "31"


async def test_legs_carry_everything_a_card_needs(hass: HomeAssistant, api) -> None:
    """Each leg is published with its own timing, line and colours."""
    await setup_entry(hass, build_entry())

    legs = hass.states.get("sensor.peatus_home_work_journey_1").attributes["legs"]
    assert [leg["mode"] for leg in legs] == ["walk", "bus", "walk"]

    ride = legs[1]
    assert ride["route"] == "18"
    assert ride["minutes"] == 15
    assert ride["duration"] == 900
    assert ride["headsign"] == "Viru keskus"
    assert ride["from"] == "Vana-Pääsküla"
    assert ride["from_stop_code"] == "04401-1"
    # Colours arrive ready for CSS; the feed publishes them bare.
    assert ride["color"] == "#de2c42"
    assert ride["text_color"] == "#ffffff"

    # A walk carries no service details, which is how a card tells the two
    # apart without knowing every mode name.
    assert legs[0]["route"] is None
    assert legs[0]["color"] is None
    assert legs[0]["trip_id"] is None


async def test_legs_tile_the_journey(hass: HomeAssistant, api) -> None:
    """The legs of a journey account for all of it, waiting included.

    This is the property a proportional bar depends on, asserted here at the
    state machine rather than inside the parser.
    """
    api.async_plan = AsyncMock(
        return_value=[
            make_itinerary(
                [
                    make_leg(mode="walk", start=0, duration=300, route=None),
                    make_leg(mode="bus", start=300, duration=900, route="18"),
                    make_leg(mode="wait", start=1200, duration=641, route=None),
                    make_leg(mode="bus", start=1841, duration=600, route="34"),
                ]
            )
        ]
    )
    await setup_entry(hass, build_entry())

    attributes = hass.states.get("sensor.peatus_home_work_journey_1").attributes
    assert (
        sum(leg["duration"] for leg in attributes["legs"])
        == (attributes["duration_seconds"])
    )
    assert attributes["wait_minutes"] == 11
    assert attributes["transfers"] == 1
    assert attributes["routes"] == ["18", "34"]


async def test_departure_names_the_first_ride(hass: HomeAssistant, api) -> None:
    """ "Departs at" is about boarding, not about leaving the house."""
    await setup_entry(hass, build_entry())

    attributes = hass.states.get("sensor.peatus_home_work_journey_1").attributes
    # The walk is 300 seconds, so boarding is later than the state.
    assert attributes["departure_time"] == (
        dt_util.utc_from_timestamp(BASE + 300).isoformat()
    )
    assert attributes["departure_stop"] == "Vana-Pääsküla"
    assert attributes["departure_stop_code"] == "04401-1"


async def test_a_walk_has_no_departure(hass: HomeAssistant, api) -> None:
    """A plan that boards nothing has no stop to leave from."""
    await setup_entry(hass, build_entry())

    attributes = hass.states.get("sensor.peatus_home_work_walk").attributes
    assert "departure_time" not in attributes
    assert "departure_stop" not in attributes
    assert attributes["transfers"] == 0


async def test_empty_slots_still_name_the_places(hass: HomeAssistant, api) -> None:
    """A sensor with no journey keeps enough for a card to bind to."""
    await setup_entry(hass, build_entry())

    state = hass.states.get(f"sensor.peatus_home_work_journey_{NUM_ITINERARIES}")
    assert state.state == STATE_UNKNOWN
    assert state.attributes["origin"] == "Home"
    assert state.attributes["destination"] == "Work"
    assert state.attributes["origin_entity_id"] == ORIGIN
    assert state.attributes["legs"] == []


async def test_friendly_names(hass: HomeAssistant, api) -> None:
    """Names read as the device plus what the sensor is."""
    await setup_entry(hass, build_entry())

    assert (
        hass.states.get("sensor.peatus_home_work_journey_2").attributes["friendly_name"]
        == "Home → Work Journey 2"
    )
    assert (
        hass.states.get("sensor.peatus_home_work_walk").attributes["friendly_name"]
        == "Home → Work Walking"
    )


async def test_icons_follow_the_first_ride(hass: HomeAssistant, api) -> None:
    """A journey is iconed by what you board first; a walk by walking."""
    await setup_entry(hass, build_entry())

    assert (
        hass.states.get("sensor.peatus_home_work_journey_1").attributes["icon"]
        == "mdi:bus"
    )
    # The fixed plans leave their icons to icons.json.
    assert "icon" not in hass.states.get("sensor.peatus_home_work_walk").attributes

    icons = json.loads((Path(peatus.__file__).parent / "icons.json").read_text())[
        "entity"
    ]["sensor"]
    assert icons["journey"]["default"] == "mdi:routes"
    assert icons["walk"]["default"] == "mdi:walk"
    assert icons["ride"]["default"] == "mdi:bike"


async def test_device_describes_the_journey(hass: HomeAssistant, api) -> None:
    """The device card names both ends and which entities feed them."""
    entry = build_entry()
    await setup_entry(hass, entry)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device.name == "Home → Work"
    assert device.model == "Journey Home → Work"
    assert device.model_id == f"{ORIGIN} → {DESTINATION}"
    assert device.manufacturer == "peatus.ee"


async def test_a_board_without_a_kind_is_still_a_stop_board(
    hass: HomeAssistant,
) -> None:
    """Entries made before journey boards existed need no migration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Viru (12102-1)",
        # No CONF_BOARD key at all, exactly as an older entry has it.
        data={
            CONF_STOP_ID: "estonia:1292",
            CONF_STOP_NAME: "Viru",
            CONF_STOP_CODE: "12102-1",
            CONF_STOP_MODE: "tram",
            CONF_STOP_DESC: None,
            CONF_DESTINATION_ID: None,
            CONF_DESTINATION_NAME: None,
        },
        options={CONF_MODES: ["tram"], CONF_SCAN_INTERVAL: 1},
        unique_id="estonia:1292|any",
    )
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        client = mock.return_value
        client.async_get_departures = AsyncMock(return_value=[make_departure(60)])
        client.async_get_pattern_codes_to = AsyncMock(return_value=set())
        client.async_get_ride_seconds = AsyncMock(return_value={})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert "sensor.peatus_viru_next_departure" in hass.states.async_entity_ids("sensor")


async def test_unload_entry(hass: HomeAssistant, api) -> None:
    """Unloading a journey board makes its sensors unavailable."""
    entry = build_entry()
    await setup_entry(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.peatus_home_work_journey_1").state == "unavailable"
