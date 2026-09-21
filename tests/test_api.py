"""Tests for the Peatus.ee API client."""

from __future__ import annotations

import pytest
from aiohttp import ClientError

from custom_components.peatus.api import (
    PeatusApi,
    _classify_mode,
    _gtfs_id_from_feature,
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
        self.geocoder_response = {"features": []}
        self.graphql_response = {"data": {}}
        self.get_params = None
        self.posted = None

    async def get(self, url, params=None, timeout=None):  # noqa: ASYNC109
        self.get_params = params
        if isinstance(self.geocoder_response, Exception):
            raise self.geocoder_response
        return FakeResponse(self.geocoder_response)

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
    session.geocoder_response = {
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
    session.geocoder_response = {
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
    "geocoder_outcome",
    [
        # The geocoder is indexed separately from the feed, so an empty answer
        # is worth a second try rather than reported as "no such stop".
        {"features": []},
        {},
        ClientError("down"),
    ],
)
async def test_search_stops_falls_back_to_name_search(
    api, session, geocoder_outcome
) -> None:
    """Without a geocoder answer the feed's own name index is used instead."""
    session.geocoder_response = geocoder_outcome
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
    session.geocoder_response = {"features": []}

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
