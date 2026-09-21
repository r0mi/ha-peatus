"""Tests for the Peatus.ee options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.peatus.api import PeatusApiError
from custom_components.peatus.const import (
    CONF_MODES,
    CONF_ROUTES,
    CONF_STOP_ID,
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
