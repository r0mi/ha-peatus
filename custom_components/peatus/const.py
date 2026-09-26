"""Constants for the Peatus.ee public transport integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "peatus"

#: Estonian national Digitransit/OpenTripPlanner GraphQL endpoint used by web.peatus.ee.
API_URL: Final = "https://api.peatus.ee/routing/v1/routers/estonia/index/graphql"

#: Pelias geocoder behind web.peatus.ee's own stop search, without the endpoint.
#: Stops are looked up here rather than through OpenTripPlanner's ``stops(name:)``
#: field, which is Lucene backed: a hyphen in the query reads as a NOT operator,
#: so hyphenated Estonian stop names such as "Vana-Pääsküla" match nothing, and
#: it silently caps every result set at ten stops.
GEOCODER_URL: Final = "https://api.peatus.ee/geocoding/v1"

#: Most stops asked of the geocoder in one search. It returns only genuine
#: matches rather than padding to this size, so it is a ceiling, not a target.
SEARCH_LIMIT: Final = 40

#: Number of upcoming departure sensors created per configured stop.
NUM_DEPARTURES: Final = 10

#: Number of itinerary sensors created per configured journey. Fewer than the
#: departure board's ten: every itinerary carries a full list of legs, and
#: planning a door-to-door trip costs the API far more than reading a
#: timetable.
NUM_ITINERARIES: Final = 5

#: How far ahead to look for departures, in seconds. A full day keeps the
#: sensors populated overnight when the next service is hours away.
TIME_RANGE: Final = 86400

#: Default poll interval in minutes. Departure boards are useful at a glance
#: rather than second-by-second, so this trades a little staleness for far
#: fewer API calls; the user can still choose 1 minute.
DEFAULT_SCAN_INTERVAL: Final = 3
MIN_SCAN_INTERVAL: Final = 1
MAX_SCAN_INTERVAL: Final = 60

#: Default poll interval for a journey board, in minutes. Slower than a stop
#: board's: planning a trip is a routing search rather than a table read, and a
#: door-to-door plan does not change meaningfully from minute to minute.
DEFAULT_JOURNEY_SCAN_INTERVAL: Final = 5

#: Decimal places a tracked entity's coordinates are rounded to before a
#: journey is planned from them. Four places is about eleven metres: enough
#: that a phone's GPS drifting while it sits still cannot re-plan the walk and
#: rewrite every sensor on each poll, and far finer than the stop spacing that
#: actually decides a plan.
COORDINATE_PRECISION: Final = 4

CONF_STOP_ID: Final = "stop_id"
CONF_STOP_NAME: Final = "stop_name"
CONF_STOP_CODE: Final = "stop_code"
CONF_STOP_MODE: Final = "stop_mode"
CONF_STOP_DESC: Final = "stop_desc"
CONF_DESTINATION_ID: Final = "destination_id"
CONF_DESTINATION_NAME: Final = "destination_name"
CONF_MODES: Final = "modes"
CONF_ROUTES: Final = "routes"
CONF_SEARCH: Final = "search"

#: Which kind of board an entry configures. Entries created before journey
#: boards existed carry no such key, so every reader defaults to a stop board;
#: that is what makes a migration unnecessary and lets VERSION stay at 1.
CONF_BOARD: Final = "board"
BOARD_STOP: Final = "stop"
BOARD_JOURNEY: Final = "journey"

#: The two ends of a journey, each a zone or a tracked entity. The far end
#: reuses CONF_DESTINATION_NAME, which means the same thing on both boards.
CONF_ORIGIN_ENTITY: Final = "origin_entity"
CONF_ORIGIN_NAME: Final = "origin_name"
CONF_DESTINATION_ENTITY: Final = "destination_entity"

#: Journey routing preferences. Speeds are held in km/h, which is what the user
#: is shown and thinks in; they are converted to the metres per second the API
#: wants at a single boundary in ``api.py`` and nowhere else.
CONF_WALK_SPEED: Final = "walk_speed"
CONF_BIKE_SPEED: Final = "bike_speed"
CONF_BIKE_OPTIMIZE: Final = "bike_optimize"

#: The API's own defaults, restated in km/h: 1.33 m/s walking and 5 m/s cycling.
DEFAULT_WALK_SPEED: Final = 4.8
DEFAULT_BIKE_SPEED: Final = 18.0
MIN_SPEED: Final = 1.0
MAX_SPEED: Final = 40.0

#: Transport modes offered to the user. The feed publishes these upper case;
#: they are lower cased on the way in so the same strings can be used as
#: translation keys for the selector, which only allows [a-z0-9-_].
MODE_BUS: Final = "bus"
MODE_TROLLEYBUS: Final = "trolleybus"
MODE_TRAM: Final = "tram"
MODE_RAIL: Final = "rail"
MODE_FERRY: Final = "ferry"

SUPPORTED_MODES: Final = [
    MODE_BUS,
    MODE_TROLLEYBUS,
    MODE_TRAM,
    MODE_RAIL,
    MODE_FERRY,
]

#: Leg kinds that are not a ride on a service. Deliberately outside
#: SUPPORTED_MODES: they are never offered in the mode filter and never
#: filtered out of a plan.
MODE_WALK: Final = "walk"
MODE_BICYCLE: Final = "bicycle"
#: Time inside an itinerary that belongs to no leg at all — standing at a stop
#: waiting for a connection. The API publishes only a per-itinerary total, so
#: the gaps are reconstructed as legs of their own; see ``api.py``.
MODE_WAIT: Final = "wait"

#: Shortest walk at either end of a journey worth showing as a leg of its own,
#: in seconds. Planning from a point rather than a stop makes the API emit a
#: few steps onto the street network first — nineteen seconds from a pavement
#: to the corner of a car park, say. Nobody needs telling to do that, and drawn
#: as its own segment it takes far more of a bar than its share of the trip.
MIN_ACCESS_WALK: Final = 60

#: The API's mode for each mode this integration offers. Its enum has no
#: trolleybus, because the feed publishes trolleybus routes as buses, so both
#: ask for BUS and the two are told apart client side afterwards.
PLAN_MODES: Final = {
    MODE_BUS: "BUS",
    MODE_TROLLEYBUS: "BUS",
    MODE_TRAM: "TRAM",
    MODE_RAIL: "RAIL",
    MODE_FERRY: "FERRY",
}

#: Bicycle route preferences worth offering, lower cased so they double as
#: selector translation keys. The API also accepts TRIANGLE, which does nothing
#: without the separate weights argument, and TRANSFERS, which is a transit
#: notion rather than a cycling one.
BIKE_OPTIMIZE: Final = ["quick", "safe", "flat", "greenways"]
DEFAULT_BIKE_OPTIMIZE: Final = "quick"

#: Tallinn's (hybrid) trolleybuses are published as ordinary buses: the feed has
#: no GTFS route_type 11/800 anywhere, and the only marker is this word in the
#: route's long name, e.g. "Mustamäe - Kopli (trollibuss)". Trolleybus therefore
#: has to be derived rather than read from the route mode.
TROLLEYBUS_MARKER: Final = "trollibuss"

#: How long a resolved origin -> destination pattern set stays valid before it is
#: recomputed. Route patterns only change when the timetable changes.
PATTERN_CACHE_TTL: Final = 21600
