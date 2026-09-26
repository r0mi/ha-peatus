"""Data update coordinator for the Peatus.ee integration."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.location import find_coordinates
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Departure, Itinerary, PeatusApi, PeatusApiError, PlanOptions
from .const import (
    BOARD_JOURNEY,
    BOARD_STOP,
    CONF_BIKE_OPTIMIZE,
    CONF_BIKE_SPEED,
    CONF_BOARD,
    CONF_DESTINATION_ENTITY,
    CONF_DESTINATION_ID,
    CONF_MODES,
    CONF_ORIGIN_ENTITY,
    CONF_ROUTES,
    CONF_STOP_ID,
    CONF_WALK_SPEED,
    COORDINATE_PRECISION,
    DEFAULT_BIKE_OPTIMIZE,
    DEFAULT_BIKE_SPEED,
    DEFAULT_JOURNEY_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WALK_SPEED,
    DOMAIN,
    MODE_BUS,
    MODE_TROLLEYBUS,
    NUM_DEPARTURES,
    NUM_ITINERARIES,
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

#: Itineraries asked of the planner, smallest first. The feed filters modes
#: itself, so the plain case asks for exactly what is shown; a line filter, or
#: a mode choice that splits buses from trolleybuses, discards plans the feed
#: cannot know about and so starts by asking for more. It never grows as far as
#: a departure board does: planning is expensive and a board this tall has no
#: use for fifty answers.
_PLAN_STEPS_PLAIN = (NUM_ITINERARIES,)
_PLAN_STEPS_FILTERED = (NUM_ITINERARIES, 15)

#: A "lat,lon" pair exactly as ``find_coordinates`` formats one.
_COORDINATES = re.compile(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")

_MAX_LATITUDE = 90
_MAX_LONGITUDE = 180

type PeatusConfigEntry = ConfigEntry[PeatusCoordinator | PeatusJourneyCoordinator]


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
        #: Scheduled ride seconds per trip ID. Emptied with the pattern cache,
        #: since both only change when the timetable does, which also keeps it
        #: from growing without bound as trips come and go.
        self._ride_seconds: dict[str, int] = {}

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
        return _modes(self.config_entry)

    @property
    def routes(self) -> list[str]:
        """Return the route numbers the user wants to see, empty for all."""
        return _routes(self.config_entry)

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
            self._ride_seconds.clear()
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
            departures = matched[:NUM_DEPARTURES]
            if self.destination_id is not None:
                await self._async_add_ride_lengths(departures)
        except PeatusApiError as err:
            raise UpdateFailed(str(err)) from err

        return departures

    async def _async_add_ride_lengths(self, departures: list[Departure]) -> None:
        """Fill in how long each departure takes to reach the destination.

        Only the trips not seen since the timetable was last read are looked
        up, so a board settles into asking for nothing extra: the same trips
        run again the next day.
        """
        assert self.destination_id is not None
        missing = [
            departure.trip_id
            for departure in departures
            if departure.trip_id is not None
            and departure.trip_id not in self._ride_seconds
        ]
        if missing:
            self._ride_seconds |= await self.api.async_get_ride_seconds(
                missing, self.stop_id, self.destination_id
            )
        for departure in departures:
            departure.ride_seconds = self._ride_seconds.get(departure.trip_id)


def board_type(entry: ConfigEntry) -> str:
    """Return which kind of board an entry configures.

    Entries created before journey boards existed carry no such key, so the
    absence of one means a stop board and no migration is needed.
    """
    return entry.data.get(CONF_BOARD, BOARD_STOP)


def _modes(entry: PeatusConfigEntry) -> list[str]:
    """Return the transport modes the user wants to see."""
    configured = entry.options.get(CONF_MODES, entry.data.get(CONF_MODES))
    return list(configured) if configured else list(SUPPORTED_MODES)


def _routes(entry: PeatusConfigEntry) -> list[str]:
    """Return the route numbers the user wants to see, empty for all."""
    configured = entry.options.get(CONF_ROUTES, entry.data.get(CONF_ROUTES))
    return list(configured) if configured else []


def _plan_options(entry: PeatusConfigEntry) -> PlanOptions:
    """Return the journey routing preferences, falling back to the defaults.

    An entry configured before these existed simply has none of the keys, so
    each one defaults on its own rather than the set defaulting as a whole.
    """
    return PlanOptions(
        walk_speed_kmh=float(entry.options.get(CONF_WALK_SPEED, DEFAULT_WALK_SPEED)),
        bike_speed_kmh=float(entry.options.get(CONF_BIKE_SPEED, DEFAULT_BIKE_SPEED)),
        bike_optimize=str(entry.options.get(CONF_BIKE_OPTIMIZE, DEFAULT_BIKE_OPTIMIZE)),
    )


def _scan_interval(entry: PeatusConfigEntry) -> int:
    """Return the configured poll interval in minutes."""
    default = (
        DEFAULT_JOURNEY_SCAN_INTERVAL
        if board_type(entry) == BOARD_JOURNEY
        else DEFAULT_SCAN_INTERVAL
    )
    return int(
        entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, default),
        )
    )


def _resolve(hass: HomeAssistant, entity_id: str) -> tuple[float, float]:
    """Return where a tracked entity or zone is now, as rounded coordinates.

    ``find_coordinates`` never raises and never signals failure with a type.
    Given an entity that does not exist it hands back the string it was given;
    given one with no location it hands back that entity's plain state
    ("not_home", "unknown", "unavailable"); given a self-referential one it
    hands back ``None``. Its answer is therefore trusted only once it has
    parsed as a coordinate pair, and each way of not being one is reported
    separately, because each needs different fixing.

    The result is rounded because a phone's GPS drifts while it sits still, and
    an unrounded pair would re-plan the journey and rewrite every sensor on
    every poll for a movement of a couple of metres.
    """
    found = find_coordinates(hass, entity_id)
    if found is None:
        raise UpdateFailed(f"{entity_id} refers back to itself; cannot locate it")

    if (match := _COORDINATES.fullmatch(found.strip())) is None:
        if (state := hass.states.get(entity_id)) is None:
            raise UpdateFailed(
                f"{entity_id} does not exist; choose the journey's ends again"
            )
        raise UpdateFailed(
            f"{entity_id} reports no location (its state is {state.state!r}); "
            "only zones and trackers that publish coordinates can be planned from"
        )

    latitude, longitude = float(match[1]), float(match[2])
    if abs(latitude) > _MAX_LATITUDE or abs(longitude) > _MAX_LONGITUDE:
        raise UpdateFailed(f"{entity_id} reports an impossible location: {found}")
    return (
        round(latitude, COORDINATE_PRECISION),
        round(longitude, COORDINATE_PRECISION),
    )


@dataclass(slots=True)
class JourneyData:
    """Everything one journey board shows.

    The walking and cycling plans are kept beside the transit ones rather than
    among them: they are always offered, never filtered, and answer a different
    question ("how long would it take me to get there myself").
    """

    itineraries: list[Itinerary]
    walk: Itinerary | None
    bicycle: Itinerary | None


class PeatusJourneyCoordinator(DataUpdateCoordinator[JourneyData]):
    """Plans door-to-door journeys between two tracked places."""

    config_entry: PeatusConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PeatusConfigEntry) -> None:
        """Initialise the coordinator from a config entry."""
        self.api = PeatusApi(async_get_clientsession(hass))
        self.origin_entity: str = entry.data[CONF_ORIGIN_ENTITY]
        self.destination_entity: str = entry.data[CONF_DESTINATION_ENTITY]

        #: The walking and cycling plans, and the endpoints they were made for.
        #: Neither depends on the timetable, so they are re-planned only when
        #: the rounded coordinates actually move. Changing a speed reloads the
        #: whole entry, which rebuilds this coordinator and drops the cache.
        self._fixed_for: tuple[tuple[float, float], tuple[float, float]] | None = None
        self._walk: Itinerary | None = None
        self._bicycle: Itinerary | None = None
        self._warned_empty = False

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.origin_entity} → {self.destination_entity}",
            update_interval=timedelta(minutes=_scan_interval(entry)),
            config_entry=entry,
        )

    @property
    def modes(self) -> list[str]:
        """Return the transport modes the user wants to travel by."""
        return _modes(self.config_entry)

    @property
    def routes(self) -> list[str]:
        """Return the route numbers the user is willing to ride, empty for all."""
        return _routes(self.config_entry)

    @property
    def _plan_steps(self) -> tuple[int, ...]:
        """Return the itinerary counts to request, smallest first."""
        modes = set(self.modes)
        # Asking for buses without trolleybuses, or the other way round, is a
        # distinction the feed cannot make, so those plans are discarded here
        # and the request has to start larger to survive it.
        splits_buses = (MODE_BUS in modes) != (MODE_TROLLEYBUS in modes)
        if self.routes or splits_buses:
            return _PLAN_STEPS_FILTERED
        return _PLAN_STEPS_PLAIN

    def _filter(self, itineraries: list[Itinerary]) -> list[Itinerary]:
        """Drop the plans the user would not want to be offered.

        A plan is kept only when every ride on it is one the user would take: a
        journey is a single take-it-or-leave-it offer, so a change onto a line
        they filtered out is not a journey they can use. Walking, cycling and
        waiting are never filtered.
        """
        modes = set(self.modes)
        routes = set(self.routes)
        kept = []
        for itinerary in itineraries:
            rides = itinerary.rides
            # Rides whose mode the feed does not report are kept, so an
            # incomplete feed never silently empties the board.
            if any(ride.mode is not None and ride.mode not in modes for ride in rides):
                continue
            if routes and any(ride.route_short_name not in routes for ride in rides):
                continue
            kept.append(itinerary)
        return kept

    async def _async_fixed(
        self, origin: tuple[float, float], destination: tuple[float, float]
    ) -> tuple[Itinerary | None, Itinerary | None]:
        """Return the walking and cycling plans, re-planning them only if moved."""
        if self._fixed_for != (origin, destination):
            options = _plan_options(self.config_entry)
            self._walk = await self.api.async_plan_walk(origin, destination, options)
            self._bicycle = await self.api.async_plan_bicycle(
                origin, destination, options
            )
            self._fixed_for = (origin, destination)
        return self._walk, self._bicycle

    async def _async_update_data(self) -> JourneyData:
        """Plan the next journeys between the two configured places."""
        # Resolved before the request and outside the API error handling: a
        # tracker that cannot be located is not the feed's fault, and saying so
        # is the difference between a fixable message and a silent wrong answer.
        origin = _resolve(self.hass, self.origin_entity)
        destination = _resolve(self.hass, self.destination_entity)

        try:
            planned: list[Itinerary] = []
            matched: list[Itinerary] = []
            for count in self._plan_steps:
                planned = await self.api.async_plan(
                    origin,
                    destination,
                    count,
                    self.modes,
                    _plan_options(self.config_entry),
                )
                matched = self._filter(planned)
                # Stop early once there is enough, or once the planner itself
                # ran out of answers and a larger request cannot help.
                if len(matched) >= NUM_ITINERARIES or len(planned) < count:
                    break
            walk, bicycle = await self._async_fixed(origin, destination)
        except PeatusApiError as err:
            raise UpdateFailed(str(err)) from err

        self._warn_if_empty(matched, planned)
        return JourneyData(
            itineraries=matched[:NUM_ITINERARIES], walk=walk, bicycle=bicycle
        )

    def _warn_if_empty(
        self, matched: list[Itinerary], planned: list[Itinerary]
    ) -> None:
        """Warn once when a successful plan leaves the board with nothing on it.

        An empty board is a legitimate answer rather than a failure, but the
        feed reports "these two places are not connected" and "your filter
        matched nothing" identically, as an empty list, so the two are told
        apart here and named differently.
        """
        if matched:
            self._warned_empty = False
            return
        if self._warned_empty:
            return
        if not planned:
            _LOGGER.warning(
                "No journey was found from %s to %s; the board will stay empty",
                self.origin_entity,
                self.destination_entity,
            )
        else:
            _LOGGER.warning(
                "None of the %d journeys from %s to %s use only the lines %s; "
                "the board will stay empty until the filter is widened",
                len(planned),
                self.origin_entity,
                self.destination_entity,
                ", ".join(self.routes) or "selected",
            )
        self._warned_empty = True
