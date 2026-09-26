"""Tests for the journey planning coordinator."""

from __future__ import annotations

import logging
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
    CONF_MODES,
    CONF_ORIGIN_ENTITY,
    CONF_ROUTES,
    CONF_WALK_SPEED,
    DEFAULT_BIKE_OPTIMIZE,
    DEFAULT_BIKE_SPEED,
    DEFAULT_WALK_SPEED,
    DOMAIN,
    NUM_ITINERARIES,
)
from custom_components.peatus.coordinator import PeatusJourneyCoordinator
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import make_itinerary, make_leg

ORIGIN = "person.romi"
DESTINATION = "zone.work"


def place(hass: HomeAssistant, entity_id: str, lat: float, lon: float) -> None:
    """Register an entity that publishes coordinates."""
    hass.states.async_set(entity_id, "home", {"latitude": lat, "longitude": lon})


def build_coordinator(
    hass: HomeAssistant,
    *,
    modes: list[str] | None = None,
    routes: list[str] | None = None,
    options: dict | None = None,
) -> tuple[PeatusJourneyCoordinator, AsyncMock]:
    """Create a journey coordinator with a mocked API client."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BOARD: BOARD_JOURNEY,
            CONF_ORIGIN_ENTITY: ORIGIN,
            CONF_DESTINATION_ENTITY: DESTINATION,
        },
        options={
            CONF_MODES: modes or ["bus", "trolleybus", "tram", "rail", "ferry"],
            CONF_ROUTES: routes or [],
            CONF_SCAN_INTERVAL: 5,
            **(options or {}),
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.peatus.coordinator.PeatusApi") as mock:
        coordinator = PeatusJourneyCoordinator(hass, entry)
    api = mock.return_value
    api.async_plan = AsyncMock(return_value=[make_itinerary()])
    api.async_plan_walk = AsyncMock(return_value=make_itinerary())
    api.async_plan_bicycle = AsyncMock(return_value=make_itinerary())
    return coordinator, api


async def test_missing_origin_entity_fails_loudly(hass: HomeAssistant) -> None:
    """An origin that does not exist is reported, not sent to the feed.

    ``find_coordinates`` hands back the string it was given for an unknown
    entity, so without a check the planner would be asked to route from the
    literal text "person.romi".
    """
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert ORIGIN in str(err.value)
    assert "does not exist" in str(err.value)
    api.async_plan.assert_not_awaited()


async def test_entity_without_a_location_fails_loudly(hass: HomeAssistant) -> None:
    """A tracker that only knows it is away cannot be planned from."""
    hass.states.async_set(ORIGIN, "not_home")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert "not_home" in str(err.value)
    api.async_plan.assert_not_awaited()


async def test_unavailable_entity_fails_loudly(hass: HomeAssistant) -> None:
    """An unavailable tracker is named along with its state."""
    hass.states.async_set(ORIGIN, "unavailable")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, _ = build_coordinator(hass)

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert "unavailable" in str(err.value)


async def test_impossible_coordinates_fail(hass: HomeAssistant) -> None:
    """Coordinates off the planet are refused rather than planned from."""
    place(hass, ORIGIN, 999.0, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, _ = build_coordinator(hass)

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert "impossible" in str(err.value)


async def test_a_person_inside_a_zone_resolves_through_it(hass: HomeAssistant) -> None:
    """A person with no coordinates of their own is placed by their zone."""
    hass.states.async_set("zone.home", "1", {"latitude": 59.35, "longitude": 24.63})
    hass.states.async_set(ORIGIN, "home")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)

    await coordinator._async_update_data()

    assert api.async_plan.await_args.args[0] == (59.35, 24.63)


async def test_coordinates_are_rounded_before_planning(hass: HomeAssistant) -> None:
    """Coordinates are rounded, so a phone sitting still does not re-plan.

    Without this a couple of metres of GPS drift would change the walk, and
    with it every sensor's state, on every single poll.
    """
    place(hass, ORIGIN, 59.35281234, 24.63829876)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)

    await coordinator._async_update_data()
    assert api.async_plan.await_args.args[0] == (59.3528, 24.6383)

    # A metre of drift resolves to the same request.
    place(hass, ORIGIN, 59.35281299, 24.63829801)
    await coordinator._async_update_data()
    assert api.async_plan.await_args.args[0] == (59.3528, 24.6383)


async def test_walk_and_ride_are_planned_once(hass: HomeAssistant) -> None:
    """The fixed plans depend only on the two places, so they are cached."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert api.async_plan.await_count == 2
    assert api.async_plan_walk.await_count == 1
    assert api.async_plan_bicycle.await_count == 1


async def test_walk_and_ride_are_replanned_after_moving(hass: HomeAssistant) -> None:
    """Moving far enough to change the rounded position re-plans them."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)

    await coordinator._async_update_data()
    place(hass, ORIGIN, 59.41, 24.80)
    await coordinator._async_update_data()

    assert api.async_plan_walk.await_count == 2


async def test_modes_and_preferences_reach_the_planner(hass: HomeAssistant) -> None:
    """The configured modes and tuning are what the plan is made with."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(
        hass,
        modes=["rail"],
        options={
            CONF_WALK_SPEED: 6.0,
            CONF_BIKE_SPEED: 22.0,
            CONF_BIKE_OPTIMIZE: "flat",
        },
    )

    await coordinator._async_update_data()

    assert api.async_plan.await_args.args[3] == ["rail"]
    plan_options = api.async_plan.await_args.args[4]
    assert plan_options.walk_speed_kmh == 6.0
    assert plan_options.bike_speed_kmh == 22.0
    assert plan_options.bike_optimize == "flat"


async def test_preferences_fall_back_to_the_defaults(hass: HomeAssistant) -> None:
    """A board configured before the tuning existed still plans."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)

    await coordinator._async_update_data()

    plan_options = api.async_plan.await_args.args[4]
    assert plan_options.walk_speed_kmh == DEFAULT_WALK_SPEED
    assert plan_options.bike_speed_kmh == DEFAULT_BIKE_SPEED
    assert plan_options.bike_optimize == DEFAULT_BIKE_OPTIMIZE


async def test_route_filter_keeps_only_the_chosen_lines(hass: HomeAssistant) -> None:
    """A journey on a line that was not chosen is not offered."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, routes=["18"])
    api.async_plan = AsyncMock(
        return_value=[
            make_itinerary([make_leg(route="18")]),
            make_itinerary([make_leg(route="34")]),
        ]
    )

    data = await coordinator._async_update_data()

    assert [itinerary.routes for itinerary in data.itineraries] == [["18"]]


async def test_route_filter_requires_every_ride_to_match(hass: HomeAssistant) -> None:
    """A journey is one offer, so a change onto an unwanted line rules it out."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, routes=["18"])
    api.async_plan = AsyncMock(
        return_value=[
            make_itinerary(
                [
                    make_leg(route="18", start=0, duration=600),
                    make_leg(route="34", start=600, duration=600),
                ]
            )
        ]
    )

    data = await coordinator._async_update_data()

    assert data.itineraries == []


async def test_walking_and_waiting_are_never_filtered(hass: HomeAssistant) -> None:
    """Only rides are matched against the filters."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, routes=["18"])
    api.async_plan = AsyncMock(
        return_value=[
            make_itinerary(
                [
                    make_leg(mode="walk", start=0, duration=300, route=None),
                    make_leg(mode="wait", start=300, duration=120, route=None),
                    make_leg(route="18", start=420, duration=600),
                ]
            )
        ]
    )

    data = await coordinator._async_update_data()

    assert len(data.itineraries) == 1


async def test_an_emptied_board_is_not_a_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Filtering everything away leaves an empty board and a warning."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, routes=["99"])
    api.async_plan = AsyncMock(return_value=[make_itinerary([make_leg(route="18")])])

    with caplog.at_level(logging.WARNING):
        data = await coordinator._async_update_data()

    assert data.itineraries == []
    assert "until the filter is widened" in caplog.text


async def test_the_empty_warning_is_logged_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A board that stays empty does not repeat itself on every poll."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, routes=["99"])
    api.async_plan = AsyncMock(return_value=[make_itinerary([make_leg(route="18")])])

    with caplog.at_level(logging.WARNING):
        await coordinator._async_update_data()
        await coordinator._async_update_data()

    assert caplog.text.count("until the filter is widened") == 1


async def test_an_unroutable_pair_is_named_differently(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The feed reports "no route" and "filtered out" alike, so they are split."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)
    api.async_plan = AsyncMock(return_value=[])

    with caplog.at_level(logging.WARNING):
        data = await coordinator._async_update_data()

    assert data.itineraries == []
    assert "No journey was found" in caplog.text


async def test_unfiltered_asks_for_exactly_what_is_shown(hass: HomeAssistant) -> None:
    """Without filters the planner is asked for one itinerary per sensor."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)
    api.async_plan = AsyncMock(
        return_value=[make_itinerary() for _ in range(NUM_ITINERARIES)]
    )

    await coordinator._async_update_data()

    assert api.async_plan.await_count == 1
    assert api.async_plan.await_args.args[2] == NUM_ITINERARIES


async def test_the_request_grows_when_filtering_discards(hass: HomeAssistant) -> None:
    """A line filter throws plans away, so it starts by asking for more."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, routes=["18"])
    api.async_plan = AsyncMock(
        return_value=[make_itinerary([make_leg(route="34")]) for _ in range(15)]
    )

    await coordinator._async_update_data()

    assert [call.args[2] for call in api.async_plan.await_args_list] == [
        NUM_ITINERARIES,
        15,
    ]


async def test_api_errors_become_update_failures(hass: HomeAssistant) -> None:
    """A failing feed fails the update rather than emptying the board."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)
    api.async_plan = AsyncMock(side_effect=PeatusApiError("boom"))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
