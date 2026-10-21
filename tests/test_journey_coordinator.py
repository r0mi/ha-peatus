"""Tests for the journey planning coordinator."""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.peatus.api import PeatusApiError, Place
from custom_components.peatus.const import (
    BOARD_JOURNEY,
    CONF_BIKE_OPTIMIZE,
    CONF_BIKE_SPEED,
    CONF_BOARD,
    CONF_DESTINATION_ENTITY,
    CONF_MAX_WALK_DISTANCE,
    CONF_MODES,
    CONF_ORIGIN_ENTITY,
    CONF_ROUTES,
    CONF_WALK_SPEED,
    DEFAULT_BIKE_OPTIMIZE,
    DEFAULT_BIKE_SPEED,
    DEFAULT_MAX_WALK_DISTANCE,
    DEFAULT_WALK_SPEED,
    DOMAIN,
    NUM_ITINERARIES,
    TIME_ARRIVE,
    TIME_DEPART,
    TIME_NOW,
)
from custom_components.peatus.coordinator import PeatusJourneyCoordinator
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .conftest import BASE, make_itinerary, make_leg

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
    origin: str = ORIGIN,
) -> tuple[PeatusJourneyCoordinator, AsyncMock]:
    """Create a journey coordinator with a mocked API client."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BOARD: BOARD_JOURNEY,
            CONF_ORIGIN_ENTITY: origin,
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
    api.async_geocode_place = AsyncMock(return_value=None)
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


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("59.3528,24.6382", (59.3528, 24.6382)),
        # A pair typed into a helper by hand, which is how a person writes one.
        # find_coordinates never emits the space itself, so only a human ever
        # hands this shape over — and until it was accepted, doing so failed.
        ("59.43443134336628, 24.610919208336178", (59.4344, 24.6109)),
        ("59.3528 , 24.6382", (59.3528, 24.6382)),
        ("-59.3528,-24.6382", (-59.3528, -24.6382)),
    ],
)
async def test_a_typed_coordinate_pair_is_an_end(
    hass: HomeAssistant, written: str, expected: tuple[float, float]
) -> None:
    """An entity holding a lat/lon pair can be planned from, however spaced."""
    hass.states.async_set("input_text.somewhere", written)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.somewhere")

    await coordinator._async_update_data()

    assert api.async_plan.await_args.args[0] == expected


async def test_a_typed_address_is_geocoded(hass: HomeAssistant) -> None:
    """Text that names no entity or zone is looked up as an address."""
    hass.states.async_set("input_text.from", "Viru väljak 4, Tallinn")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.from")
    api.async_geocode_place = AsyncMock(
        return_value=Place(label="Viru Väljak 4", lat=59.43588123, lon=24.75432199)
    )

    data = await coordinator._async_update_data()

    # Looked up near home, and rounded like any other end.
    api.async_geocode_place.assert_awaited_once_with(
        "Viru väljak 4, Tallinn", (hass.config.latitude, hass.config.longitude)
    )
    assert api.async_plan.await_args.args[0] == (59.4359, 24.7543)
    # The match is published, so a loose one can be seen rather than trusted.
    assert data.origin_place == "Viru Väljak 4"
    assert data.destination_place is None


async def test_an_address_is_geocoded_once(hass: HomeAssistant) -> None:
    """An address does not move, so polling does not look it up again."""
    hass.states.async_set("input_text.from", "Kaubamaja")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.from")
    api.async_geocode_place = AsyncMock(
        return_value=Place(label="Kaubamaja", lat=59.43, lon=24.75)
    )

    await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert api.async_geocode_place.await_count == 1

    # A new search is a new lookup.
    hass.states.async_set("input_text.from", "Balti jaam")
    await coordinator._async_update_data()
    assert api.async_geocode_place.await_count == 2


async def test_an_address_matching_nothing_fails_loudly(hass: HomeAssistant) -> None:
    """A typo is reported by name rather than planned from somewhere else."""
    hass.states.async_set("input_text.from", "qwxzqq")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.from")

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert "qwxzqq" in str(err.value)
    api.async_plan.assert_not_awaited()


async def test_a_failing_geocoder_fails_the_update(hass: HomeAssistant) -> None:
    """A geocoder outage fails the update and is not remembered as no match."""
    hass.states.async_set("input_text.from", "Kaubamaja")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.from")
    api.async_geocode_place = AsyncMock(side_effect=PeatusApiError("down"))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    api.async_geocode_place = AsyncMock(
        return_value=Place(label="Kaubamaja", lat=59.43, lon=24.75)
    )
    await coordinator._async_update_data()
    api.async_plan.assert_awaited()


@pytest.mark.parametrize("state", ["unknown", "not_home"])
async def test_a_placeless_state_is_never_geocoded(
    hass: HomeAssistant, state: str
) -> None:
    """A tracker's "not_home" is not an address, whatever the geocoder thinks."""
    hass.states.async_set("input_text.from", state)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.from")

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert "reports no location" in str(err.value)
    api.async_geocode_place.assert_not_awaited()


async def test_a_misspelt_entity_id_is_not_geocoded(hass: HomeAssistant) -> None:
    """Text shaped like an entity ID was meant as one, so it is not looked up."""
    hass.states.async_set("input_text.from", "zone.wrok")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.from")

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()

    assert "zone.wrok" in str(err.value)
    assert "does not exist" in str(err.value)
    api.async_geocode_place.assert_not_awaited()


@pytest.mark.parametrize("state", ["", "   "])
async def test_a_blank_end_leaves_the_board_empty(
    hass: HomeAssistant, state: str
) -> None:
    """A search box nobody has filled in is an empty board, not a failure."""
    hass.states.async_set("input_text.from", state)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.from")

    data = await coordinator._async_update_data()

    assert data.itineraries == []
    assert data.walk is None
    assert data.bicycle is None
    api.async_plan.assert_not_awaited()
    api.async_geocode_place.assert_not_awaited()


async def test_a_zone_can_be_typed_by_name(hass: HomeAssistant) -> None:
    """A zone's friendly name is found by Home Assistant, not the geocoder."""
    hass.states.async_set(
        "zone.office",
        "0",
        {"friendly_name": "Office", "latitude": 59.4, "longitude": 24.7},
    )
    hass.states.async_set("input_text.from", "Office")
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass, origin="input_text.from")

    await coordinator._async_update_data()

    assert api.async_plan.await_args.args[0] == (59.4, 24.7)
    api.async_geocode_place.assert_not_awaited()


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
            CONF_MAX_WALK_DISTANCE: 7500,
        },
    )

    await coordinator._async_update_data()

    assert api.async_plan.await_args.args[3] == ["rail"]
    plan_options = api.async_plan.await_args.args[4]
    assert plan_options.walk_speed_kmh == 6.0
    assert plan_options.bike_speed_kmh == 22.0
    assert plan_options.bike_optimize == "flat"
    assert plan_options.max_walk_distance_m == 7500


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
    assert plan_options.max_walk_distance_m == DEFAULT_MAX_WALK_DISTANCE


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


async def test_a_chosen_time_reaches_the_planner(hass: HomeAssistant) -> None:
    """Leaving at a later time plans for that time rather than now."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)
    when = dt_util.utcnow() + timedelta(hours=2)
    coordinator.time_mode = TIME_DEPART
    coordinator.chosen_time = when

    data = await coordinator._async_update_data()

    plan_options = api.async_plan.await_args.args[4]
    assert plan_options.when == when
    assert plan_options.arrive_by is False
    assert data.time_mode == TIME_DEPART
    assert data.requested_time == when


@pytest.mark.parametrize(
    ("time_mode", "offset"),
    [
        # "Now" ignores whatever time is still set from before.
        (TIME_NOW, timedelta(hours=2)),
        # A time already past would only offer journeys that have left.
        (TIME_ARRIVE, timedelta(minutes=-5)),
        # A mode picked before any time is set.
        (TIME_DEPART, None),
    ],
)
async def test_the_board_falls_back_to_now(
    hass: HomeAssistant, time_mode: str, offset: timedelta | None
) -> None:
    """Without a usable time the board plans from now, and says so."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)
    coordinator.time_mode = time_mode
    coordinator.chosen_time = dt_util.utcnow() + offset if offset else None

    data = await coordinator._async_update_data()

    assert api.async_plan.await_args.args[4].when is None
    assert data.time_mode == TIME_NOW
    assert data.requested_time is None


async def test_arriving_keeps_the_plans_nearest_the_deadline(
    hass: HomeAssistant,
) -> None:
    """A larger request only adds earlier arrivals, so those are the ones cut."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)
    coordinator.time_mode = TIME_ARRIVE
    coordinator.chosen_time = dt_util.utcnow() + timedelta(hours=2)
    # Latest arrival first, the way the feed answers an arrive-by plan.
    api.async_plan = AsyncMock(
        return_value=[
            make_itinerary([make_leg(start=600 * (10 - n))])
            for n in range(NUM_ITINERARIES + 2)
        ]
    )

    data = await coordinator._async_update_data()

    starts = [itinerary.start_timestamp - BASE for itinerary in data.itineraries]
    assert starts == [600 * n for n in range(6, 11)]
    assert api.async_plan.await_args.args[4].arrive_by is True


async def test_walk_and_ride_move_to_the_chosen_time(hass: HomeAssistant) -> None:
    """The fixed plans are moved to the time rather than planned again."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, api = build_coordinator(hass)
    when = (dt_util.utcnow() + timedelta(hours=2)).replace(microsecond=0)
    coordinator.time_mode = TIME_ARRIVE
    coordinator.chosen_time = when

    data = await coordinator._async_update_data()

    walk = data.walk
    assert walk.end_timestamp == int(when.timestamp())
    assert walk.duration == make_itinerary().duration
    assert walk.legs[0].start_timestamp == walk.start_timestamp
    assert walk.legs[-1].end_timestamp == walk.end_timestamp
    # Moved, not re-planned, and the cached original is left as it was.
    api.async_plan_walk.assert_awaited_once()
    coordinator.time_mode = TIME_NOW
    data = await coordinator._async_update_data()
    assert data.walk.start_timestamp == BASE


async def test_only_a_change_that_matters_replans(hass: HomeAssistant) -> None:
    """Picking a mode with no time set yet costs no request."""
    place(hass, ORIGIN, 59.35, 24.63)
    place(hass, DESTINATION, 59.39, 24.72)
    coordinator, _ = build_coordinator(hass)
    coordinator.data = await coordinator._async_update_data()

    with patch.object(coordinator, "async_request_refresh") as refresh:
        coordinator.time_mode = TIME_DEPART
        await coordinator.async_replan_if_moved()
        refresh.assert_not_awaited()

        coordinator.chosen_time = dt_util.utcnow() + timedelta(hours=1)
        await coordinator.async_replan_if_moved()
        refresh.assert_awaited_once()
