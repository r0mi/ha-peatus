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

#: How far ahead to look for departures, in seconds. A full day keeps the
#: sensors populated overnight when the next service is hours away.
TIME_RANGE: Final = 86400

#: Default poll interval in minutes. Departure boards are useful at a glance
#: rather than second-by-second, so this trades a little staleness for far
#: fewer API calls; the user can still choose 1 minute.
DEFAULT_SCAN_INTERVAL: Final = 3
MIN_SCAN_INTERVAL: Final = 1
MAX_SCAN_INTERVAL: Final = 60

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

#: Tallinn's (hybrid) trolleybuses are published as ordinary buses: the feed has
#: no GTFS route_type 11/800 anywhere, and the only marker is this word in the
#: route's long name, e.g. "Mustamäe - Kopli (trollibuss)". Trolleybus therefore
#: has to be derived rather than read from the route mode.
TROLLEYBUS_MARKER: Final = "trollibuss"

#: How long a resolved origin -> destination pattern set stays valid before it is
#: recomputed. Route patterns only change when the timetable changes.
PATTERN_CACHE_TTL: Final = 21600
