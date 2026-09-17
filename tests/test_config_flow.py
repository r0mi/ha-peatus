"""Tests for the Peatus.ee config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.peatus.api import PeatusApiError, PeatusStopNotFoundError
from custom_components.peatus.const import (
    CONF_DESTINATION_ID,
    CONF_MODES,
    CONF_STOP_DESC,
    CONF_STOP_ID,
    CONF_STOP_MODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import make_stop

ORIGIN = make_stop("estonia:1292", "Viru")
DESTINATION = make_stop("estonia:1256", "Vana-Lõuna", code="12345-1")


@pytest.fixture(name="api")
def api_fixture():
    """Patch the API client used by the config flow."""
    with patch("custom_components.peatus.config_flow.PeatusApi") as mock:
        client = mock.return_value
        client.async_search_stops = AsyncMock(return_value=[ORIGIN, DESTINATION])
        client.async_get_stop = AsyncMock(return_value=ORIGIN)
        yield client


@pytest.fixture(name="no_setup")
def no_setup_fixture():
    """Skip the real integration setup when an entry is created."""
    with patch("custom_components.peatus.async_setup_entry", return_value=True):
        yield


async def test_search_flow_without_destination(
    hass: HomeAssistant, api, no_setup
) -> None:
    """A stop can be set up by searching, leaving the destination empty."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "search"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Viru"}
    )
    assert result["step_id"] == "pick_stop"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": "estonia:1292"}
    )
    assert result["step_id"] == "destination"

    # Empty destination skips straight to the settings step.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "settings"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODES: ["tram"], CONF_SCAN_INTERVAL: 2}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Viru (12102-1)"
    assert result["data"][CONF_STOP_ID] == "estonia:1292"
    assert result["data"][CONF_STOP_MODE] == "tram"
    assert result["data"][CONF_STOP_DESC] == "Rong Balti jaama suunas"
    assert result["data"][CONF_DESTINATION_ID] is None
    assert result["options"] == {CONF_MODES: ["tram"], CONF_SCAN_INTERVAL: 2}


async def test_search_flow_with_destination(hass: HomeAssistant, api, no_setup) -> None:
    """A destination stop can be chosen to filter the departures."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "search"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Viru"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": "estonia:1292"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Vana"}
    )
    assert result["step_id"] == "pick_destination"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": "estonia:1256"}
    )
    assert result["step_id"] == "settings"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODES: ["tram"], CONF_SCAN_INTERVAL: 1}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Viru (12102-1) → Vana-Lõuna (12345-1)"
    assert result["data"][CONF_DESTINATION_ID] == "estonia:1256"


async def test_destination_cannot_equal_origin(
    hass: HomeAssistant, api, no_setup
) -> None:
    """Picking the origin as destination shows an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "search"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Viru"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": "estonia:1292"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Viru"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": "estonia:1292"}
    )
    assert result["errors"] == {"stop": "same_stop"}


@pytest.mark.parametrize(
    ("vehicle_mode", "expected"),
    [
        # A bus stop may also be served by trolleybus routes, which the feed
        # publishes as buses, so both are preselected.
        ("bus", ["bus", "trolleybus"]),
        ("tram", ["tram"]),
        (None, ["bus", "trolleybus", "tram", "rail", "ferry"]),
    ],
)
async def test_default_modes_for_stop(
    hass: HomeAssistant, api, no_setup, vehicle_mode, expected
) -> None:
    """The settings step preselects the modes the stop can actually serve."""
    api.async_get_stop.return_value = make_stop(
        "estonia:1292", "Viru", vehicle_mode=vehicle_mode
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gtfs_id"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STOP_ID: "estonia:1292"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "settings"

    defaults = {
        key.schema: key.default() for key in result["data_schema"].schema if key.default
    }
    assert defaults[CONF_MODES] == expected


async def test_default_scan_interval(hass: HomeAssistant, api, no_setup) -> None:
    """The settings step offers the default poll interval."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gtfs_id"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STOP_ID: "estonia:1292"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    defaults = {
        key.schema: key.default() for key in result["data_schema"].schema if key.default
    }
    assert defaults[CONF_SCAN_INTERVAL] == DEFAULT_SCAN_INTERVAL == 3


async def test_gtfs_id_flow(hass: HomeAssistant, api, no_setup) -> None:
    """A stop can be set up by pasting its GTFS ID."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gtfs_id"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STOP_ID: " estonia:1292 "}
    )
    assert result["step_id"] == "destination"
    api.async_get_stop.assert_awaited_once_with("estonia:1292")


async def test_gtfs_id_unknown_stop(hass: HomeAssistant, api) -> None:
    """An unknown GTFS ID is reported on the form."""
    api.async_get_stop.side_effect = PeatusStopNotFoundError
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gtfs_id"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STOP_ID: "estonia:9999999"}
    )
    assert result["errors"] == {CONF_STOP_ID: "unknown_stop"}


async def test_search_no_results(hass: HomeAssistant, api) -> None:
    """An empty search result is reported on the form."""
    api.async_search_stops.return_value = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "search"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "nope"}
    )
    assert result["errors"] == {CONF_NAME: "no_stops_found"}


async def test_search_api_error(hass: HomeAssistant, api) -> None:
    """A transport error is reported as cannot_connect."""
    api.async_search_stops.side_effect = PeatusApiError("boom")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "search"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Viru"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def _add_board(hass, api, destination: str | None = None):
    """Run the whole flow, optionally choosing a destination."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "search"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Viru"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": "estonia:1292"}
    )
    if destination is None:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    else:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: "Vana"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"stop": destination}
        )
    if result["type"] is not FlowResultType.FORM:
        return result
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODES: ["tram"], CONF_SCAN_INTERVAL: 1}
    )


async def test_same_stop_can_be_added_again_with_a_destination(
    hass: HomeAssistant, api, no_setup
) -> None:
    """A stop configured without a destination can be added again with one.

    Regression test: the duplicate check used to run before the destination was
    known, so the second board was rejected as a duplicate of the first.
    """
    first = await _add_board(hass, api)
    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert first["data"][CONF_DESTINATION_ID] is None

    second = await _add_board(hass, api, destination="estonia:1256")
    assert second["type"] is FlowResultType.CREATE_ENTRY
    assert second["data"][CONF_DESTINATION_ID] == "estonia:1256"


async def test_duplicate_entry_aborts(hass: HomeAssistant, api, no_setup) -> None:
    """The very same stop and destination pair cannot be added twice."""
    assert (await _add_board(hass, api))["type"] is FlowResultType.CREATE_ENTRY

    repeat = await _add_board(hass, api)
    assert repeat["type"] is FlowResultType.ABORT
    assert repeat["reason"] == "already_configured"


async def test_duplicate_destination_pair_aborts(
    hass: HomeAssistant, api, no_setup
) -> None:
    """Two boards for the same stop and the same destination are refused."""
    first = await _add_board(hass, api, destination="estonia:1256")
    assert first["type"] is FlowResultType.CREATE_ENTRY

    repeat = await _add_board(hass, api, destination="estonia:1256")
    assert repeat["type"] is FlowResultType.ABORT
    assert repeat["reason"] == "already_configured"
