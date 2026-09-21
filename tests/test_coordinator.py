"""Tests for the Peatus.ee data coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.peatus.api import PeatusApiError
from custom_components.peatus.const import (
    CONF_DESTINATION_ID,
    CONF_MODES,
    CONF_ROUTES,
    CONF_STOP_ID,
    DOMAIN,
    NUM_DEPARTURES,
)
from custom_components.peatus.coordinator import PeatusCoordinator
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import make_departure


def build_coordinator(
    hass: HomeAssistant,
    *,
    destination: str | None = None,
    modes: list[str] | None = None,
    routes: list[str] | None = None,
) -> tuple[PeatusCoordinator, AsyncMock]:
    """Create a coordinator with a mocked API client."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_STOP_ID: "estonia:1292", CONF_DESTINATION_ID: destination},
        options={
            CONF_MODES: modes or ["bus", "tram", "rail", "ferry"],
            CONF_ROUTES: routes or [],
            CONF_SCAN_INTERVAL: 1,
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        coordinator = PeatusCoordinator(hass, entry)
    return coordinator, mock.return_value


async def test_unfiltered_requests_only_what_is_needed(hass: HomeAssistant) -> None:
    """Without filters exactly NUM_DEPARTURES departures are requested once."""
    coordinator, api = build_coordinator(hass)
    api.async_get_departures = AsyncMock(
        return_value=[make_departure(60 * i) for i in range(NUM_DEPARTURES)]
    )

    data = await coordinator._async_update_data()

    assert len(data) == NUM_DEPARTURES
    api.async_get_departures.assert_awaited_once_with("estonia:1292", NUM_DEPARTURES)


async def test_destination_filter_starts_with_a_larger_batch(
    hass: HomeAssistant,
) -> None:
    """A destination filter skips the small request that would surely fail."""
    coordinator, api = build_coordinator(hass, destination="estonia:1256")
    api.async_get_pattern_codes_to = AsyncMock(return_value={"good"})
    api.async_get_departures = AsyncMock(
        return_value=[make_departure(60 * i, pattern="good") for i in range(60)]
    )

    await coordinator._async_update_data()

    api.async_get_departures.assert_awaited_once_with("estonia:1292", 60)


async def test_mode_filter_drops_other_modes(hass: HomeAssistant) -> None:
    """Departures whose mode is not selected are discarded."""
    coordinator, api = build_coordinator(hass, modes=["tram"])
    api.async_get_departures = AsyncMock(
        return_value=[
            make_departure(60, mode="tram"),
            make_departure(120, mode="bus"),
            make_departure(180, mode="tram"),
        ]
    )

    data = await coordinator._async_update_data()

    assert [d.mode for d in data] == ["tram", "tram"]


async def test_trolleybus_is_filterable_separately_from_bus(
    hass: HomeAssistant,
) -> None:
    """Selecting only Trolleybus excludes ordinary buses, and vice versa."""
    trolley = make_departure(60, mode="trolleybus")
    bus = make_departure(120, mode="bus")

    coordinator, api = build_coordinator(hass, modes=["trolleybus"])
    api.async_get_departures = AsyncMock(return_value=[trolley, bus])
    assert [d.mode for d in await coordinator._async_update_data()] == ["trolleybus"]

    coordinator, api = build_coordinator(hass, modes=["bus"])
    api.async_get_departures = AsyncMock(return_value=[trolley, bus])
    assert [d.mode for d in await coordinator._async_update_data()] == ["bus"]


async def test_unknown_mode_is_kept(hass: HomeAssistant) -> None:
    """A departure with no reported mode is not silently dropped."""
    coordinator, api = build_coordinator(hass, modes=["tram"])
    api.async_get_departures = AsyncMock(return_value=[make_departure(60, mode=None)])

    assert len(await coordinator._async_update_data()) == 1


async def test_destination_filter_uses_pattern_codes(hass: HomeAssistant) -> None:
    """Only departures on a pattern reaching the destination are kept."""
    coordinator, api = build_coordinator(hass, destination="estonia:1256")
    api.async_get_pattern_codes_to = AsyncMock(return_value={"good"})
    api.async_get_departures = AsyncMock(
        return_value=[
            make_departure(60, pattern="good"),
            make_departure(120, pattern="bad"),
            make_departure(180, pattern="good"),
        ]
    )

    data = await coordinator._async_update_data()

    assert [d.pattern_code for d in data] == ["good", "good"]
    api.async_get_pattern_codes_to.assert_awaited_once_with(
        "estonia:1292", "estonia:1256"
    )


async def test_pattern_codes_are_cached(hass: HomeAssistant) -> None:
    """The pattern lookup is not repeated on every update."""
    coordinator, api = build_coordinator(hass, destination="estonia:1256")
    api.async_get_pattern_codes_to = AsyncMock(return_value={"good"})
    api.async_get_departures = AsyncMock(
        return_value=[make_departure(60 * i, pattern="good") for i in range(20)]
    )

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert api.async_get_pattern_codes_to.await_count == 1


async def test_request_grows_when_filters_discard_results(
    hass: HomeAssistant,
) -> None:
    """A larger request is made when filtering leaves too few departures."""
    coordinator, api = build_coordinator(hass, destination="estonia:1256")
    api.async_get_pattern_codes_to = AsyncMock(return_value={"good"})

    def departures(_stop_id: str, count: int):
        # Only every twentieth departure matches, so the first batch of 60
        # yields 3 and the request has to grow.
        return [
            make_departure(60 * i, pattern="good" if i % 20 == 0 else "bad")
            for i in range(count)
        ]

    api.async_get_departures = AsyncMock(side_effect=departures)

    data = await coordinator._async_update_data()

    assert len(data) == NUM_DEPARTURES
    assert api.async_get_departures.await_count > 1
    requested = [call.args[1] for call in api.async_get_departures.await_args_list]
    assert requested == sorted(requested), "requests should grow monotonically"


async def test_stops_growing_when_feed_is_exhausted(hass: HomeAssistant) -> None:
    """No further requests are made once the feed returns fewer than asked."""
    coordinator, api = build_coordinator(hass, destination="estonia:1256")
    api.async_get_pattern_codes_to = AsyncMock(return_value={"good"})
    api.async_get_departures = AsyncMock(
        return_value=[make_departure(60, pattern="good")]
    )

    data = await coordinator._async_update_data()

    assert len(data) == 1
    api.async_get_departures.assert_awaited_once()


async def test_api_error_becomes_update_failed(hass: HomeAssistant) -> None:
    """API errors surface as UpdateFailed so HA retries."""
    coordinator, api = build_coordinator(hass)
    api.async_get_departures = AsyncMock(side_effect=PeatusApiError("boom"))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_route_filter_drops_other_routes(hass: HomeAssistant) -> None:
    """Only the selected lines are shown."""
    coordinator, api = build_coordinator(hass, routes=["1", "10"])
    api.async_get_departures = AsyncMock(
        return_value=[
            make_departure(60, route="1"),
            make_departure(120, route="163"),
            make_departure(180, route="10"),
            make_departure(240, route="191"),
        ]
    )

    data = await coordinator._async_update_data()

    assert [departure.route_short_name for departure in data] == ["1", "10"]


async def test_route_filter_matches_the_number_not_the_route_id(
    hass: HomeAssistant,
) -> None:
    """Both routes sharing a number match, as they are one line to the rider.

    The feed publishes a separate route per timetable period, so around a
    schedule change the same line appears under two IDs and one number.
    """
    coordinator, api = build_coordinator(hass, routes=["1"])
    api.async_get_departures = AsyncMock(
        return_value=[
            make_departure(60, route="1", long_name="Vana-Pääsküla (kuni 20.09)"),
            make_departure(120, route="1", long_name="Vana-Pääsküla (al 21.09)"),
        ]
    )

    data = await coordinator._async_update_data()

    assert len(data) == 2


async def test_empty_route_filter_shows_every_route(hass: HomeAssistant) -> None:
    """An empty line filter is not a filter, so nothing is discarded."""
    coordinator, api = build_coordinator(hass, routes=[])
    api.async_get_departures = AsyncMock(
        return_value=[make_departure(60, route=str(i)) for i in range(NUM_DEPARTURES)]
    )

    data = await coordinator._async_update_data()

    assert len(data) == NUM_DEPARTURES
    # Unfiltered, so the smallest request is still enough.
    api.async_get_departures.assert_awaited_once_with("estonia:1292", NUM_DEPARTURES)


async def test_route_filter_grows_the_request(hass: HomeAssistant) -> None:
    """A rarely served line makes the request grow until enough are found."""
    coordinator, api = build_coordinator(hass, routes=["8"])

    def departures(_stop_id: str, count: int):
        # One in ten departures is the wanted line, so the first batch of ten
        # yields a single match and the request has to grow.
        return [
            make_departure(60 * i, route="8" if i % 10 == 0 else "5")
            for i in range(count)
        ]

    api.async_get_departures = AsyncMock(side_effect=departures)

    data = await coordinator._async_update_data()

    assert len(data) == NUM_DEPARTURES
    assert {departure.route_short_name for departure in data} == {"8"}
    requested = [call.args[1] for call in api.async_get_departures.await_args_list]
    assert requested == [NUM_DEPARTURES, 60, 150]
