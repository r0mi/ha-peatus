"""Tests for the Peatus.ee options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.peatus.const import CONF_MODES, CONF_STOP_ID, DOMAIN
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import make_departure


@pytest.fixture(name="api")
def api_fixture():
    """Patch the API client used by the coordinator."""
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        client = mock.return_value
        client.async_get_departures = AsyncMock(
            return_value=[make_departure(60), make_departure(120, mode="bus")]
        )
        client.async_get_pattern_codes_to = AsyncMock(return_value=set())
        yield client


async def test_options_flow_updates_modes_and_interval(
    hass: HomeAssistant, api
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

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODES: ["tram", "bus"], CONF_SCAN_INTERVAL: 5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options == {CONF_MODES: ["tram", "bus"], CONF_SCAN_INTERVAL: 5}
    # The reload picked up both the new modes and the new interval.
    assert len(entry.runtime_data.data) == 2
    assert entry.runtime_data.update_interval.total_seconds() == 300
