"""Tests for the Peatus.ee options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.peatus.api import PeatusApiError
from custom_components.peatus.const import (
    BOARD_JOURNEY,
    CONF_BIKE_OPTIMIZE,
    CONF_BIKE_SPEED,
    CONF_BOARD,
    CONF_DESTINATION_ENTITY,
    CONF_DESTINATION_NAME,
    CONF_MODES,
    CONF_ORIGIN_ENTITY,
    CONF_ORIGIN_NAME,
    CONF_ROUTES,
    CONF_STOP_ID,
    CONF_WALK_SPEED,
    DOMAIN,
)
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import make_departure, make_stop


@pytest.fixture(name="api")
def api_fixture():
    """Patch the API client used by the coordinator."""
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        client = mock.return_value
        client.async_get_departures = AsyncMock(
            return_value=[make_departure(60), make_departure(120, mode="bus")]
        )
        client.async_get_pattern_codes_to = AsyncMock(return_value=set())
        client.async_get_ride_seconds = AsyncMock(return_value={})
        yield client


@pytest.fixture(name="flow_api")
def flow_api_fixture():
    """Patch the API client the options form lists the stop's routes with."""
    with patch("custom_components.peatus.config_flow.PeatusApi") as mock:
        client = mock.return_value
        client.async_get_stop = AsyncMock(
            return_value=make_stop("estonia:1292", "Viru", routes=["T3", "1", "10"])
        )
        yield client


def _routes_offered(result) -> list[str]:
    """Return the route options the form's line selector offers."""
    selector = result["data_schema"].schema[CONF_ROUTES].config
    return list(selector["options"])


async def test_options_flow_updates_modes_and_interval(
    hass: HomeAssistant, api, flow_api
) -> None:
    """Changing options reloads the entry and applies the new settings."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Viru",
        data={CONF_STOP_ID: "estonia:1292"},
        options={CONF_MODES: ["tram"], CONF_SCAN_INTERVAL: 1},
        unique_id="estonia:1292|any",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Only the TRAM departure is exposed to begin with.
    assert entry.runtime_data.data[0].mode == "tram"
    assert len(entry.runtime_data.data) == 1
    assert entry.runtime_data.update_interval.total_seconds() == 60

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    # The lines calling at the stop are offered in timetable order.
    assert _routes_offered(result) == ["1", "10", "T3"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODES: ["tram", "bus"], CONF_SCAN_INTERVAL: 5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options == {
        CONF_MODES: ["tram", "bus"],
        CONF_ROUTES: [],
        CONF_SCAN_INTERVAL: 5,
    }
    # The reload picked up both the new modes and the new interval.
    assert len(entry.runtime_data.data) == 2
    assert entry.runtime_data.update_interval.total_seconds() == 300


async def test_options_flow_filters_by_route(
    hass: HomeAssistant, api, flow_api
) -> None:
    """Picking lines narrows the board to those routes."""
    api.async_get_departures = AsyncMock(
        return_value=[
            make_departure(60, route="1"),
            make_departure(120, route="10"),
            make_departure(180, route="163"),
        ]
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Vana-Pääsküla",
        data={CONF_STOP_ID: "estonia:952"},
        options={CONF_MODES: ["tram"], CONF_SCAN_INTERVAL: 1},
        unique_id="estonia:952|any",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert len(entry.runtime_data.data) == 3

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_MODES: ["tram"], CONF_ROUTES: ["1", "10"], CONF_SCAN_INTERVAL: 1},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_ROUTES] == ["1", "10"]
    assert [d.route_short_name for d in entry.runtime_data.data] == ["1", "10"]


async def test_options_flow_keeps_withdrawn_route_selectable(
    hass: HomeAssistant, api, flow_api
) -> None:
    """A filtered line the timetable has since dropped stays in the form."""
    flow_api.async_get_stop.return_value = make_stop(
        "estonia:952", "Vana-Pääsküla", routes=["1", "10"]
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_STOP_ID: "estonia:952"},
        options={
            CONF_MODES: ["tram"],
            CONF_ROUTES: ["10", "27"],
            CONF_SCAN_INTERVAL: 1,
        },
        unique_id="estonia:952|any",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    # 27 is no longer served but is still selected, so it is still offered.
    assert _routes_offered(result) == ["1", "10", "27"]


async def test_options_flow_survives_unreachable_api(
    hass: HomeAssistant, api, flow_api
) -> None:
    """The form still opens when the stop's routes cannot be listed."""
    flow_api.async_get_stop.side_effect = PeatusApiError("down")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_STOP_ID: "estonia:952"},
        options={CONF_MODES: ["tram"], CONF_ROUTES: ["10"], CONF_SCAN_INTERVAL: 1},
        unique_id="estonia:952|any",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["step_id"] == "init"
    # Only the saved filter is left to offer, so it can be kept or cleared.
    assert _routes_offered(result) == ["10"]


# --- Journey boards ---------------------------------------------------------


def build_journey_entry(**options) -> MockConfigEntry:
    """Create a journey board entry, which holds no stop ID at all."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home → Work",
        data={
            CONF_BOARD: BOARD_JOURNEY,
            CONF_ORIGIN_ENTITY: "person.romi",
            CONF_ORIGIN_NAME: "Home",
            CONF_DESTINATION_ENTITY: "zone.work",
            CONF_DESTINATION_NAME: "Work",
        },
        options={
            CONF_MODES: ["bus"],
            CONF_ROUTES: [],
            CONF_SCAN_INTERVAL: 5,
            CONF_WALK_SPEED: 4.8,
            CONF_BIKE_SPEED: 18.0,
            CONF_BIKE_OPTIMIZE: "quick",
            **options,
        },
        unique_id="journey:person.romi|zone.work",
    )


@pytest.fixture(name="journey_api")
def journey_api_fixture():
    """Patch the API client used by the journey coordinator."""
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        client = mock.return_value
        client.async_plan = AsyncMock(return_value=[])
        client.async_plan_walk = AsyncMock(return_value=None)
        client.async_plan_bicycle = AsyncMock(return_value=None)
        yield client


async def setup_journey(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Place both ends and set up a journey entry."""
    hass.states.async_set("person.romi", "home", {"latitude": 59.35, "longitude": 24.6})
    hass.states.async_set("zone.work", "1", {"latitude": 59.39, "longitude": 24.72})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_journey_options_never_ask_the_feed_for_routes(
    hass: HomeAssistant, journey_api, flow_api
) -> None:
    """A journey has no stop to list lines from, and asking would fail.

    The stop board's form re-reads its stop to offer every line calling there.
    A journey entry holds no stop ID, so taking that path would raise KeyError
    before the form could even be shown.
    """
    entry = build_journey_entry()
    await setup_journey(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "journey_options"
    flow_api.async_get_stop.assert_not_awaited()


async def test_journey_options_keep_the_chosen_lines_offered(
    hass: HomeAssistant, journey_api, flow_api
) -> None:
    """The lines already chosen are the ones the selector lists."""
    entry = build_journey_entry(**{CONF_ROUTES: ["18", "1"]})
    await setup_journey(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    selector = result["data_schema"].schema[CONF_ROUTES].config
    assert list(selector["options"]) == ["1", "18"]
    # Typing a line the board has never seen must still be possible.
    assert selector["custom_value"] is True


async def test_journey_options_round_trip(
    hass: HomeAssistant, journey_api, flow_api
) -> None:
    """Every journey setting is saved, and nothing else is."""
    entry = build_journey_entry()
    await setup_journey(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MODES: ["rail"],
            CONF_ROUTES: ["R12"],
            CONF_SCAN_INTERVAL: 9,
            CONF_WALK_SPEED: 6.0,
            CONF_BIKE_SPEED: 22.5,
            CONF_BIKE_OPTIMIZE: "flat",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {
        CONF_MODES: ["rail"],
        CONF_ROUTES: ["R12"],
        CONF_SCAN_INTERVAL: 9,
        CONF_WALK_SPEED: 6.0,
        CONF_BIKE_SPEED: 22.5,
        CONF_BIKE_OPTIMIZE: "flat",
    }
