"""Data update coordinator for the Peatus.ee integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Departure, PeatusApi, PeatusApiError
from .const import (
    CONF_DESTINATION_ID,
    CONF_MODES,
    CONF_ROUTES,
    CONF_STOP_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    NUM_DEPARTURES,
    PATTERN_CACHE_TTL,
    SUPPORTED_MODES,
)

_LOGGER = logging.getLogger(__name__)

#: Departure counts tried in turn, smallest first. Requesting only what is
#: needed keeps the unfiltered case at a couple of kilobytes per poll, while a
#: destination filter starts larger because it usually discards most results.
_FETCH_STEPS_PLAIN = (NUM_DEPARTURES,)
_FETCH_STEPS_FILTERED = (NUM_DEPARTURES, 60, 150)
_FETCH_STEPS_DESTINATION = (60, 150, 300)

type PeatusConfigEntry = ConfigEntry[PeatusCoordinator]


class PeatusCoordinator(DataUpdateCoordinator[list[Departure]]):
    """Polls peatus.ee for the upcoming departures of one configured stop."""

    config_entry: PeatusConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PeatusConfigEntry) -> None:
        """Initialise the coordinator from a config entry."""
        self.api = PeatusApi(async_get_clientsession(hass))
        self.stop_id: str = entry.data[CONF_STOP_ID]
        self.destination_id: str | None = entry.data.get(CONF_DESTINATION_ID)

        self._pattern_codes: set[str] | None = None
        self._pattern_codes_fetched: float = 0.0

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.stop_id}",
            update_interval=timedelta(minutes=_scan_interval(entry)),
            config_entry=entry,
        )

    @property
    def modes(self) -> list[str]:
        """Return the transport modes the user wants to see."""
        configured = self.config_entry.options.get(
            CONF_MODES, self.config_entry.data.get(CONF_MODES)
        )
        return list(configured) if configured else list(SUPPORTED_MODES)

    @property
    def routes(self) -> list[str]:
        """Return the route numbers the user wants to see, empty for all."""
        configured = self.config_entry.options.get(
            CONF_ROUTES, self.config_entry.data.get(CONF_ROUTES)
        )
        return list(configured) if configured else []

    @property
    def _fetch_steps(self) -> tuple[int, ...]:
        """Return the departure counts to request, smallest first."""
        if self.destination_id is not None:
            return _FETCH_STEPS_DESTINATION
        if self.routes or set(self.modes) != set(SUPPORTED_MODES):
            return _FETCH_STEPS_FILTERED
        return _FETCH_STEPS_PLAIN

    async def _async_pattern_codes(self) -> set[str]:
        """Return the cached set of pattern codes reaching the destination."""
        now = dt_util.utcnow().timestamp()
        if (
            self._pattern_codes is None
            or now - self._pattern_codes_fetched > PATTERN_CACHE_TTL
        ):
            assert self.destination_id is not None
            self._pattern_codes = await self.api.async_get_pattern_codes_to(
                self.stop_id, self.destination_id
            )
            self._pattern_codes_fetched = now
            if not self._pattern_codes:
                _LOGGER.warning(
                    "No route from %s reaches %s; no departures will be shown",
                    self.stop_id,
                    self.destination_id,
                )
        return self._pattern_codes

    def _filter(
        self, departures: list[Departure], codes: set[str] | None
    ) -> list[Departure]:
        """Apply the destination, route and mode filters to fetched departures."""
        modes = set(self.modes)
        # Matched by route number rather than route ID: the feed carries a
        # separate route per timetable period, so the same line changes ID
        # whenever the schedule is revised while its number stays put.
        routes = set(self.routes)
        result = []
        for departure in departures:
            if codes is not None and departure.pattern_code not in codes:
                continue
            if routes and departure.route_short_name not in routes:
                continue
            # Departures whose mode the feed does not report are kept, so an
            # incomplete feed never silently empties the sensors.
            if departure.mode is not None and departure.mode not in modes:
                continue
            result.append(departure)
        return result

    async def _async_update_data(self) -> list[Departure]:
        """Fetch the next departures, growing the request until enough match."""
        try:
            codes = await self._async_pattern_codes() if self.destination_id else None

            matched: list[Departure] = []
            for count in self._fetch_steps:
                fetched = await self.api.async_get_departures(self.stop_id, count)
                matched = self._filter(fetched, codes)
                # Stop early once we have enough, or once the feed itself ran
                # out of departures and a larger request cannot help.
                if len(matched) >= NUM_DEPARTURES or len(fetched) < count:
                    break
        except PeatusApiError as err:
            raise UpdateFailed(str(err)) from err

        return matched[:NUM_DEPARTURES]


def _scan_interval(entry: PeatusConfigEntry) -> int:
    """Return the configured poll interval in minutes."""
    return int(
        entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
    )
