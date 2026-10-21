"""Tests for the time controls of a journey board."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.peatus.const import (
    CONF_DESTINATION_ID,
    CONF_DESTINATION_NAME,
    CONF_MODES,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    DOMAIN,
    TIME_ARRIVE,
    TIME_DEPART,
    TIME_NOW,
)
from homeassistant.const import CONF_SCAN_INTERVAL, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .conftest import make_departure, make_itinerary
from .test_journey_sensor import build_entry, setup_entry

SELECT = "select.peatus_home_work_time_mode"
TIME = "datetime.peatus_home_work_time"


@pytest.fixture(name="api")
def api_fixture():
    """Patch the API client used by the journey coordinator."""
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        client = mock.return_value
        client.async_plan = AsyncMock(return_value=[make_itinerary()])
        client.async_plan_walk = AsyncMock(return_value=make_itinerary())
        client.async_plan_bicycle = AsyncMock(return_value=make_itinerary())
        yield client


async def test_a_new_board_plans_from_now(hass: HomeAssistant, api) -> None:
    """Both controls exist and start out planning from now."""
    await setup_entry(hass, build_entry())

    assert hass.states.get(SELECT).state == TIME_NOW
    assert hass.states.get(SELECT).attributes["options"] == [
        TIME_NOW,
        TIME_DEPART,
        TIME_ARRIVE,
    ]
    assert hass.states.get(TIME).state == STATE_UNKNOWN
    journey = hass.states.get("sensor.peatus_home_work_journey_1")
    assert journey.attributes["time_mode"] == TIME_NOW
    assert journey.attributes["requested_time"] is None


async def test_choosing_a_time_replans_the_board(hass: HomeAssistant, api) -> None:
    """Setting the mode and the time plans for them without a reload."""
    await setup_entry(hass, build_entry())
    when = (dt_util.utcnow() + timedelta(hours=3)).replace(microsecond=0)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": SELECT, "option": TIME_ARRIVE},
        blocking=True,
    )
    await hass.services.async_call(
        "datetime",
        "set_value",
        {"entity_id": TIME, "datetime": when},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(SELECT).state == TIME_ARRIVE
    assert dt_util.parse_datetime(hass.states.get(TIME).state) == when
    plan_options = api.async_plan.await_args.args[4]
    assert plan_options.when == when
    assert plan_options.arrive_by is True
    journey = hass.states.get("sensor.peatus_home_work_journey_1")
    assert journey.attributes["time_mode"] == TIME_ARRIVE
    assert journey.attributes["requested_time"] == when.isoformat()


async def test_the_choice_survives_a_restart(hass: HomeAssistant, api) -> None:
    """The last mode and time are restored and planned for."""
    when = (dt_util.utcnow() + timedelta(hours=3)).replace(microsecond=0)
    mock_restore_cache(
        hass,
        [State(SELECT, TIME_DEPART), State(TIME, when.isoformat())],
    )

    await setup_entry(hass, build_entry())
    await hass.async_block_till_done()

    assert hass.states.get(SELECT).state == TIME_DEPART
    assert dt_util.parse_datetime(hass.states.get(TIME).state) == when
    assert api.async_plan.await_args.args[4].when == when


async def test_a_stop_board_has_no_time_controls(hass: HomeAssistant) -> None:
    """Departure boards always show what leaves next."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Viru",
        data={
            CONF_STOP_ID: "estonia:1292",
            CONF_STOP_NAME: "Viru",
            CONF_DESTINATION_ID: None,
            CONF_DESTINATION_NAME: None,
        },
        options={CONF_MODES: ["tram"], CONF_SCAN_INTERVAL: 1},
    )
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        client = mock.return_value
        client.async_get_departures = AsyncMock(return_value=[make_departure(60)])
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.async_entity_ids("select") == []
    assert hass.states.async_entity_ids("datetime") == []
