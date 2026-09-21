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
    Stop,
    route_sort_key,
)
from .const import (
    CONF_DESTINATION_ID,
    CONF_DESTINATION_NAME,
    CONF_MODES,
    CONF_ROUTES,
    CONF_STOP_CODE,
    CONF_STOP_DESC,
    CONF_STOP_ID,
    CONF_STOP_MODE,
    CONF_STOP_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MODE_BUS,
    MODE_TROLLEYBUS,
    SUPPORTED_MODES,
)
from .coordinator import PeatusConfigEntry

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

    @property
    def _api(self) -> PeatusApi:
        """Return an API client bound to Home Assistant's shared session."""
        return PeatusApi(async_get_clientsession(self.hass))

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose how to identify the stop."""
        return self.async_show_menu(step_id="user", menu_options=["search", "gtfs_id"])

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
        self._stops = sorted(stops, key=lambda stop: (stop.name, stop.code or ""))
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
