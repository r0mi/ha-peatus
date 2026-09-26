"""Tests for the Peatus.ee API client."""

from __future__ import annotations

import pytest
from aiohttp import ClientError

from custom_components.peatus.api import (
    PeatusApi,
    PeatusApiError,
    PlanOptions,
    _classify_mode,
    _color,
    _feature_stop_name,
    _gtfs_id_from_feature,
    _ride_seconds,
    route_sort_key,
)


class FakeResponse:
    """A minimal stand-in for an aiohttp response."""

    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    """Records what the client requests and replays canned answers."""

    def __init__(self) -> None:
        # The geocoder's two endpoints answer separately: autocomplete ranks
        # the shortlist, search fills the platforms it collapsed back in.
        self.autocomplete_response = {"features": []}
        self.search_response = {"features": []}
        self.graphql_response = {"data": {}}
        self.get_params = None
        self.get_urls: list[str] = []
        self.posted = None

    async def get(self, url, params=None, timeout=None):  # noqa: ASYNC109
        self.get_params = params
        self.get_urls.append(url)
        response = (
            self.search_response
            if url.endswith("/search")
            else self.autocomplete_response
        )
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)

    async def post(self, url, json=None, headers=None, timeout=None):  # noqa: ASYNC109
        self.posted = json
        if isinstance(self.graphql_response, Exception):
            raise self.graphql_response
        return FakeResponse(self.graphql_response)


@pytest.fixture(name="session")
def session_fixture() -> FakeSession:
    return FakeSession()


@pytest.fixture(name="api")
def api_fixture(session: FakeSession) -> PeatusApi:
    return PeatusApi(session)


@pytest.mark.parametrize(
    ("mode", "long_name", "expected"),
    [
        # Tallinn publishes trolleybus routes as buses, marked only in the name.
        ("BUS", "Mustamäe - Kopli (trollibuss)", "trolleybus"),
        ("BUS", "Keskuse - Balti jaam (trollibuss)", "trolleybus"),
        ("BUS", "MUSTAMÄE - KOPLI (TROLLIBUSS)", "trolleybus"),
        # Ordinary buses stay buses.
        ("BUS", "Vana-Pääsküla - Viru - Viimsi", "bus"),
        ("BUS", None, "bus"),
        # Other modes are never reclassified.
        ("TRAM", "Tondi - Kadriorg", "tram"),
        ("RAIL", "Tallinn - Tartu", "rail"),
        (None, "Something (trollibuss)", None),
    ],
)
def test_classify_mode(mode, long_name, expected) -> None:
    """Trolleybus routes are separated out of the buses by their long name."""
    assert _classify_mode(mode, long_name) == expected


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [("TRAM", "tram"), ("RAIL", "rail"), (None, None)],
)
def test_parse_stop_lowercases_vehicle_mode(raw_mode, expected) -> None:
    """The feed's upper case vehicleMode is normalised on the way in."""
    stop = PeatusApi._parse_stop(
        {"gtfsId": "estonia:1292", "name": "Balti jaam", "vehicleMode": raw_mode}
    )
    assert stop.vehicle_mode == expected


def _feature(gtfs_id: str, code: str, locality: str | None = "Tallinna linn, Nõmme"):
    """Build a geocoder stop feature the way Pelias returns one."""
    return {
        "properties": {
            "id": f"GTFS:{gtfs_id}#{code}",
            "layer": "stop",
            "locality": locality,
        }
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The feed's own ID sits between the source tag and the platform code.
        (_feature("estonia:952", "04401-1"), "estonia:952"),
        ({"properties": {"id": "GTFS:estonia:952", "layer": "stop"}}, "estonia:952"),
        # Addresses and other layers share the index but are not stops.
        ({"properties": {"id": "GTFS:estonia:952#1", "layer": "address"}}, None),
        # Anything not sourced from the GTFS feed has no ID this feed knows.
        ({"properties": {"id": "whosonfirst:locality:l:1", "layer": "stop"}}, None),
        ({"properties": {"id": "", "layer": "stop"}}, None),
        ({"properties": {"layer": "stop"}}, None),
        ({}, None),
    ],
)
def test_gtfs_id_from_feature(raw, expected) -> None:
    """A stop's feed ID is recovered from the geocoder's composite ID."""
    assert _gtfs_id_from_feature(raw) == expected


def test_label_includes_locality() -> None:
    """The locality disambiguates the many stops sharing a name."""
    stop = PeatusApi._parse_stop(
        {"gtfsId": "estonia:34041", "name": "Kohtla-Nõmme", "code": "4490009-1"},
        locality="Ida-Virumaa, Kohtla-Nõmme alev",
    )
    assert stop.label.startswith(
        "Kohtla-Nõmme (4490009-1) · Ida-Virumaa, Kohtla-Nõmme alev"
    )


async def test_search_stops_uses_geocoder(api, session) -> None:
    """Stops are searched through the geocoder and hydrated from the feed."""
    session.autocomplete_response = {
        "features": [_feature("estonia:952", "04401-1"), _feature("estonia:953", "x")]
    }
    session.graphql_response = {
        "data": {
            "stops": [
                {"gtfsId": "estonia:952", "name": "Vana-Pääsküla", "code": "04401-1"},
                {"gtfsId": "estonia:953", "name": "Vana-Pääsküla", "code": "04402-1"},
            ]
        }
    }

    stops = await api.async_search_stops("Vana-Pääsküla")

    assert [stop.gtfs_id for stop in stops] == ["estonia:952", "estonia:953"]
    # The hyphen reaches the geocoder untouched; only stop features are asked for.
    assert session.get_params["text"] == "Vana-Pääsküla"
    assert session.get_params["layers"] == "stop"
    assert session.posted["variables"]["ids"] == ["estonia:952", "estonia:953"]
    assert stops[0].locality == "Tallinna linn, Nõmme"


async def test_search_stops_skips_unknown_and_duplicate_ids(api, session) -> None:
    """A stop the geocoder lists but the feed does not know is dropped."""
    session.autocomplete_response = {
        "features": [
            _feature("estonia:952", "04401-1"),
            # The same stop indexed a second time under another name.
            _feature("estonia:952", "04401-1", locality="Ignored"),
            _feature("estonia:1", "gone"),
            {"properties": {"id": "GTFS:estonia:2#addr", "layer": "address"}},
        ]
    }
    session.graphql_response = {
        "data": {
            "stops": [
                {"gtfsId": "estonia:952", "name": "Vana-Pääsküla"},
                None,
            ]
        }
    }

    stops = await api.async_search_stops("Vana-Pääsküla")

    assert [stop.gtfs_id for stop in stops] == ["estonia:952"]
    assert session.posted["variables"]["ids"] == ["estonia:952", "estonia:1"]
    # The first hit wins, since the geocoder answers by descending relevance.
    assert stops[0].locality == "Tallinna linn, Nõmme"


@pytest.mark.parametrize(
    "autocomplete_outcome",
    [
        # The geocoder is indexed separately from the feed, so an empty answer
        # is worth a second try rather than reported as "no such stop".
        {"features": []},
        {},
        ClientError("down"),
    ],
)
async def test_search_stops_falls_back_to_name_search(
    api, session, autocomplete_outcome
) -> None:
    """Without a geocoder answer the feed's own name index is used instead."""
    session.autocomplete_response = autocomplete_outcome
    session.graphql_response = {
        "data": {"stops": [{"gtfsId": "estonia:952", "name": "Vana-Pääsküla"}]}
    }

    stops = await api.async_search_stops("Vana-Pääsküla")

    assert [stop.gtfs_id for stop in stops] == ["estonia:952"]
    # Punctuation is an operator to the Lucene parser behind stops(name:) — a
    # hyphen reads as NOT — so the query is reduced to bare terms.
    assert session.posted["variables"]["name"] == "Vana Pääsküla"


async def test_search_stops_skips_name_search_without_terms(api, session) -> None:
    """A query of pure punctuation has nothing left to search the feed with."""
    session.autocomplete_response = {"features": []}

    assert await api.async_search_stops(" -- ") == []
    assert session.posted is None


def test_route_sort_key_orders_like_a_timetable() -> None:
    """Route numbers sort numerically, with any prefix or suffix as text."""
    routes = ["119", "1", "18V", "10", "T3", "18", "104B", "104A", "2", "S12", "Expr"]
    assert sorted(routes, key=route_sort_key) == [
        "1",
        "2",
        "10",
        "18",
        "18V",
        "104A",
        "104B",
        "119",
        "S12",
        "T3",
        # Routes with no number at all come last.
        "Expr",
    ]


def test_parse_stop_orders_routes_naturally() -> None:
    """A stop's route list is ordered for the picker label, not as text."""
    stop = PeatusApi._parse_stop(
        {
            "gtfsId": "estonia:952",
            "name": "Vana-Pääsküla",
            "routes": [{"shortName": n} for n in ("191", "18", "1", "119", "10")],
        }
    )
    assert stop.routes == ["1", "10", "18", "119", "191"]


def _named_feature(gtfs_id: str, name: str, code: str):
    """Build a stop feature named the way the geocoder names one."""
    return {
        "properties": {
            "id": f"GTFS:{gtfs_id}#{code}",
            "layer": "stop",
            "name": f"{name} {code}",
            "locality": "Tallinna linn, Kristiine",
        }
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_named_feature("estonia:10416", "Järve", "06804-1"), "Järve"),
        # A stop with no code is named by itself.
        (
            {"properties": {"id": "GTFS:estonia:1", "name": "Järve"}},
            "Järve",
        ),
        # A name that merely ends in something code-shaped is left alone.
        (
            {"properties": {"id": "GTFS:estonia:1#06804-1", "name": "Balti jaam 2"}},
            "Balti jaam 2",
        ),
        ({}, ""),
    ],
)
def test_feature_stop_name(raw, expected) -> None:
    """The platform code the feature's ID carries is trimmed off its name."""
    assert _feature_stop_name(raw) == expected


async def test_search_recovers_platforms_autocomplete_collapsed(api, session) -> None:
    """Sibling platforms hidden by the shortlist are put back.

    Autocomplete keeps one platform per stop name and locality, so searching
    "Järve" offers the train towards Paldiski but not the one back.
    """
    session.autocomplete_response = {
        "features": [_named_feature("estonia:10415", "Järve", "06805-1")]
    }
    session.search_response = {
        "features": [
            _named_feature("estonia:10415", "Järve", "06805-1"),
            _named_feature("estonia:10416", "Järve", "06804-1"),
            _named_feature("estonia:1053", "Järve", "06801-1"),
            # A different stop that merely starts alike is not asked for.
            _named_feature("estonia:999", "Järveküla", "12345-1"),
        ]
    }
    session.graphql_response = {
        "data": {
            "stops": [
                {"gtfsId": "estonia:10415", "name": "Järve"},
                {"gtfsId": "estonia:10416", "name": "Järve"},
                {"gtfsId": "estonia:1053", "name": "Järve"},
            ]
        }
    }

    stops = await api.async_search_stops("Järve")

    assert [stop.gtfs_id for stop in stops] == [
        "estonia:10415",
        "estonia:10416",
        "estonia:1053",
    ]
    # Both endpoints are asked, and the shortlist still leads.
    assert sorted(url.rsplit("/", 1)[-1] for url in session.get_urls) == [
        "autocomplete",
        "search",
    ]
    assert "estonia:999" not in session.posted["variables"]["ids"]


async def test_search_keeps_shortlist_when_completion_fails(api, session) -> None:
    """A shortlist missing platforms still beats failing the whole search."""
    session.autocomplete_response = {
        "features": [_named_feature("estonia:10415", "Järve", "06805-1")]
    }
    session.search_response = ClientError("down")
    session.graphql_response = {
        "data": {"stops": [{"gtfsId": "estonia:10415", "name": "Järve"}]}
    }

    stops = await api.async_search_stops("Järve")

    assert [stop.gtfs_id for stop in stops] == ["estonia:10415"]


@pytest.mark.parametrize(
    ("stoptimes", "expected"),
    [
        # The ordinary case: depart the origin, arrive at the destination.
        (
            [
                {"stop": {"gtfsId": "estonia:A"}, "scheduledDeparture": 100},
                {"stop": {"gtfsId": "estonia:B"}, "scheduledArrival": 940},
            ],
            840,
        ),
        # Stops before the origin are not where the ride starts.
        (
            [
                {"stop": {"gtfsId": "estonia:X"}, "scheduledDeparture": 0},
                {"stop": {"gtfsId": "estonia:A"}, "scheduledDeparture": 100},
                {"stop": {"gtfsId": "estonia:B"}, "scheduledArrival": 940},
            ],
            840,
        ),
        # A loop route calls at the destination before the origin as well; the
        # ride is the leg after boarding, not the one before it.
        (
            [
                {"stop": {"gtfsId": "estonia:B"}, "scheduledArrival": 10},
                {"stop": {"gtfsId": "estonia:A"}, "scheduledDeparture": 100},
                {"stop": {"gtfsId": "estonia:B"}, "scheduledArrival": 940},
            ],
            840,
        ),
        # A trip that never reaches the destination has no ride length.
        ([{"stop": {"gtfsId": "estonia:A"}, "scheduledDeparture": 100}], None),
        ([], None),
        # Times the feed leaves out cannot be turned into a duration.
        (
            [
                {"stop": {"gtfsId": "estonia:A"}, "scheduledDeparture": 100},
                {"stop": {"gtfsId": "estonia:B"}},
            ],
            None,
        ),
    ],
)
def test_ride_seconds(stoptimes, expected) -> None:
    """The ride is measured from the origin to the destination after it."""
    assert _ride_seconds(stoptimes, "estonia:A", "estonia:B") == expected


async def test_get_ride_seconds_batches_trips(api, session) -> None:
    """Every trip is looked up in one request, and unknown ones are skipped."""
    session.graphql_response = {
        "data": {
            "t0": {
                "stoptimes": [
                    {"stop": {"gtfsId": "estonia:A"}, "scheduledDeparture": 100},
                    {"stop": {"gtfsId": "estonia:B"}, "scheduledArrival": 940},
                ]
            },
            # The feed does not know this trip any more.
            "t1": None,
        }
    }

    rides = await api.async_get_ride_seconds(
        ["trip:1", "trip:2"], "estonia:A", "estonia:B"
    )

    assert rides == {"trip:1": 840}
    assert session.posted["variables"] == {"id0": "trip:1", "id1": "trip:2"}
    # One aliased lookup per trip, since the feed has no "these trips" field.
    assert "t0: trip(id: $id0)" in session.posted["query"]
    assert "t1: trip(id: $id1)" in session.posted["query"]


async def test_get_ride_seconds_without_trips_asks_nothing(api, session) -> None:
    """An empty board makes no request at all."""
    assert await api.async_get_ride_seconds([], "estonia:A", "estonia:B") == {}
    assert session.posted is None


# --- Journey planning -------------------------------------------------------


def _plan_leg(mode, start, end, route=None, **kwargs):
    """Build one raw leg of a plan, in the shape the feed publishes."""
    leg = {
        "mode": mode,
        "startTime": start * 1000,
        "endTime": end * 1000,
        "distance": 1234.56,
        "realTime": False,
        "from": {"name": "Vana-Pääsküla", "stop": None},
        "to": {"name": "Järve", "stop": None},
        "route": None,
        "trip": None,
    }
    if route is not None:
        leg["route"] = {
            "shortName": route,
            "longName": "Viru keskus - Urda",
            "mode": mode,
            "color": "de2c42",
            "textColor": "FFFFFF",
        }
        leg["trip"] = {"gtfsId": "estonia:17206", "tripHeadsign": "Viru keskus"}
        leg["from"]["stop"] = {"gtfsId": "estonia:952", "code": "04401-1"}
    return {**leg, **kwargs}


def _plan(itineraries):
    """Wrap raw itineraries in the envelope the feed returns."""
    return {"data": {"plan": {"itineraries": itineraries}}}


def _itinerary(legs, walk_distance=796):
    """Build one raw itinerary spanning its legs."""
    return {
        "startTime": min(leg["startTime"] for leg in legs),
        "endTime": max(leg["endTime"] for leg in legs),
        "walkDistance": walk_distance,
        "legs": legs,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("de2c42", "#de2c42"),
        ("FFFFFF", "#ffffff"),
        ("#abc123", "#abc123"),
        (None, None),
        ("", None),
        ("red", None),
        ("12345", None),
    ],
)
async def test_color_normalises_to_css(raw, expected) -> None:
    """Route colours reach the frontend as CSS, or not at all."""
    assert _color(raw) == expected


async def test_plan_parses_a_single_ride(api, session) -> None:
    """A walk, a ride and a walk come back classified and in seconds."""
    session.graphql_response = _plan(
        [
            _itinerary(
                [
                    _plan_leg("WALK", 1000, 1300),
                    _plan_leg("BUS", 1300, 2200, route="18"),
                    _plan_leg("WALK", 2200, 2500),
                ]
            )
        ]
    )

    itineraries = await api.async_plan(
        (59.35, 24.63), (59.39, 24.72), 3, ["bus"], PlanOptions()
    )

    assert len(itineraries) == 1
    itinerary = itineraries[0]
    # Milliseconds on the wire, seconds everywhere in the integration.
    assert itinerary.start_timestamp == 1000
    assert itinerary.end_timestamp == 2500
    assert [leg.mode for leg in itinerary.legs] == ["walk", "bus", "walk"]
    ride = itinerary.first_ride
    assert ride.route_short_name == "18"
    # Legs carry no headsign of their own; it comes off the trip.
    assert ride.headsign == "Viru keskus"
    assert ride.color == "#de2c42"
    assert ride.text_color == "#ffffff"
    assert ride.from_stop_code == "04401-1"
    assert itinerary.transfers == 0
    assert itinerary.routes == ["18"]


async def test_plan_legs_tile_the_itinerary(api, session) -> None:
    """The legs of a plan account for every second of it."""
    session.graphql_response = _plan(
        [
            _itinerary(
                [
                    _plan_leg("WALK", 1000, 1300),
                    _plan_leg("BUS", 1300, 2200, route="18"),
                    _plan_leg("WALK", 2200, 2500),
                ]
            )
        ]
    )

    itinerary = (
        await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, ["bus"], PlanOptions())
    )[0]

    assert sum(leg.duration for leg in itinerary.legs) == itinerary.duration


async def test_plan_fills_a_transfer_gap_with_a_wait(api, session) -> None:
    """Waiting for a connection becomes a leg of its own, where it falls.

    The feed reports waiting only as one per-itinerary total, which cannot be
    split back across transfers, so the gap is rebuilt where it actually is.
    """
    session.graphql_response = _plan(
        [
            _itinerary(
                [
                    _plan_leg("WALK", 1000, 1300),
                    _plan_leg("BUS", 1300, 2200, route="18"),
                    _plan_leg("WALK", 2200, 2320),
                    # 641 seconds nothing accounts for: the measured gap.
                    _plan_leg("BUS", 2961, 3900, route="34"),
                    _plan_leg("WALK", 3900, 4100),
                ]
            )
        ]
    )

    itinerary = (
        await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, ["bus"], PlanOptions())
    )[0]

    assert [leg.mode for leg in itinerary.legs] == [
        "walk",
        "bus",
        "walk",
        "wait",
        "bus",
        "walk",
    ]
    wait = itinerary.legs[3]
    assert wait.duration == 641
    assert wait.route_short_name is None
    assert itinerary.wait_seconds == 641
    assert itinerary.transfers == 1
    # The whole point of the wait leg: the plan still tiles.
    assert sum(leg.duration for leg in itinerary.legs) == itinerary.duration


async def test_plan_fills_each_gap_separately(api, session) -> None:
    """Two transfers produce two waits, which one total could never describe."""
    session.graphql_response = _plan(
        [
            _itinerary(
                [
                    _plan_leg("BUS", 1000, 1300, route="18"),
                    _plan_leg("BUS", 1400, 1700, route="34"),
                    _plan_leg("BUS", 1900, 2200, route="55"),
                ]
            )
        ]
    )

    itinerary = (
        await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, ["bus"], PlanOptions())
    )[0]

    waits = [leg.duration for leg in itinerary.legs if leg.mode == "wait"]
    assert waits == [100, 200]
    assert sum(leg.duration for leg in itinerary.legs) == itinerary.duration


async def test_plan_adds_no_wait_when_legs_touch(api, session) -> None:
    """A plan with no waiting gets no waiting legs."""
    session.graphql_response = _plan(
        [
            _itinerary(
                [
                    _plan_leg("WALK", 1000, 1300),
                    _plan_leg("BUS", 1300, 2200, route="18"),
                ]
            )
        ]
    )

    itinerary = (
        await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, ["bus"], PlanOptions())
    )[0]

    assert [leg.mode for leg in itinerary.legs] == ["walk", "bus"]


async def test_plan_clamps_overlapping_legs(api, session) -> None:
    """Legs that overlap produce no negative wait."""
    session.graphql_response = _plan(
        [
            _itinerary(
                [
                    _plan_leg("BUS", 1000, 1600, route="18"),
                    _plan_leg("BUS", 1400, 2000, route="34"),
                ]
            )
        ]
    )

    itinerary = (
        await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, ["bus"], PlanOptions())
    )[0]

    assert [leg.mode for leg in itinerary.legs] == ["bus", "bus"]
    assert all(leg.duration >= 0 for leg in itinerary.legs)


async def test_plan_classifies_a_trolleybus_ride(api, session) -> None:
    """A trolleybus the feed calls a bus is named the same as on a stop board."""
    leg = _plan_leg("BUS", 1000, 2000, route="3")
    leg["route"]["longName"] = "Mustamäe - Kaubamaja (trollibuss)"
    session.graphql_response = _plan([_itinerary([leg])])

    itinerary = (
        await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, ["trolleybus"], PlanOptions())
    )[0]

    assert itinerary.modes == ["trolleybus"]


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        (["bus"], ["WALK", "BUS"]),
        # The feed has no trolleybus, so asking for one asks for a bus...
        (["trolleybus"], ["WALK", "BUS"]),
        # ...and asking for both must not ask for BUS twice.
        (["bus", "trolleybus"], ["WALK", "BUS"]),
        (["rail", "tram"], ["WALK", "RAIL", "TRAM"]),
        # Nothing recognised would otherwise plan a walk.
        ([], ["WALK", "TRANSIT"]),
    ],
)
async def test_plan_maps_modes_onto_the_feeds_enum(
    api, session, modes, expected
) -> None:
    """This integration's mode names are translated for the feed."""
    session.graphql_response = _plan([])

    await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, modes, PlanOptions())

    assert [mode["mode"] for mode in session.posted["variables"]["modes"]] == expected


async def test_plan_sends_speeds_in_metres_per_second(api, session) -> None:
    """Speeds are set in km/h and converted only where the request is built."""
    session.graphql_response = _plan([])

    await api.async_plan(
        (1.0, 2.0),
        (3.0, 4.0),
        3,
        ["bus"],
        PlanOptions(walk_speed_kmh=4.8, bike_speed_kmh=18.0, bike_optimize="flat"),
    )

    variables = session.posted["variables"]
    assert variables["walkSpeed"] == pytest.approx(1.3333, abs=0.0001)
    assert variables["bikeSpeed"] == pytest.approx(5.0)
    assert variables["optimize"] == "FLAT"


async def test_plan_orders_itineraries_by_departure(api, session) -> None:
    """Plans are ordered by when the traveller has to leave."""
    session.graphql_response = _plan(
        [
            _itinerary([_plan_leg("BUS", 3000, 3600, route="18")]),
            _itinerary([_plan_leg("BUS", 1000, 1600, route="18")]),
        ]
    )

    itineraries = await api.async_plan(
        (1.0, 2.0), (3.0, 4.0), 3, ["bus"], PlanOptions()
    )

    assert [itinerary.start_timestamp for itinerary in itineraries] == [1000, 3000]


async def test_plan_skips_itineraries_without_legs(api, session) -> None:
    """A plan with nothing in it is not a plan a board can show."""
    session.graphql_response = _plan([{"startTime": 1000, "endTime": 2000, "legs": []}])

    assert await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, ["bus"], PlanOptions()) == []


async def test_plan_walk_asks_only_to_walk(api, session) -> None:
    """The walking plan is a single answer, because walking is one route."""
    session.graphql_response = _plan([_itinerary([_plan_leg("WALK", 1000, 7000)])])

    itinerary = await api.async_plan_walk((1.0, 2.0), (3.0, 4.0), PlanOptions())

    assert [mode["mode"] for mode in session.posted["variables"]["modes"]] == ["WALK"]
    assert itinerary is not None
    assert [leg.mode for leg in itinerary.legs] == ["walk"]


async def test_plan_bicycle_keeps_the_walk_to_the_bike(api, session) -> None:
    """A cycling plan starts with the few steps to the bicycle."""
    session.graphql_response = _plan(
        [_itinerary([_plan_leg("WALK", 1000, 1038), _plan_leg("BICYCLE", 1038, 2880)])]
    )

    itinerary = await api.async_plan_bicycle((1.0, 2.0), (3.0, 4.0), PlanOptions())

    assert [mode["mode"] for mode in session.posted["variables"]["modes"]] == [
        "BICYCLE"
    ]
    assert [leg.mode for leg in itinerary.legs] == ["walk", "bicycle"]
    # Cycling is not a ride on a service, so it never counts as a transfer.
    assert itinerary.transfers == 0
    assert itinerary.rides == []


async def test_plan_walk_returns_none_when_unroutable(api, session) -> None:
    """Two places with no route between them simply have no plan."""
    session.graphql_response = _plan([])

    assert await api.async_plan_walk((1.0, 2.0), (3.0, 4.0), PlanOptions()) is None


async def test_plan_wraps_api_errors(api, session) -> None:
    """A failing plan raises the integration's own error."""
    session.graphql_response = {"errors": [{"message": "boom"}]}

    with pytest.raises(PeatusApiError):
        await api.async_plan((1.0, 2.0), (3.0, 4.0), 3, ["bus"], PlanOptions())
