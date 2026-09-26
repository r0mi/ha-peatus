"""Tests for the Peatus.ee config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.peatus.api import PeatusApiError, PeatusStopNotFoundError
from custom_components.peatus.config_flow import _estonian_sort_key, _stop_sort_key
from custom_components.peatus.const import (
    BOARD_JOURNEY,
    BOARD_STOP,
    CONF_BIKE_OPTIMIZE,
    CONF_BIKE_SPEED,
    CONF_BOARD,
    CONF_DESTINATION_ENTITY,
    CONF_DESTINATION_ID,
    CONF_DESTINATION_NAME,
    CONF_MAX_WALK_DISTANCE,
    CONF_MODES,
    CONF_ORIGIN_ENTITY,
    CONF_ORIGIN_NAME,
    CONF_ROUTES,
    CONF_STOP_DESC,
    CONF_STOP_ID,
    CONF_STOP_MODE,
    CONF_WALK_SPEED,
    DEFAULT_BIKE_OPTIMIZE,
    DEFAULT_BIKE_SPEED,
    DEFAULT_JOURNEY_SCAN_INTERVAL,
    DEFAULT_MAX_WALK_DISTANCE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WALK_SPEED,
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
    # No lines picked, so the filter stays empty and every route is shown.
    assert result["options"] == {
        CONF_MODES: ["tram"],
        CONF_ROUTES: [],
        CONF_SCAN_INTERVAL: 2,
    }


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


def test_estonian_sort_key_follows_the_estonian_alphabet() -> None:
    """Text sorts by the Estonian alphabet, not by code point."""
    words = ["Zoo", "Šokolaad", "Taevas", "Õismäe", "Ära", "Öö", "Ülemiste", "Sadam"]

    assert sorted(words, key=_estonian_sort_key) == [
        "Sadam",
        "Šokolaad",
        "Zoo",
        "Taevas",
        # The vowels come after w, in this order, rather than after "ä" as
        # their code points would have it.
        "Õismäe",
        "Ära",
        "Öö",
        "Ülemiste",
    ]


def test_stop_sort_key_groups_platforms_by_place_then_mode() -> None:
    """Platforms of one stop are offered together, Tallinn first.

    A search for a name as common as "Järve" turns up two dozen stops, so the
    four platforms of the Tallinn one have to sit together, with the trains
    beside each other rather than split by the bus platforms between them.
    """
    tallinn = "Tallinna linn, Kristiine"
    stops = [
        make_stop(
            "estonia:1",
            "Järve",
            code="6700178-1",
            vehicle_mode="bus",
            locality="Pärnumaa, Pärnu linn",
        ),
        make_stop(
            "estonia:2", "Järve", code="06803-1", vehicle_mode="bus", locality=tallinn
        ),
        make_stop(
            "estonia:3",
            "Järve",
            code="6500093-1",
            vehicle_mode="bus",
            locality="Põlvamaa, Põlva vald",
        ),
        make_stop(
            "estonia:4", "Järve", code="06805-1", vehicle_mode="rail", locality=tallinn
        ),
        make_stop(
            "estonia:5",
            "Järve tee",
            code="4991022",
            vehicle_mode="bus",
            locality="Jõgevamaa, Mustvee vald",
        ),
        make_stop(
            "estonia:6", "Järve", code="06804-1", vehicle_mode="rail", locality=tallinn
        ),
        make_stop(
            "estonia:7", "Järve", code="06801-1", vehicle_mode="bus", locality=tallinn
        ),
    ]

    assert [stop.gtfs_id for stop in sorted(stops, key=_stop_sort_key)] == [
        # Tallinn leads, trains before buses, then platform code.
        "estonia:6",
        "estonia:4",
        "estonia:7",
        "estonia:2",
        # Then the rest of the country, in Estonian alphabetical order:
        # Põlvamaa before Pärnumaa, which a code point sort reverses.
        "estonia:3",
        "estonia:1",
        # A different stop name is a separate group entirely.
        "estonia:5",
    ]


def test_stop_sort_key_tolerates_missing_details() -> None:
    """Stops the feed describes only partly still sort without failing."""
    stops = [
        make_stop("estonia:1", "Järve", code=None, vehicle_mode=None, locality=None),
        make_stop(
            "estonia:2",
            "Järve",
            code="06805-1",
            vehicle_mode="rail",
            locality="Tallinna linn, Kristiine",
        ),
    ]

    # A stop of unknown mode is listed after the modes the feed does report.
    assert [stop.gtfs_id for stop in sorted(stops, key=_stop_sort_key)] == [
        "estonia:2",
        "estonia:1",
    ]


# --- Journey boards ---------------------------------------------------------


JOURNEY_ORIGIN = "person.romi"
JOURNEY_DESTINATION = "zone.work"


def place_both_ends(hass: HomeAssistant) -> None:
    """Register the two places a journey runs between."""
    hass.states.async_set(
        JOURNEY_ORIGIN, "home", {"friendly_name": "Romi", "latitude": 59.35}
    )
    hass.states.async_set(
        JOURNEY_DESTINATION, "1", {"friendly_name": "Work", "latitude": 59.39}
    )


async def _add_journey(hass: HomeAssistant, **settings):
    """Run the whole journey flow and return its result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "journey"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ORIGIN_ENTITY: JOURNEY_ORIGIN,
            CONF_DESTINATION_ENTITY: JOURNEY_DESTINATION,
        },
    )
    if result["type"] is not FlowResultType.FORM or result["step_id"] != (
        "journey_settings"
    ):
        return result
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MODES: ["bus"],
            CONF_ROUTES: [],
            CONF_SCAN_INTERVAL: 5,
            CONF_WALK_SPEED: 4.8,
            CONF_BIKE_SPEED: 18.0,
            CONF_BIKE_OPTIMIZE: "quick",
            CONF_MAX_WALK_DISTANCE: 5000,
            **settings,
        },
    )


async def test_menu_offers_a_journey(hass: HomeAssistant) -> None:
    """A board can watch a stop or plan a journey."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.MENU
    assert "journey" in result["menu_options"]


async def test_journey_flow_creates_an_entry(hass: HomeAssistant, no_setup) -> None:
    """A journey board records both places and how it plans between them."""
    place_both_ends(hass)

    result = await _add_journey(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Romi → Work"
    assert result["data"] == {
        CONF_BOARD: BOARD_JOURNEY,
        CONF_ORIGIN_ENTITY: JOURNEY_ORIGIN,
        # The names are snapshotted, so renaming the tracker later cannot move
        # the entity IDs of a board that already exists.
        CONF_ORIGIN_NAME: "Romi",
        CONF_DESTINATION_ENTITY: JOURNEY_DESTINATION,
        CONF_DESTINATION_NAME: "Work",
    }
    assert result["options"] == {
        CONF_MODES: ["bus"],
        CONF_ROUTES: [],
        CONF_SCAN_INTERVAL: 5,
        CONF_WALK_SPEED: 4.8,
        CONF_BIKE_SPEED: 18.0,
        CONF_BIKE_OPTIMIZE: "quick",
        CONF_MAX_WALK_DISTANCE: 5000,
    }


async def test_journey_ends_must_differ(hass: HomeAssistant) -> None:
    """Planning from a place to itself is not a journey."""
    place_both_ends(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "journey"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ORIGIN_ENTITY: JOURNEY_ORIGIN,
            CONF_DESTINATION_ENTITY: JOURNEY_ORIGIN,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_DESTINATION_ENTITY: "same_place"}


async def test_duplicate_journey_aborts(hass: HomeAssistant, no_setup) -> None:
    """The same pair of places cannot be set up twice."""
    place_both_ends(hass)
    await _add_journey(hass)

    result = await _add_journey(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured_journey"


async def test_a_journey_does_not_collide_with_a_stop_board(
    hass: HomeAssistant, api, no_setup
) -> None:
    """Journey IDs are namespaced, so they cannot clash with a GTFS one."""
    place_both_ends(hass)
    await _add_board(hass, api)

    result = await _add_journey(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_stop_board_records_its_kind(
    hass: HomeAssistant, api, no_setup
) -> None:
    """New stop entries say so, even though the absence of a kind means stop."""
    result = await _add_board(hass, api)

    assert result["data"][CONF_BOARD] == BOARD_STOP


async def test_journey_settings_default_to_the_feeds_own_speeds(
    hass: HomeAssistant,
) -> None:
    """The form opens on the planner's defaults, restated in km/h."""
    place_both_ends(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "journey"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ORIGIN_ENTITY: JOURNEY_ORIGIN,
            CONF_DESTINATION_ENTITY: JOURNEY_DESTINATION,
        },
    )

    assert result["step_id"] == "journey_settings"
    schema = result["data_schema"].schema
    defaults = {str(key): key.default() for key in schema}
    assert defaults[CONF_WALK_SPEED] == DEFAULT_WALK_SPEED
    assert defaults[CONF_BIKE_SPEED] == DEFAULT_BIKE_SPEED
    assert defaults[CONF_BIKE_OPTIMIZE] == DEFAULT_BIKE_OPTIMIZE
    assert defaults[CONF_MAX_WALK_DISTANCE] == DEFAULT_MAX_WALK_DISTANCE
    assert defaults[CONF_SCAN_INTERVAL] == DEFAULT_JOURNEY_SCAN_INTERVAL


async def test_journey_ends_accept_any_entity_that_names_a_place(
    hass: HomeAssistant,
) -> None:
    """A destination can be held by a sensor, not just a tracker or a zone.

    An end is resolved by state rather than by domain, and a template sensor
    holding a zone's entity ID is the usual way to build a destination that
    changes with the time of day.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "journey"}
    )

    offered = result["data_schema"].schema[CONF_DESTINATION_ENTITY].config
    assert set(offered["domain"]) >= {
        "person",
        "device_tracker",
        "zone",
        "sensor",
    }
