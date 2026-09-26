"""GraphQL client for the peatus.ee routing API."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import (
    API_URL,
    DEFAULT_BIKE_OPTIMIZE,
    DEFAULT_BIKE_SPEED,
    DEFAULT_WALK_SPEED,
    GEOCODER_URL,
    MIN_ACCESS_WALK,
    MODE_BICYCLE,
    MODE_BUS,
    MODE_TROLLEYBUS,
    MODE_WAIT,
    MODE_WALK,
    PLAN_MODES,
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
    #: Scheduled seconds from this stop to the configured destination, set once
    #: the coordinator has resolved it. ``None`` without a destination.
    ride_seconds: int | None = None


@dataclass(slots=True)
class Leg:
    """One segment of a planned journey: a walk, a ride, or a wait between them.

    Legs tile their itinerary exactly — each starts where the previous one
    ended — so a bar drawn from their durations leaves no gap to account for.
    """

    #: ``walk``, ``bicycle``, ``wait``, or a classified transit mode
    #: (``bus``, ``trolleybus``, ``tram``, ``rail``, ``ferry``). Always lower
    #: case, spelled the same way a departure's mode is.
    mode: str
    #: Absolute epoch seconds. The feed answers in milliseconds.
    start_timestamp: int
    end_timestamp: int
    #: Seconds this leg lasts, derived from its own two timestamps rather than
    #: read from the feed's separate float, which is what guarantees the legs
    #: of an itinerary sum to its span.
    duration: int
    #: Metres travelled, rounded. ``None`` on a wait, which goes nowhere.
    distance: int | None
    from_name: str | None
    from_stop_id: str | None
    from_stop_code: str | None
    to_name: str | None
    to_stop_id: str | None
    to_stop_code: str | None
    route_short_name: str | None
    route_long_name: str | None
    #: Where the vehicle is signed for. Read from the trip: legs have no
    #: headsign field of their own.
    headsign: str | None
    trip_id: str | None
    #: ``#rrggbb``. The feed publishes bare hex, which CSS will not accept.
    color: str | None
    text_color: str | None
    realtime: bool

    @property
    def transit(self) -> bool:
        """Return whether this leg is a ride rather than a walk or a wait."""
        return self.mode not in (MODE_WALK, MODE_BICYCLE, MODE_WAIT)


@dataclass(slots=True)
class Itinerary:
    """One complete door-to-door plan: a single row of a journey board."""

    #: When the traveller has to leave, and when they arrive. Epoch seconds.
    start_timestamp: int
    end_timestamp: int
    duration: int
    #: Both derived from the legs rather than read from the feed's own totals,
    #: so they can never disagree with a bar drawn from the same legs.
    walk_seconds: int
    wait_seconds: int
    #: The feed's own figure: it measures the street geometry more finely than
    #: the per-leg distances do, so this one is not derivable.
    walk_distance: int
    legs: list[Leg]

    @property
    def rides(self) -> list[Leg]:
        """Return the transit legs, in order."""
        return [leg for leg in self.legs if leg.transit]

    @property
    def transfers(self) -> int:
        """Return how many times the traveller changes vehicle.

        The feed has no transfers field, so it is one fewer than the number of
        rides, floored at zero for a plan that never boards anything.
        """
        return max(0, len(self.rides) - 1)

    @property
    def first_ride(self) -> Leg | None:
        """Return the first transit leg, or ``None`` if nothing is boarded."""
        return next(iter(self.rides), None)

    @property
    def routes(self) -> list[str]:
        """Return the line numbers ridden, in order, repeats kept."""
        return [ride.route_short_name for ride in self.rides if ride.route_short_name]

    @property
    def modes(self) -> list[str]:
        """Return the modes ridden, in order, repeats kept."""
        return [ride.mode for ride in self.rides]


@dataclass(slots=True)
class PlanOptions:
    """Routing preferences, in the units the user sets them in.

    Grouped rather than passed one by one so the planning call stays within a
    sane number of arguments. Speeds are km/h here and become metres per second
    only where the request is built.
    """

    walk_speed_kmh: float = DEFAULT_WALK_SPEED
    bike_speed_kmh: float = DEFAULT_BIKE_SPEED
    bike_optimize: str = DEFAULT_BIKE_OPTIMIZE


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

#: One aliased lookup per trip, since the feed has no "these trips" field: only
#: ``trip(id:)`` one at a time, which GraphQL is happy to batch into one request.
_RIDE_FIELDS = "stoptimes { stop { gtfsId } scheduledDeparture scheduledArrival }"


def _rides_query(count: int) -> str:
    """Return a query fetching the stop times of ``count`` trips at once."""
    declarations = ", ".join(f"$id{index}: String!" for index in range(count))
    lookups = " ".join(
        f"t{index}: trip(id: $id{index}) {{ {_RIDE_FIELDS} }}" for index in range(count)
    )
    return f"query Rides({declarations}) {{ {lookups} }}"


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


#: A leg's own fields. ``duration`` is deliberately not requested: it is a
#: float of the same span the two timestamps already describe, and deriving it
#: from them is what makes the legs of an itinerary tile it exactly. Legs carry
#: no headsign of their own, so it comes from the trip.
_LEG_FIELDS = """
      mode
      startTime
      endTime
      distance
      realTime
      from { name stop { gtfsId code } }
      to { name stop { gtfsId code } }
      route { shortName longName mode color textColor }
      trip { gtfsId tripHeadsign }
"""

#: No date or time is sent, so the feed plans from now, which is what a live
#: board wants. ``arriveBy`` with a time is the obvious later extension.
PLAN_QUERY = f"""
query Plan(
  $fromLat: Float!
  $fromLon: Float!
  $toLat: Float!
  $toLon: Float!
  $count: Int!
  $modes: [TransportMode]
  $walkSpeed: Float
  $bikeSpeed: Float
  $optimize: OptimizeType
) {{
  plan(
    from: {{lat: $fromLat, lon: $fromLon}}
    to: {{lat: $toLat, lon: $toLon}}
    numItineraries: $count
    transportModes: $modes
    walkSpeed: $walkSpeed
    bikeSpeed: $bikeSpeed
    optimize: $optimize
    omitCanceled: true
  ) {{
    itineraries {{
      startTime
      endTime
      walkDistance
      legs {{ {_LEG_FIELDS} }}
    }}
  }}
}}
"""

#: Punctuation and underscores are operators to the Lucene query parser behind
#: ``stops(name:)``, not characters to match, so the name-search fallback
#: replaces them with the spaces the parser treats as term separators.
_QUERY_OPERATORS = re.compile(r"[\W_]+", re.UNICODE)

#: Six hex digits and nothing else. The feed publishes route colours bare and
#: lower case ("de2c42"), and their text colours upper case ("FFFFFF").
_HEX_COLOR = re.compile(r"[0-9a-fA-F]{6}")

#: Metres per second per km/h, the only place the two units meet.
_KMH_TO_MS = 3.6


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


def _feature_id(raw: dict[str, Any]) -> str:
    """Return a geocoder feature's own ID, e.g. ``GTFS:estonia:952#04401-1``."""
    return str((raw.get("properties") or {}).get("id") or "")


def _feature_stop_name(raw: dict[str, Any]) -> str:
    """Return a geocoder feature's stop name without its platform code.

    Features are named "Järve 06804-1": the stop name with the same platform
    code that the feature's ID carries after the fragment marker. Trimming it
    off leaves the name the platforms of one stop share.
    """
    name = str((raw.get("properties") or {}).get("name") or "")
    _, _, code = _feature_id(raw).partition("#")
    if code and name.endswith(f" {code}"):
        return name[: -len(code) - 1]
    return name


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


def _ride_seconds(
    stoptimes: list[dict[str, Any]], origin_id: str, destination_id: str
) -> int | None:
    """Return the scheduled seconds from ``origin_id`` to ``destination_id``.

    A trip can call at the same stop twice, so the destination is looked for
    after the origin rather than anywhere on the trip, matching how patterns
    are matched to a destination.
    """
    departure: int | None = None
    for stoptime in stoptimes:
        gtfs_id = (stoptime.get("stop") or {}).get("gtfsId")
        if departure is None:
            if gtfs_id == origin_id:
                departure = stoptime.get("scheduledDeparture")
        elif gtfs_id == destination_id:
            arrival = stoptime.get("scheduledArrival")
            if arrival is None:
                return None
            return arrival - departure
    return None


def _color(raw: str | None) -> str | None:
    """Return a route colour as ``#rrggbb``, or ``None`` if it has none.

    A value the feed spells some other way is dropped rather than passed
    through, because the only thing that ever reads it is a stylesheet.
    """
    if not raw:
        return None
    value = raw.strip().removeprefix("#")
    if not _HEX_COLOR.fullmatch(value):
        return None
    return f"#{value.lower()}"


def _leg_mode(raw: dict[str, Any], route: dict[str, Any]) -> str:
    """Return the classified mode of one planned leg.

    Rides are classified the same way a departure is, so a trolleybus the feed
    calls a bus is named the same on both kinds of board. The two ways of
    moving under your own power are named by the plan itself.
    """
    if route:
        classified = _classify_mode(
            route.get("mode") or raw.get("mode"), route.get("longName")
        )
        if classified is not None:
            return classified
    mode = str(raw.get("mode") or "").upper()
    return MODE_BICYCLE if mode == "BICYCLE" else MODE_WALK


def _parse_leg(raw: dict[str, Any]) -> Leg | None:
    """Convert one raw leg into a :class:`Leg`, or ``None`` if it has no span."""
    start, end = raw.get("startTime"), raw.get("endTime")
    if start is None or end is None:
        return None

    route = raw.get("route") or {}
    trip = raw.get("trip") or {}
    origin = raw.get("from") or {}
    destination = raw.get("to") or {}
    origin_stop = origin.get("stop") or {}
    destination_stop = destination.get("stop") or {}
    distance = raw.get("distance")

    start_timestamp = start // 1000
    end_timestamp = end // 1000
    return Leg(
        mode=_leg_mode(raw, route),
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        duration=max(0, end_timestamp - start_timestamp),
        distance=round(distance) if distance is not None else None,
        from_name=origin.get("name"),
        from_stop_id=origin_stop.get("gtfsId"),
        from_stop_code=origin_stop.get("code"),
        to_name=destination.get("name"),
        to_stop_id=destination_stop.get("gtfsId"),
        to_stop_code=destination_stop.get("code"),
        route_short_name=route.get("shortName"),
        route_long_name=route.get("longName"),
        headsign=trip.get("tripHeadsign"),
        trip_id=trip.get("gtfsId"),
        color=_color(route.get("color")),
        text_color=_color(route.get("textColor")),
        realtime=bool(raw.get("realTime")),
    )


def _wait(start_timestamp: int, end_timestamp: int) -> Leg:
    """Return a waiting leg covering the time between two other legs."""
    return Leg(
        mode=MODE_WAIT,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        duration=end_timestamp - start_timestamp,
        distance=None,
        from_name=None,
        from_stop_id=None,
        from_stop_code=None,
        to_name=None,
        to_stop_id=None,
        to_stop_code=None,
        route_short_name=None,
        route_long_name=None,
        headsign=None,
        trip_id=None,
        color=None,
        text_color=None,
        realtime=False,
    )


def _fill_gaps(legs: list[Leg], start: int, end: int) -> list[Leg]:
    """Return ``legs`` with the time between them filled by waiting legs.

    A plan with a transfer does not account for all of its own span: in a
    measured example the legs covered 3650 of 4291 seconds, the missing 641
    being exactly the waiting the feed reports as one per-itinerary total. That
    total cannot be split back across two transfers, so the gaps are rebuilt
    where they actually fall instead. Legs that already touch produce nothing,
    and legs that overlap are left alone rather than given a negative wait.
    """
    filled: list[Leg] = []
    previous = start
    for leg in legs:
        if leg.start_timestamp > previous:
            filled.append(_wait(previous, leg.start_timestamp))
        filled.append(leg)
        previous = max(previous, leg.end_timestamp)
    if end > previous:
        filled.append(_wait(previous, end))
    return filled


def _absorb(leg: Leg, walk: Leg, *, leading: bool) -> None:
    """Stretch ``leg`` over an access ``walk`` beside it."""
    if leading:
        leg.start_timestamp = walk.start_timestamp
        leg.from_name = walk.from_name
        leg.from_stop_id = walk.from_stop_id
        leg.from_stop_code = walk.from_stop_code
    else:
        leg.end_timestamp = walk.end_timestamp
        leg.to_name = walk.to_name
        leg.to_stop_id = walk.to_stop_id
        leg.to_stop_code = walk.to_stop_code
    leg.duration = leg.end_timestamp - leg.start_timestamp
    if leg.distance is not None and walk.distance is not None:
        leg.distance += walk.distance


def _merge_access_walks(legs: list[Leg]) -> list[Leg]:
    """Absorb a few seconds of walking at either end into the leg beside it.

    Only ever into a walk or a ride of your own, never into a service. Merging
    an access walk into a bus would move the boarding stop to "Origin" and pull
    the departure time a few seconds earlier than the bus actually leaves —
    both of which a board states as fact. Cycling has no such claim to spoil,
    and it is where the stray segment actually shows up.

    No time is discarded: the surviving leg is stretched over the walk, so the
    legs still tile the itinerary exactly.
    """
    merged = list(legs)
    if (
        len(merged) > 1
        and merged[0].mode == MODE_WALK
        and merged[0].duration < MIN_ACCESS_WALK
        and not merged[1].transit
    ):
        _absorb(merged[1], merged.pop(0), leading=True)
    if (
        len(merged) > 1
        and merged[-1].mode == MODE_WALK
        and merged[-1].duration < MIN_ACCESS_WALK
        and not merged[-2].transit
    ):
        _absorb(merged[-2], merged.pop(), leading=False)
    return merged


def _parse_itinerary(raw: dict[str, Any]) -> Itinerary | None:
    """Convert one raw itinerary into an :class:`Itinerary`.

    Returns ``None`` for a plan with no usable legs, which a board cannot show.
    """
    start, end = raw.get("startTime"), raw.get("endTime")
    if start is None or end is None:
        return None
    start_timestamp, end_timestamp = start // 1000, end // 1000

    parsed = [leg for raw_leg in raw.get("legs") or [] if (leg := _parse_leg(raw_leg))]
    if not parsed:
        return None
    parsed.sort(key=lambda leg: leg.start_timestamp)
    legs = _fill_gaps(_merge_access_walks(parsed), start_timestamp, end_timestamp)

    walk_distance = raw.get("walkDistance") or 0
    return Itinerary(
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        duration=max(0, end_timestamp - start_timestamp),
        walk_seconds=sum(leg.duration for leg in legs if leg.mode == MODE_WALK),
        wait_seconds=sum(leg.duration for leg in legs if leg.mode == MODE_WAIT),
        walk_distance=round(walk_distance),
        legs=legs,
    )


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
            features = await self._async_geocode(name)
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

    async def _async_geocode(self, name: str) -> list[dict[str, Any]]:
        """Return the geocoder's stop features for a (partial) name.

        Both of the geocoder's endpoints are needed, because each is wrong on
        its own for a stop picker. ``autocomplete`` ranks partial input well but
        keeps only one platform per stop name and locality, which hides the rest
        of an interchange: searching "Järve" offers one of the four platforms in
        Tallinn, so the train to Paldiski is listed and the one back is not.
        ``search`` has every platform but pads its answer out with whatever else
        starts alike, burying them among names the user did not ask for.

        So the shortlist comes from ``autocomplete``, and ``search`` is used
        only to put back the platforms it collapsed: a stop is added when its
        name already appears in the shortlist, and never otherwise.
        """
        params = {"text": name, "size": SEARCH_LIMIT, "layers": "stop"}
        shortlist, complete = await asyncio.gather(
            self._get(f"{GEOCODER_URL}/autocomplete", params),
            self._get(f"{GEOCODER_URL}/search", params),
            return_exceptions=True,
        )
        if isinstance(shortlist, BaseException):
            raise shortlist

        features: list[dict[str, Any]] = shortlist.get("features") or []
        if isinstance(complete, BaseException):
            # A shortlist missing some platforms still beats no answer at all.
            _LOGGER.debug("Could not complete the geocoder shortlist: %s", complete)
            return features

        names = {_feature_stop_name(feature) for feature in features}
        seen = {_feature_id(feature) for feature in features}
        for feature in complete.get("features") or []:
            if (
                _feature_stop_name(feature) in names
                and _feature_id(feature) not in seen
            ):
                seen.add(_feature_id(feature))
                features.append(feature)
        return features

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

    async def async_get_ride_seconds(
        self, trip_ids: list[str], origin_id: str, destination_id: str
    ) -> dict[str, int]:
        """Return the scheduled ride seconds for each of ``trip_ids``.

        Only the scheduled times are read. They never change for a given trip,
        which is what lets the coordinator cache the answer rather than ask
        again on every poll; a trip running late still shows through, because
        the ride is measured from its realtime departure.
        """
        if not trip_ids:
            return {}
        data = await self._query(
            _rides_query(len(trip_ids)),
            {f"id{index}": trip_id for index, trip_id in enumerate(trip_ids)},
        )
        rides: dict[str, int] = {}
        for index, trip_id in enumerate(trip_ids):
            raw = data.get(f"t{index}")
            if raw is None:
                continue
            seconds = _ride_seconds(
                raw.get("stoptimes") or [], origin_id, destination_id
            )
            if seconds is not None:
                rides[trip_id] = seconds
        return rides

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

    async def _async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        count: int,
        transport_modes: list[dict[str, str]],
        options: PlanOptions,
    ) -> list[Itinerary]:
        """Plan journeys between two points for an explicit list of modes."""
        data = await self._query(
            PLAN_QUERY,
            {
                "fromLat": origin[0],
                "fromLon": origin[1],
                "toLat": destination[0],
                "toLon": destination[1],
                "count": count,
                "modes": transport_modes,
                # The only place km/h becomes the metres per second the feed
                # wants; everything else in the integration speaks km/h.
                "walkSpeed": options.walk_speed_kmh / _KMH_TO_MS,
                "bikeSpeed": options.bike_speed_kmh / _KMH_TO_MS,
                "optimize": options.bike_optimize.upper(),
            },
        )
        plan = data.get("plan") or {}
        itineraries = [
            itinerary
            for raw in plan.get("itineraries") or []
            if (itinerary := _parse_itinerary(raw))
        ]
        # Near-identical plans are successive departures on the same line
        # rather than duplicates, so they are all kept; only the order is made
        # certain, the same way departures are.
        itineraries.sort(key=lambda itinerary: itinerary.start_timestamp)
        return itineraries

    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        count: int,
        modes: list[str],
        options: PlanOptions,
    ) -> list[Itinerary]:
        """Plan door-to-door journeys by public transport between two points.

        ``modes`` are this integration's own mode names. They are translated to
        the feed's enum, which has no trolleybus, so asking for trolleybuses
        asks for buses and the caller separates the two afterwards.
        """
        asked = sorted({PLAN_MODES[mode] for mode in modes if mode in PLAN_MODES})
        transport_modes = [{"mode": "WALK"}]
        # An empty or unrecognised selection would otherwise plan a walk, so
        # fall back to every service the feed knows.
        transport_modes += [{"mode": mode} for mode in asked] or [{"mode": "TRANSIT"}]
        return await self._async_plan(
            origin, destination, count, transport_modes, options
        )

    async def async_plan_walk(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        options: PlanOptions,
    ) -> Itinerary | None:
        """Plan the walk between two points, if one is possible.

        Only ever one answer: walking somewhere is a single route rather than a
        choice between departures.
        """
        planned = await self._async_plan(
            origin, destination, 1, [{"mode": "WALK"}], options
        )
        return next(iter(planned), None)

    async def async_plan_bicycle(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        options: PlanOptions,
    ) -> Itinerary | None:
        """Plan the ride between two points, if one is possible."""
        planned = await self._async_plan(
            origin, destination, 1, [{"mode": "BICYCLE"}], options
        )
        return next(iter(planned), None)
