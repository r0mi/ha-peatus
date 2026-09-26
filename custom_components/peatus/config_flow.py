"""Config and options flow for the Peatus.ee integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .api import (
    PeatusApi,
    PeatusApiError,
    PeatusStopNotFoundError,
    PlanOptions,
    Stop,
    route_sort_key,
)
from .const import (
    BIKE_OPTIMIZE,
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
    CONF_STOP_CODE,
    CONF_STOP_DESC,
    CONF_STOP_ID,
    CONF_STOP_MODE,
    CONF_STOP_NAME,
    CONF_WALK_SPEED,
    DEFAULT_BIKE_OPTIMIZE,
    DEFAULT_BIKE_SPEED,
    DEFAULT_JOURNEY_SCAN_INTERVAL,
    DEFAULT_MAX_WALK_DISTANCE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WALK_SPEED,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MAX_SPEED,
    MAX_WALK_DISTANCE,
    MIN_SCAN_INTERVAL,
    MIN_SPEED,
    MIN_WALK_DISTANCE,
    MODE_BUS,
    MODE_FERRY,
    MODE_RAIL,
    MODE_TRAM,
    MODE_TROLLEYBUS,
    SUPPORTED_MODES,
)
from .coordinator import PeatusConfigEntry, board_type

_LOGGER = logging.getLogger(__name__)

CONF_STOP_PICK = "stop"

SEARCH_SCHEMA = vol.Schema({vol.Required(CONF_NAME): TextSelector()})

GTFS_ID_SCHEMA = vol.Schema(
    {vol.Required(CONF_STOP_ID): TextSelector(TextSelectorConfig(autocomplete="off"))}
)

MODES_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(SUPPORTED_MODES),
        multiple=True,
        mode=SelectSelectorMode.LIST,
        translation_key="modes",
    )
)

INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL,
        max=MAX_SCAN_INTERVAL,
        step=1,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="min",
    )
)


def _routes_selector(routes: list[str]) -> SelectSelector:
    """Return a selector listing the route numbers a stop is served by."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(routes),
            multiple=True,
            mode=SelectSelectorMode.DROPDOWN,
            # A line the timetable has dropped must stay selectable for as long
            # as it is saved in the entry, so the filter is not silently
            # cleared, and a line the stop has just gained can be typed in
            # before the feed's own route list catches up.
            custom_value=True,
            # The options arrive in timetable order (1, 2, 10, 18V, 104A), which
            # sorting them as text in the frontend would undo.
            sort=False,
        )
    )


def _settings_schema(
    modes: list[str],
    interval: int,
    routes: list[str],
    selected_routes: list[str],
) -> vol.Schema:
    """Return the schema for the mode / route / interval step."""
    return vol.Schema(
        {
            vol.Required(CONF_MODES, default=modes): MODES_SELECTOR,
            vol.Optional(CONF_ROUTES, default=list(selected_routes)): _routes_selector(
                routes
            ),
            vol.Required(CONF_SCAN_INTERVAL, default=interval): INTERVAL_SELECTOR,
        }
    )


#: Both ends of a journey are places rather than stops, which is what gives the
#: plan a walk at each end.
#:
#: Sensors and the text and select helpers are offered alongside the obvious
#: three because an end is resolved by state, not by domain: anything whose
#: state names a place works, including an entity holding another entity's ID,
#: which is how a destination that changes with the time of day is usually
#: built. A router-based tracker, meanwhile, cannot be excluded here however
#: much one would like to — publishing no coordinates is a property of its
#: state rather than of its domain, so the coordinator is what says so, by
#: name, when it cannot locate one.
PLACE_SELECTOR = EntitySelector(
    EntitySelectorConfig(
        domain=[
            "person",
            "device_tracker",
            "zone",
            "sensor",
            "input_text",
            "input_select",
        ]
    )
)

JOURNEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ORIGIN_ENTITY): PLACE_SELECTOR,
        vol.Required(CONF_DESTINATION_ENTITY): PLACE_SELECTOR,
    }
)

#: Speeds are asked for in km/h because that is what people think in. They
#: become the metres per second the feed wants only when the request is built.
SPEED_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SPEED,
        max=MAX_SPEED,
        step=0.1,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="km/h",
    )
)

#: A limit in metres rather than a preference, so it is asked for as a plain
#: distance: anything needing a longer walk to or from a stop is thrown away
#: before the planner ranks what is left.
WALK_DISTANCE_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_WALK_DISTANCE,
        max=MAX_WALK_DISTANCE,
        step=100,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="m",
    )
)

BIKE_OPTIMIZE_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=list(BIKE_OPTIMIZE),
        mode=SelectSelectorMode.DROPDOWN,
        translation_key="bike_optimize",
        sort=False,
    )
)


def _journey_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return the options a journey board stores, from its settings form."""
    return {
        CONF_MODES: user_input[CONF_MODES],
        CONF_ROUTES: user_input.get(CONF_ROUTES) or [],
        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
        CONF_WALK_SPEED: float(user_input[CONF_WALK_SPEED]),
        CONF_BIKE_SPEED: float(user_input[CONF_BIKE_SPEED]),
        CONF_BIKE_OPTIMIZE: user_input[CONF_BIKE_OPTIMIZE],
        CONF_MAX_WALK_DISTANCE: int(user_input[CONF_MAX_WALK_DISTANCE]),
    }


def _journey_settings_schema(
    modes: list[str],
    interval: int,
    routes: list[str],
    selected_routes: list[str],
    plan: PlanOptions,
) -> vol.Schema:
    """Return the schema for a journey board's settings.

    The first three fields are the ones a stop board also has; the rest tune
    the planning itself, which only a journey does. Those three travel together
    as one object, which is also how the API client takes them.
    """
    return vol.Schema(
        {
            vol.Required(CONF_MODES, default=modes): MODES_SELECTOR,
            vol.Optional(CONF_ROUTES, default=list(selected_routes)): _routes_selector(
                routes
            ),
            vol.Required(CONF_SCAN_INTERVAL, default=interval): INTERVAL_SELECTOR,
            vol.Required(CONF_WALK_SPEED, default=plan.walk_speed_kmh): SPEED_SELECTOR,
            vol.Required(CONF_BIKE_SPEED, default=plan.bike_speed_kmh): SPEED_SELECTOR,
            vol.Required(
                CONF_BIKE_OPTIMIZE, default=plan.bike_optimize
            ): BIKE_OPTIMIZE_SELECTOR,
            vol.Required(
                CONF_MAX_WALK_DISTANCE, default=plan.max_walk_distance_m
            ): WALK_DISTANCE_SELECTOR,
        }
    )


#: The Estonian alphabet past "r", where it stops matching code point order:
#: ``š`` and ``ž`` sort beside their plain letters, and the vowels come after
#: ``w`` rather than in the Latin-1 order Python would put them in. Each letter
#: maps to a token that sorts the right way as plain text.
_ESTONIAN_COLLATION = str.maketrans(
    {
        "s": "s1",
        "š": "s2",
        "z": "s3",
        "ž": "s4",
        "õ": "w1",
        "ä": "w2",
        "ö": "w3",
        "ü": "w4",
    }
)

#: Modes in the order they are offered within one place, most distinctive
#: first: a search that turns up a train and a bus stop of the same name is
#: nearly always after the train.
_MODE_ORDER = (MODE_RAIL, MODE_TRAM, MODE_TROLLEYBUS, MODE_BUS, MODE_FERRY)

#: Tallinn's own localities are spelled "Tallinna linn, <district>".
_TALLINN = "tallinna linn"


def _estonian_sort_key(text: str) -> str:
    """Return a key ordering text by the Estonian alphabet."""
    return text.casefold().translate(_ESTONIAN_COLLATION)


def _stop_sort_key(stop: Stop) -> tuple:
    """Return the order stops are offered in for picking.

    Stop names repeat across the country, so the platforms of one stop are kept
    together: by name, then by place with Tallinn first, then by mode so an
    interchange's trains sit beside each other, and finally by platform code.
    """
    locality = stop.locality or ""
    try:
        mode = _MODE_ORDER.index(stop.vehicle_mode)
    except ValueError:
        # A mode the feed does not report, or one this integration has no name
        # for, is listed after the modes it does.
        mode = len(_MODE_ORDER)
    return (
        _estonian_sort_key(stop.name),
        0 if locality.casefold().startswith(_TALLINN) else 1,
        _estonian_sort_key(locality),
        mode,
        stop.code or "",
    )


def _stop_picker(stops: list[Stop]) -> vol.Schema:
    """Return a schema letting the user pick one of the matched stops."""
    return vol.Schema(
        {
            vol.Required(CONF_STOP_PICK): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=stop.gtfs_id, label=stop.label)
                        for stop in stops
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _default_modes(vehicle_mode: str | None) -> list[str]:
    """Return the modes to preselect for a stop.

    A stop the feed calls a bus stop can also be served by trolleybus routes,
    which are published as buses, so both are preselected together.
    """
    if vehicle_mode == MODE_BUS:
        return [MODE_BUS, MODE_TROLLEYBUS]
    if vehicle_mode in SUPPORTED_MODES:
        return [vehicle_mode]
    return list(SUPPORTED_MODES)


def _labelled(stop: Stop) -> str:
    """Return a stop as "Name (code)", or just the name if it has no code."""
    return f"{stop.name} ({stop.code})" if stop.code else stop.name


def _title(origin: Stop, destination: Stop | None) -> str:
    """Build the config entry title.

    Both ends carry their stop code, since stop names repeat across Estonia and
    the entry list is where boards are told apart.
    """
    title = _labelled(origin)
    if destination is not None:
        return f"{title} → {_labelled(destination)}"
    return title


class PeatusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-facing setup of a departure board."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow state."""
        self._stops: list[Stop] = []
        self._origin: Stop | None = None
        self._destination: Stop | None = None
        self._picking_destination = False
        # The journey branch works in places rather than stops, so it keeps its
        # own state; only one of the two sets is ever filled in.
        self._origin_entity: str | None = None
        self._destination_entity: str | None = None
        self._origin_name: str | None = None
        self._destination_name: str | None = None

    @property
    def _api(self) -> PeatusApi:
        """Return an API client bound to Home Assistant's shared session."""
        return PeatusApi(async_get_clientsession(self.hass))

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose what kind of board to add."""
        return self.async_show_menu(
            step_id="user", menu_options=["search", "gtfs_id", "journey"]
        )

    async def async_step_journey(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the two places a journey runs between."""
        errors: dict[str, str] = {}
        if user_input is not None:
            origin = user_input[CONF_ORIGIN_ENTITY]
            destination = user_input[CONF_DESTINATION_ENTITY]
            if origin == destination:
                errors[CONF_DESTINATION_ENTITY] = "same_place"
            else:
                self._origin_entity = origin
                self._destination_entity = destination
                # Snapshotted now, the same way a stop's name is: the entity ID
                # is unreadable in a sensor name, and a later rename must not
                # move the entity IDs of a board that already exists.
                self._origin_name = self._place_name(origin)
                self._destination_name = self._place_name(destination)
                return await self.async_step_journey_settings()
        return self.async_show_form(
            step_id="journey", data_schema=JOURNEY_SCHEMA, errors=errors
        )

    async def async_step_journey_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how the journey is planned, then create the entry."""
        assert self._origin_entity is not None
        assert self._destination_entity is not None

        await self.async_set_unique_id(self._journey_unique_id())
        self._abort_if_unique_id_configured(error="already_configured_journey")

        if user_input is not None:
            return self.async_create_entry(
                title=f"{self._origin_name} → {self._destination_name}",
                data={
                    CONF_BOARD: BOARD_JOURNEY,
                    CONF_ORIGIN_ENTITY: self._origin_entity,
                    CONF_ORIGIN_NAME: self._origin_name,
                    CONF_DESTINATION_ENTITY: self._destination_entity,
                    CONF_DESTINATION_NAME: self._destination_name,
                },
                options=_journey_options(user_input),
            )

        return self.async_show_form(
            step_id="journey_settings",
            data_schema=_journey_settings_schema(
                list(SUPPORTED_MODES),
                DEFAULT_JOURNEY_SCAN_INTERVAL,
                [],
                [],
                PlanOptions(),
            ),
            description_placeholders={
                "origin": self._origin_name or "",
                "destination": self._destination_name or "",
            },
        )

    def _place_name(self, entity_id: str) -> str:
        """Return what to call one end of a journey.

        Falls back to the entity ID, so a board is still nameable for an entity
        that has not been given a friendly name.
        """
        if (state := self.hass.states.get(entity_id)) is not None:
            return state.name or entity_id
        return entity_id

    def _journey_unique_id(self) -> str:
        """Return a stable unique ID for a journey board.

        Namespaced by board kind, which stop boards are not: theirs are GTFS
        IDs already written into existing entries, and a GTFS ID never starts
        with this prefix, so the two can never collide.
        """
        return f"{BOARD_JOURNEY}:{self._origin_entity}|{self._destination_entity}"

    async def async_step_search(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Search for the departure stop by name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._picking_destination = False
            errors = await self._async_search(user_input[CONF_NAME])
            if not errors:
                return await self.async_step_pick_stop()
        return self.async_show_form(
            step_id="search", data_schema=SEARCH_SCHEMA, errors=errors
        )

    async def async_step_gtfs_id(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept a GTFS stop ID such as ``estonia:1292`` directly."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._origin = await self._api.async_get_stop(
                    user_input[CONF_STOP_ID].strip()
                )
            except PeatusStopNotFoundError:
                errors[CONF_STOP_ID] = "unknown_stop"
            except PeatusApiError as err:
                _LOGGER.debug("Stop lookup failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_destination()
        return self.async_show_form(
            step_id="gtfs_id", data_schema=GTFS_ID_SCHEMA, errors=errors
        )

    async def async_step_pick_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the departure stop from the search results."""
        if user_input is not None:
            self._origin = self._selected(user_input[CONF_STOP_PICK])
            return await self.async_step_destination()
        return self.async_show_form(
            step_id="pick_stop", data_schema=_stop_picker(self._stops)
        )

    async def async_step_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally restrict departures to those heading to another stop."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = (user_input.get(CONF_NAME) or "").strip()
            if not name:
                return await self.async_step_settings()
            self._picking_destination = True
            errors = await self._async_search(name)
            if not errors:
                return await self.async_step_pick_destination()
        return self.async_show_form(
            step_id="destination",
            data_schema=vol.Schema({vol.Optional(CONF_NAME): TextSelector()}),
            errors=errors,
            description_placeholders={
                "stop": self._origin.name if self._origin else ""
            },
        )

    async def async_step_pick_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the destination stop from the search results."""
        errors: dict[str, str] = {}
        if user_input is not None:
            destination = self._selected(user_input[CONF_STOP_PICK])
            assert self._origin is not None
            if destination.gtfs_id == self._origin.gtfs_id:
                errors[CONF_STOP_PICK] = "same_stop"
            else:
                self._destination = destination
                return await self.async_step_settings()
        return self.async_show_form(
            step_id="pick_destination",
            data_schema=_stop_picker(self._stops),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose transport modes and the poll interval, then create the entry."""
        assert self._origin is not None
        # Checked here rather than earlier: the same stop may be configured once
        # without a destination and again with one, so the pair is only complete
        # once the destination step has been answered or skipped.
        await self.async_set_unique_id(self._unique_id())
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title=_title(self._origin, self._destination),
                data={
                    # Written explicitly so a new entry describes itself, even
                    # though the absence of it already means a stop board.
                    CONF_BOARD: BOARD_STOP,
                    CONF_STOP_ID: self._origin.gtfs_id,
                    CONF_STOP_NAME: self._origin.name,
                    CONF_STOP_CODE: self._origin.code,
                    CONF_STOP_MODE: self._origin.vehicle_mode,
                    CONF_STOP_DESC: self._origin.desc,
                    CONF_DESTINATION_ID: (
                        self._destination.gtfs_id if self._destination else None
                    ),
                    CONF_DESTINATION_NAME: (
                        self._destination.name if self._destination else None
                    ),
                },
                options={
                    CONF_MODES: user_input[CONF_MODES],
                    CONF_ROUTES: user_input.get(CONF_ROUTES) or [],
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                },
            )

        default_modes = _default_modes(self._origin.vehicle_mode)
        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(
                default_modes, DEFAULT_SCAN_INTERVAL, self._origin.routes, []
            ),
            description_placeholders={"stop": self._origin.name},
        )

    async def _async_search(self, name: str) -> dict[str, str]:
        """Run a stop search, storing results. Returns form errors, if any."""
        try:
            stops = await self._api.async_search_stops(name)
        except PeatusApiError as err:
            _LOGGER.debug("Stop search failed: %s", err)
            return {"base": "cannot_connect"}
        if not stops:
            return {CONF_NAME: "no_stops_found"}
        self._stops = sorted(stops, key=_stop_sort_key)
        return {}

    def _selected(self, gtfs_id: str) -> Stop:
        """Return the searched stop matching ``gtfs_id``."""
        return next(stop for stop in self._stops if stop.gtfs_id == gtfs_id)

    def _unique_id(self) -> str:
        """Return a stable unique ID for the origin/destination pair."""
        assert self._origin is not None
        destination = self._destination.gtfs_id if self._destination else "any"
        return f"{self._origin.gtfs_id}|{destination}"

    @staticmethod
    @callback
    def async_get_options_flow(entry: PeatusConfigEntry) -> PeatusOptionsFlow:
        """Return the options flow handler."""
        return PeatusOptionsFlow()


class PeatusOptionsFlow(OptionsFlow):
    """Allow changing modes, route filters and the poll interval after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the adjustable settings."""
        if board_type(self.config_entry) == BOARD_JOURNEY:
            return await self.async_step_journey_options(user_input)

        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_MODES: user_input[CONF_MODES],
                    CONF_ROUTES: user_input.get(CONF_ROUTES) or [],
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                }
            )

        options = self.config_entry.options
        selected = list(options.get(CONF_ROUTES) or [])
        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(
                list(options.get(CONF_MODES) or SUPPORTED_MODES),
                int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
                await self._async_routes(selected),
                selected,
            ),
            description_placeholders={
                "stop": self.config_entry.data.get(CONF_STOP_NAME) or ""
            },
        )

    async def async_step_journey_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the adjustable settings of a journey board.

        A journey has no single stop to list lines from — and asking the feed
        for one would fail, since the entry holds no stop ID at all — so the
        lines already chosen are the only ones offered. The selector accepts
        typed values, so a line can still be added by number.
        """
        if user_input is not None:
            return self.async_create_entry(data=_journey_options(user_input))

        options = self.config_entry.options
        selected = list(options.get(CONF_ROUTES) or [])
        data = self.config_entry.data
        return self.async_show_form(
            step_id="journey_options",
            data_schema=_journey_settings_schema(
                list(options.get(CONF_MODES) or SUPPORTED_MODES),
                int(options.get(CONF_SCAN_INTERVAL, DEFAULT_JOURNEY_SCAN_INTERVAL)),
                sorted(selected, key=route_sort_key),
                selected,
                PlanOptions(
                    walk_speed_kmh=float(
                        options.get(CONF_WALK_SPEED, DEFAULT_WALK_SPEED)
                    ),
                    bike_speed_kmh=float(
                        options.get(CONF_BIKE_SPEED, DEFAULT_BIKE_SPEED)
                    ),
                    bike_optimize=str(
                        options.get(CONF_BIKE_OPTIMIZE, DEFAULT_BIKE_OPTIMIZE)
                    ),
                    max_walk_distance_m=int(
                        options.get(CONF_MAX_WALK_DISTANCE, DEFAULT_MAX_WALK_DISTANCE)
                    ),
                ),
            ),
            description_placeholders={
                "origin": data.get(CONF_ORIGIN_NAME) or "",
                "destination": data.get(CONF_DESTINATION_NAME) or "",
            },
        )

    async def _async_routes(self, selected: list[str]) -> list[str]:
        """Return the route numbers to offer, including any already selected.

        The stop is re-read rather than taken from the entry, because the lines
        calling at a stop change whenever the timetable does. A selected route
        that has since been withdrawn is still offered, so that reopening the
        options does not silently drop it from the filter.
        """
        routes = list(selected)
        try:
            stop = await PeatusApi(async_get_clientsession(self.hass)).async_get_stop(
                self.config_entry.data[CONF_STOP_ID]
            )
        except PeatusApiError as err:
            # Not worth failing the form over: the settings that do not depend
            # on the feed are still editable, and the saved filter is kept.
            _LOGGER.debug("Could not list routes for the options form: %s", err)
        else:
            routes += [route for route in stop.routes if route not in routes]
        return sorted(routes, key=route_sort_key)
