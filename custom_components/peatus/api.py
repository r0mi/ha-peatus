"""GraphQL client for the peatus.ee routing API."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import API_URL, MODE_BUS, MODE_TROLLEYBUS, TIME_RANGE, TROLLEYBUS_MARKER

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

    @property
    def label(self) -> str:
        """Return a human readable label used in the config flow picker."""
        parts = [self.name if not self.code else f"{self.name} ({self.code})"]
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

    @staticmethod
    def _parse_stop(raw: dict[str, Any]) -> Stop:
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
            routes=sorted(
                {
                    route["shortName"]
                    for route in (raw.get("routes") or [])
                    if route.get("shortName")
                }
            ),
        )

    async def async_search_stops(self, name: str) -> list[Stop]:
        """Search stops by (partial) name."""
        data = await self._query(SEARCH_STOPS_QUERY, {"name": name})
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
