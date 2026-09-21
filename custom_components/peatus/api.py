"""GraphQL client for the peatus.ee routing API."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import (
    API_URL,
    GEOCODER_URL,
    MODE_BUS,
    MODE_TROLLEYBUS,
    SEARCH_LIMIT,
    TIME_RANGE,
    TROLLEYBUS_MARKER,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30

#: Number of route numbers shown in a stop's picker label before truncating.
MAX_LABEL_ROUTES = 6


class PeatusApiError(Exception):
    """Raised when the peatus.ee API cannot be reached or returns an error."""


class PeatusStopNotFoundError(PeatusApiError):
    """Raised when a stop ID does not exist in the feed."""


@dataclass(slots=True)
class Stop:
    """A single stop (one platform / one direction) in the feed."""

    gtfs_id: str
    name: str
    code: str | None
    desc: str | None
    zone_id: str | None
    vehicle_mode: str | None
    lat: float | None
    lon: float | None
    routes: list[str]
    #: Municipality and district, e.g. "Tallinna linn, Nõmme". Only the
    #: geocoder knows this; it is absent on stops fetched straight by ID.
    locality: str | None = None

    @property
    def label(self) -> str:
        """Return a human readable label used in the config flow picker."""
        parts = [self.name if not self.code else f"{self.name} ({self.code})"]
        if self.locality:
            parts.append(self.locality)
        if self.desc:
            parts.append(self.desc)
        if self.vehicle_mode:
            parts.append(self.vehicle_mode.title())
        if self.zone_id:
            parts.append(self.zone_id)
        if self.routes:
            shown = ", ".join(self.routes[:MAX_LABEL_ROUTES])
            if len(self.routes) > MAX_LABEL_ROUTES:
                shown += ", …"
            parts.append(shown)
        return " · ".join(parts)


@dataclass(slots=True)
class Departure:
    """A single upcoming departure from a stop."""

    #: Absolute epoch seconds of the departure, realtime when available.
    timestamp: int
    scheduled_timestamp: int
    realtime: bool
    realtime_state: str | None
    delay_seconds: int
    headsign: str | None
    route_short_name: str | None
    route_long_name: str | None
    #: Derived mode: ``TROLLEYBUS`` for trolleybus routes the feed reports as
    #: buses, otherwise the route mode as published.
    mode: str | None
    trip_id: str | None
    pattern_code: str | None


_STOP_FIELDS = "gtfsId name code desc zoneId vehicleMode lat lon routes{shortName}"

SEARCH_STOPS_QUERY = f"""
query SearchStops($name: String!) {{
  stops(name: $name) {{ {_STOP_FIELDS} }}
}}
"""

GET_STOPS_QUERY = f"""
query GetStops($ids: [String]!) {{
  stops(ids: $ids) {{ {_STOP_FIELDS} }}
}}
"""

GET_STOP_QUERY = f"""
query GetStop($id: String!) {{
  stop(id: $id) {{ {_STOP_FIELDS} }}
}}
"""

PATTERNS_QUERY = """
query StopPatterns($id: String!) {
  stop(id: $id) {
    patterns { code stops { gtfsId } }
  }
}
"""

DEPARTURES_QUERY = """
query Departures($id: String!, $count: Int!, $timeRange: Int!) {
  stop(id: $id) {
    stoptimesWithoutPatterns(
      numberOfDepartures: $count
      timeRange: $timeRange
      omitNonPickups: true
      omitCanceled: true
    ) {
      scheduledDeparture
      realtimeDeparture
      departureDelay
      realtime
      realtimeState
      serviceDay
      headsign
      trip {
        gtfsId
        pattern { code }
        route { shortName longName mode }
      }
    }
  }
}
"""


#: Punctuation and underscores are operators to the Lucene query parser behind
#: ``stops(name:)``, not characters to match, so the name-search fallback
#: replaces them with the spaces the parser treats as term separators.
_QUERY_OPERATORS = re.compile(r"[\W_]+", re.UNICODE)


def route_sort_key(short_name: str) -> tuple[int, str, int, str]:
    """Return a sort key ordering route numbers the way a timetable does.

    Route names are a mix of numbers and letters ("1", "18", "119", "18V",
    "S12", "T3"). Sorting them as plain text puts 119 before 18, so the digits
    are compared as a number and any prefix or suffix as text around it.
    """
    match = re.fullmatch(r"(\D*)(\d+)(.*)", short_name.strip())
    if match is None:
        # No digits to compare: order these after the numbered routes.
        return (1, short_name.casefold(), 0, "")
    prefix, digits, suffix = match.groups()
    return (0, prefix.casefold(), int(digits), suffix.casefold())


def _gtfs_id_from_feature(raw: dict[str, Any]) -> str | None:
    """Return the GTFS ID of a geocoder stop feature, or ``None`` if it has none.

    Stop features carry an ID like ``GTFS:estonia:952#04401-1``: the feed's own
    ``estonia:952`` behind a source tag, with the platform code repeated after
    the fragment marker.
    """
    properties = raw.get("properties") or {}
    if properties.get("layer") != "stop":
        return None
    source, _, gtfs_id = str(properties.get("id") or "").partition(":")
    if source.upper() != "GTFS" or not gtfs_id:
        return None
    return gtfs_id.split("#", 1)[0]


def _classify_mode(mode: str | None, long_name: str | None) -> str | None:
    """Return the mode of a route, separating trolleybuses out of the buses.

    The feed publishes modes upper case; they are lower cased here so the rest of
    the integration uses a single spelling.
    """
    if mode is None:
        return None
    mode = mode.lower()
    if mode == MODE_BUS and long_name and TROLLEYBUS_MARKER in long_name.casefold():
        return MODE_TROLLEYBUS
    return mode


class PeatusApi:
    """Thin async wrapper around the peatus.ee GraphQL endpoint."""

    def __init__(self, session: ClientSession) -> None:
        """Initialise the client with a shared aiohttp session."""
        self._session = session

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Execute a GraphQL query and return its ``data`` payload."""
        try:
            response = await self._session.post(
                API_URL,
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = await response.json()
        except ClientError as err:
            raise PeatusApiError(f"Error talking to peatus.ee: {err}") from err
        except TimeoutError as err:
            raise PeatusApiError("Timeout talking to peatus.ee") from err

        if errors := payload.get("errors"):
            message = "; ".join(str(error.get("message", error)) for error in errors)
            raise PeatusApiError(f"peatus.ee returned an error: {message}")

        data = payload.get("data")
        if data is None:
            raise PeatusApiError("peatus.ee returned no data")
        return data

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Fetch and decode a JSON document over GET."""
        try:
            response = await self._session.get(
                url, params=params, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            # Pelias answers as application/json; be lenient about the header so
            # a proxy in front of it cannot break the search.
            return await response.json(content_type=None)
        except ClientError as err:
            raise PeatusApiError(f"Error talking to peatus.ee: {err}") from err
        except TimeoutError as err:
            raise PeatusApiError("Timeout talking to peatus.ee") from err

    @staticmethod
    def _parse_stop(raw: dict[str, Any], locality: str | None = None) -> Stop:
        """Convert a raw GraphQL stop object into a :class:`Stop`."""
        vehicle_mode = raw.get("vehicleMode")
        return Stop(
            gtfs_id=raw["gtfsId"],
            name=raw.get("name") or raw["gtfsId"],
            code=raw.get("code"),
            desc=raw.get("desc"),
            zone_id=raw.get("zoneId"),
            vehicle_mode=vehicle_mode.lower() if vehicle_mode else None,
            lat=raw.get("lat"),
            lon=raw.get("lon"),
            locality=locality,
            routes=sorted(
                {
                    route["shortName"]
                    for route in (raw.get("routes") or [])
                    if route.get("shortName")
                },
                key=route_sort_key,
            ),
        )

    async def async_search_stops(self, name: str) -> list[Stop]:
        """Search stops by (partial) name.

        Searches the geocoder web.peatus.ee itself uses, because
        OpenTripPlanner's ``stops(name:)`` field cannot answer this reliably:
        it parses the query as Lucene, where punctuation is an operator rather
        than text, so hyphenated names like "Vana-Pääsküla" match nothing, and
        it truncates every answer to ten stops without saying so.

        The geocoder only identifies stops, so the matches are hydrated from
        the feed to recover the modes and routes the picker labels them with.
        """
        try:
            features = await self._geocode(name)
        except PeatusApiError as err:
            _LOGGER.debug("Geocoder unreachable, falling back to name search: %s", err)
            return await self._async_search_stops_by_name(name)

        localities: dict[str, str | None] = {}
        for feature in features:
            if (gtfs_id := _gtfs_id_from_feature(feature)) is None:
                continue
            # Keep the first hit per stop: the geocoder answers by descending
            # relevance, and a stop can appear once per name it is indexed under.
            localities.setdefault(
                gtfs_id, (feature.get("properties") or {}).get("locality")
            )

        if not localities:
            # The geocoder is indexed separately from the feed, so let the name
            # search have a go at anything it has never heard of.
            return await self._async_search_stops_by_name(name)

        return await self._async_hydrate_stops(localities)

    async def _geocode(self, name: str) -> list[dict[str, Any]]:
        """Return the geocoder's stop features for a (partial) name."""
        payload = await self._get(
            GEOCODER_URL,
            {"text": name, "size": SEARCH_LIMIT, "layers": "stop"},
        )
        return payload.get("features") or []

    async def _async_hydrate_stops(
        self, localities: dict[str, str | None]
    ) -> list[Stop]:
        """Fetch full records for the given stop IDs, keeping the given order."""
        data = await self._query(GET_STOPS_QUERY, {"ids": list(localities)})
        stops: list[Stop] = []
        for raw in data.get("stops") or []:
            # The feed returns a null per ID it does not know, in the order
            # asked, so a stop the geocoder still lists is simply skipped.
            if raw is None:
                continue
            stops.append(self._parse_stop(raw, localities.get(raw.get("gtfsId"))))
        return stops

    async def _async_search_stops_by_name(self, name: str) -> list[Stop]:
        """Search stops through the feed's own name index.

        Only a fallback for when the geocoder cannot answer: the query is
        stripped down to bare terms so the Lucene parser behind it treats the
        whole name as text to match, at the cost of matching each word of a
        name independently.
        """
        terms = _QUERY_OPERATORS.sub(" ", name).strip()
        if not terms:
            return []
        data = await self._query(SEARCH_STOPS_QUERY, {"name": terms})
        return [self._parse_stop(raw) for raw in (data.get("stops") or [])]

    async def async_get_stop(self, gtfs_id: str) -> Stop:
        """Fetch a single stop by its GTFS ID."""
        data = await self._query(GET_STOP_QUERY, {"id": gtfs_id})
        if (raw := data.get("stop")) is None:
            raise PeatusStopNotFoundError(f"Stop {gtfs_id} does not exist")
        return self._parse_stop(raw)

    async def async_get_pattern_codes_to(
        self, origin_id: str, destination_id: str
    ) -> set[str]:
        """Return codes of patterns serving ``destination_id`` after ``origin_id``.

        Filtering departures by pattern code keeps the per-update payload small:
        the pattern list only has to be fetched when the timetable changes,
        rather than pulling every trip's full stop list on each poll.
        """
        data = await self._query(PATTERNS_QUERY, {"id": origin_id})
        if (stop := data.get("stop")) is None:
            raise PeatusStopNotFoundError(f"Stop {origin_id} does not exist")

        codes: set[str] = set()
        for pattern in stop.get("patterns") or []:
            stop_ids = [entry["gtfsId"] for entry in pattern.get("stops") or []]
            if origin_id not in stop_ids:
                continue
            origin_index = stop_ids.index(origin_id)
            if destination_id in stop_ids[origin_index + 1 :]:
                codes.add(pattern["code"])
        return codes

    async def async_get_departures(self, gtfs_id: str, count: int) -> list[Departure]:
        """Fetch up to ``count`` upcoming departures from a stop."""
        data = await self._query(
            DEPARTURES_QUERY,
            {"id": gtfs_id, "count": count, "timeRange": TIME_RANGE},
        )
        if (stop := data.get("stop")) is None:
            raise PeatusStopNotFoundError(f"Stop {gtfs_id} does not exist")

        departures: list[Departure] = []
        for raw in stop.get("stoptimesWithoutPatterns") or []:
            service_day = raw.get("serviceDay")
            scheduled = raw.get("scheduledDeparture")
            if service_day is None or scheduled is None:
                continue
            realtime_departure = raw.get("realtimeDeparture")
            if realtime_departure is None:
                realtime_departure = scheduled

            trip = raw.get("trip") or {}
            route = trip.get("route") or {}
            pattern = trip.get("pattern") or {}
            long_name = route.get("longName")

            departures.append(
                Departure(
                    timestamp=service_day + realtime_departure,
                    scheduled_timestamp=service_day + scheduled,
                    realtime=bool(raw.get("realtime")),
                    realtime_state=raw.get("realtimeState"),
                    delay_seconds=raw.get("departureDelay") or 0,
                    headsign=raw.get("headsign") or trip.get("tripHeadsign"),
                    route_short_name=route.get("shortName"),
                    route_long_name=long_name,
                    mode=_classify_mode(route.get("mode"), long_name),
                    trip_id=trip.get("gtfsId"),
                    pattern_code=pattern.get("code"),
                )
            )

        # OTP returns departures in order, but realtime delays can reorder them.
        departures.sort(key=lambda departure: departure.timestamp)
        return departures
