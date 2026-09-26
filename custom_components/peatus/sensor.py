"""Departure time sensors for the Peatus.ee integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .api import Departure, Itinerary, Leg
from .const import (
    CONF_DESTINATION_ENTITY,
    CONF_DESTINATION_NAME,
    CONF_ORIGIN_ENTITY,
    CONF_ORIGIN_NAME,
    CONF_STOP_CODE,
    CONF_STOP_DESC,
    CONF_STOP_ID,
    CONF_STOP_MODE,
    CONF_STOP_NAME,
    DOMAIN,
    MODE_BICYCLE,
    MODE_BUS,
    MODE_FERRY,
    MODE_RAIL,
    MODE_TRAM,
    MODE_TROLLEYBUS,
    MODE_WAIT,
    MODE_WALK,
    NUM_DEPARTURES,
    NUM_ITINERARIES,
)
from .coordinator import (
    PeatusConfigEntry,
    PeatusCoordinator,
    PeatusJourneyCoordinator,
)

#: Icon per vehicle mode for the numbered departure sensors. Trolleybuses share
#: the bus icon, as Material Design Icons has no trolleybus glyph.
#: Public page for a single stop on peatus.ee, used as the device link.
STOP_URL = "https://web.peatus.ee/pysakit/"

#: How each mode is named on the device card.
MODE_NOUNS = {
    MODE_BUS: "Bus",
    MODE_TROLLEYBUS: "Trolleybus",
    MODE_TRAM: "Tram",
    MODE_RAIL: "Train",
    MODE_FERRY: "Ferry",
}

MODE_ICONS = {
    MODE_BUS: "mdi:bus",
    MODE_TROLLEYBUS: "mdi:bus",
    MODE_TRAM: "mdi:tram",
    MODE_RAIL: "mdi:train",
    MODE_FERRY: "mdi:ferry",
    # The three ways of spending time that are not a ride on a service. They
    # share the map so a dashboard can icon any leg from one lookup.
    MODE_WALK: "mdi:walk",
    MODE_BICYCLE: "mdi:bike",
    MODE_WAIT: "mdi:timer-sand",
}


def _model(entry: PeatusConfigEntry) -> str:
    """Describe what the board covers.

    A board without a destination watches a stop ("Train stop Laagri"); one with
    a destination watches a route through it ("Train route Laagri → Järve").
    """
    noun = MODE_NOUNS.get(entry.data.get(CONF_STOP_MODE))
    stop = entry.data.get(CONF_STOP_NAME)
    destination = entry.data.get(CONF_DESTINATION_NAME)

    kind = "route" if destination else "stop"
    label = f"{noun} {kind}" if noun else kind.capitalize()
    if not stop:
        return label
    if destination:
        return f"{label} {stop} → {destination}"
    return f"{label} {stop}"


def _model_id(entry: PeatusConfigEntry) -> str | None:
    """Return the feed's own description of the stop.

    This is the direction text peatus.ee shows, in Estonian, e.g.
    "Rong Balti jaama suunas". Many stops have none, in which case the
    device simply shows no model ID.
    """
    return entry.data.get(CONF_STOP_DESC)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PeatusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors a board needs, which differ by the kind of board."""
    coordinator = entry.runtime_data

    if isinstance(coordinator, PeatusJourneyCoordinator):
        # No "next journey" sensor: consecutive itineraries can use different
        # lines and even different modes, so a single alias for "the next one"
        # would name something that keeps changing meaning.
        journey: list[SensorEntity] = [
            PeatusItinerarySensor(coordinator, index=index)
            for index in range(NUM_ITINERARIES)
        ]
        journey.append(PeatusFixedJourneySensor(coordinator, kind=MODE_WALK))
        journey.append(PeatusFixedJourneySensor(coordinator, kind=MODE_BICYCLE))
        async_add_entities(journey)
        return

    entities: list[PeatusDepartureSensor] = [
        PeatusDepartureSensor(coordinator, index=0, is_next=True)
    ]
    entities.extend(
        PeatusDepartureSensor(coordinator, index=index, is_next=False)
        for index in range(NUM_DEPARTURES)
    )
    async_add_entities(entities)


class PeatusDepartureSensor(CoordinatorEntity[PeatusCoordinator], SensorEntity):
    """A single upcoming departure, exposed as a timestamp sensor.

    The state is the departure time rather than a countdown, so Home Assistant
    renders the remaining minutes itself and the sensor only writes a new state
    when the departure actually changes.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coordinator: PeatusCoordinator, index: int, is_next: bool
    ) -> None:
        """Initialise the sensor for position ``index`` in the departure list."""
        super().__init__(coordinator)
        self._index = index
        self._is_next = is_next
        entry = coordinator.config_entry

        if is_next:
            self._attr_translation_key = "next_departure"
            self._attr_unique_id = f"{entry.entry_id}_next_departure"
            suffix = "next departure"
        else:
            self._attr_translation_key = "departure"
            self._attr_translation_placeholders = {"index": str(index + 1)}
            self._attr_unique_id = f"{entry.entry_id}_departure_{index + 1}"
            suffix = f"departure {index + 1}"

        # Entity IDs read peatus_<stop>[_<destination>]_departure_<n>. Setting
        # entity_id here is what stops Home Assistant prefixing the device name
        # (which would repeat the stop and its code). It is only a suggestion:
        # the registry appends _2, _3 ... if the ID is already taken, and any
        # rename a user makes later is preserved.
        self.entity_id = "sensor." + slugify(
            " ".join(
                part
                for part in (
                    DOMAIN,
                    entry.data.get(CONF_STOP_NAME),
                    entry.data.get(CONF_DESTINATION_NAME),
                    suffix,
                )
                if part
            )
        )

        # Home Assistant builds each friendly name as "<device name> <entity name>",
        # so the device is named after the stop alone: "Laagri Departure 1" rather
        # than repeating the stop code. The code stays on the device as its model,
        # and in the config entry title, where it disambiguates platforms.
        device_name = " → ".join(
            part
            for part in (
                entry.data.get(CONF_STOP_NAME),
                entry.data.get(CONF_DESTINATION_NAME),
            )
            if part
        )
        stop_id = entry.data.get(CONF_STOP_ID)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name or entry.title,
            manufacturer="peatus.ee",
            model=_model(entry),
            model_id=_model_id(entry),
            # No firmware or serial number: a stop is not a physical device, and
            # the integration's version is already shown on the integration page.
            # Links "Visit device" straight to this stop on peatus.ee.
            configuration_url=(
                f"{STOP_URL}{quote(stop_id, safe='')}" if stop_id else STOP_URL
            ),
        )

    @property
    def _departure(self) -> Departure | None:
        """Return the departure this sensor tracks, if it exists."""
        departures = self.coordinator.data or []
        if self._index < len(departures):
            return departures[self._index]
        return None

    @property
    def icon(self) -> str | None:
        """Return an icon matching the vehicle mode of this departure.

        Returning ``None`` leaves the icon unset on the state, which makes the
        frontend fall back to the static default in ``icons.json`` — the
        clock for "next departure", and a timetable when nothing is due.
        """
        if self._is_next or (departure := self._departure) is None:
            return None
        return MODE_ICONS.get(departure.mode)

    @property
    def native_value(self) -> datetime | None:
        """Return the departure time, realtime when the feed provides it."""
        if (departure := self._departure) is None:
            return None
        return dt_util.utc_from_timestamp(departure.timestamp)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return route details for the tracked departure.

        ``minutes_until`` is a convenience snapshot taken when the state is
        written; templates that need a live countdown should use the state.
        """
        entry = self.coordinator.config_entry
        attributes: dict[str, Any] = {
            "stop": entry.data.get(CONF_STOP_NAME),
            "stop_code": entry.data.get(CONF_STOP_CODE),
            "stop_id": entry.data.get(CONF_STOP_ID),
            "destination": entry.data.get(CONF_DESTINATION_NAME),
        }

        if (departure := self._departure) is None:
            return attributes

        scheduled = dt_util.utc_from_timestamp(departure.scheduled_timestamp)
        remaining = departure.timestamp - dt_util.utcnow().timestamp()

        attributes |= {
            "route": departure.route_short_name,
            "route_long_name": departure.route_long_name,
            "mode": departure.mode,
            "headsign": departure.headsign,
            "scheduled_departure": scheduled.isoformat(),
            "realtime": departure.realtime,
            "realtime_state": departure.realtime_state,
            "delay_seconds": departure.delay_seconds,
            "minutes_until": max(0, int(remaining // 60)),
            "trip_id": departure.trip_id,
        }

        if departure.ride_seconds is not None:
            arrival = dt_util.utc_from_timestamp(
                departure.timestamp + departure.ride_seconds
            )
            # The ride is scheduled, so a departure already running late carries
            # its delay over into the arrival rather than losing it.
            attributes |= {
                "ride_minutes": round(departure.ride_seconds / 60),
                "arrival_time": arrival.isoformat(),
            }
        return attributes


#: Entity ID suffix and translation key for each of the two fixed plans. The
#: bicycle one is called "ride" because that is what a person calls it.
FIXED_SUFFIXES = {MODE_WALK: "walk", MODE_BICYCLE: "ride"}


def _journey_model(entry: PeatusConfigEntry) -> str:
    """Describe what a journey board covers, for the device card."""
    origin = entry.data.get(CONF_ORIGIN_NAME)
    destination = entry.data.get(CONF_DESTINATION_NAME)
    if origin and destination:
        return f"Journey {origin} → {destination}"
    return "Journey"


def _leg_attributes(leg: Leg) -> dict[str, Any]:
    """Return one leg of a journey as plain JSON-safe values.

    ``duration`` is the authoritative figure: the legs of one itinerary sum to
    its own duration exactly, including the waits, which is what lets a card
    draw a bar straight from them. ``minutes`` is only for labelling, and is
    rounded, so it deliberately does not add up.
    """
    return {
        "mode": leg.mode,
        "start_time": dt_util.utc_from_timestamp(leg.start_timestamp).isoformat(),
        "end_time": dt_util.utc_from_timestamp(leg.end_timestamp).isoformat(),
        "duration": leg.duration,
        "minutes": round(leg.duration / 60),
        "route": leg.route_short_name,
        "route_long_name": leg.route_long_name,
        "headsign": leg.headsign,
        "color": leg.color,
        "text_color": leg.text_color,
        "from": leg.from_name,
        "from_stop_code": leg.from_stop_code,
        "from_stop_id": leg.from_stop_id,
        "to": leg.to_name,
        "to_stop_code": leg.to_stop_code,
        "to_stop_id": leg.to_stop_id,
        "distance": leg.distance,
        "trip_id": leg.trip_id,
        "realtime": leg.realtime,
    }


def _itinerary_attributes(itinerary: Itinerary) -> dict[str, Any]:
    """Return the details of one planned journey."""
    remaining = itinerary.start_timestamp - dt_util.utcnow().timestamp()
    attributes: dict[str, Any] = {
        # Carried as an attribute even though it is also the state of a journey
        # sensor: the walking and cycling sensors state a duration instead, and
        # one dashboard card should be able to read both the same way.
        "start_time": dt_util.utc_from_timestamp(itinerary.start_timestamp).isoformat(),
        "arrival_time": dt_util.utc_from_timestamp(itinerary.end_timestamp).isoformat(),
        "duration_seconds": itinerary.duration,
        "duration_minutes": round(itinerary.duration / 60),
        "walk_minutes": round(itinerary.walk_seconds / 60),
        "wait_minutes": round(itinerary.wait_seconds / 60),
        "walk_distance": itinerary.walk_distance,
        "transfers": itinerary.transfers,
        "routes": itinerary.routes,
        "modes": itinerary.modes,
        "minutes_until": max(0, int(remaining // 60)),
        "legs": [_leg_attributes(leg) for leg in itinerary.legs],
    }

    # Omitted rather than left empty on a plan that boards nothing: there is no
    # stop to leave from, so a null departure time would be a claim, not a gap.
    if (ride := itinerary.first_ride) is not None:
        attributes |= {
            "departure_time": dt_util.utc_from_timestamp(
                ride.start_timestamp
            ).isoformat(),
            "departure_stop": ride.from_name,
            "departure_stop_code": ride.from_stop_code,
            "departure_stop_id": ride.from_stop_id,
        }
    return attributes


class PeatusJourneySensor(CoordinatorEntity[PeatusJourneyCoordinator], SensorEntity):
    """Shared plumbing for the sensors of one journey board."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PeatusJourneyCoordinator, suffix: str) -> None:
        """Initialise the device and entity ID shared by every journey sensor."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        origin = entry.data.get(CONF_ORIGIN_NAME)
        destination = entry.data.get(CONF_DESTINATION_NAME)

        # Built from the names the places had when the board was made, never
        # from the tracker's own entity ID: "person.romi_agar" would make for a
        # dreadful entity ID, and the ID must not move if the tracker is
        # renamed later.
        self.entity_id = "sensor." + slugify(
            " ".join(part for part in (DOMAIN, origin, destination, suffix) if part)
        )

        device_name = " → ".join(part for part in (origin, destination) if part)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=device_name or entry.title,
            manufacturer="peatus.ee",
            model=_journey_model(entry),
            # Names which Home Assistant entities feed the board, the journey
            # equivalent of the feed's own direction text on a stop.
            model_id=" → ".join(
                part
                for part in (
                    entry.data.get(CONF_ORIGIN_ENTITY),
                    entry.data.get(CONF_DESTINATION_ENTITY),
                )
                if part
            )
            or None,
            configuration_url=STOP_URL,
        )

    @property
    def _base_attributes(self) -> dict[str, Any]:
        """Return the attributes every journey sensor carries, itinerary or not.

        Present even on an empty slot so a dashboard can bind to them without
        first testing whether the board found anything.
        """
        entry = self.coordinator.config_entry
        return {
            "origin": entry.data.get(CONF_ORIGIN_NAME),
            "origin_entity_id": entry.data.get(CONF_ORIGIN_ENTITY),
            "destination": entry.data.get(CONF_DESTINATION_NAME),
            "destination_entity_id": entry.data.get(CONF_DESTINATION_ENTITY),
            "legs": [],
        }


class PeatusItinerarySensor(PeatusJourneySensor):
    """One upcoming journey by public transport, as a timestamp sensor.

    The state is when you have to leave rather than a countdown, so Home
    Assistant renders the remaining minutes itself and the sensor only writes a
    new state when the journey actually changes.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PeatusJourneyCoordinator, index: int) -> None:
        """Initialise the sensor for position ``index`` in the journey list."""
        super().__init__(coordinator, suffix=f"journey {index + 1}")
        self._index = index
        self._attr_translation_key = "journey"
        self._attr_translation_placeholders = {"index": str(index + 1)}
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_journey_{index + 1}"
        )

    @property
    def _itinerary(self) -> Itinerary | None:
        """Return the journey this sensor tracks, if it exists."""
        data = self.coordinator.data
        if data is None or self._index >= len(data.itineraries):
            return None
        return data.itineraries[self._index]

    @property
    def icon(self) -> str | None:
        """Return an icon matching the first ride of this journey.

        A journey that boards nothing is a walk, and one that is not there at
        all leaves the icon to ``icons.json``.
        """
        if (itinerary := self._itinerary) is None:
            return None
        if (ride := itinerary.first_ride) is None:
            return MODE_ICONS[MODE_WALK]
        return MODE_ICONS.get(ride.mode)

    @property
    def native_value(self) -> datetime | None:
        """Return when the traveller has to set off."""
        if (itinerary := self._itinerary) is None:
            return None
        return dt_util.utc_from_timestamp(itinerary.start_timestamp)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the legs and totals of the tracked journey."""
        attributes = self._base_attributes
        if (itinerary := self._itinerary) is None:
            return attributes
        return attributes | _itinerary_attributes(itinerary)


class PeatusFixedJourneySensor(PeatusJourneySensor):
    """The walk or the ride between the two places, as a duration sensor.

    Unlike a journey by public transport these do not wait for a timetable:
    they start whenever the traveller does. A timestamp state would therefore
    only ever say "now" and would be rewritten on every poll, so the useful and
    stable figure is how long the trip takes.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: PeatusJourneyCoordinator, kind: str) -> None:
        """Initialise the sensor for one of the two fixed plans."""
        suffix = FIXED_SUFFIXES[kind]
        super().__init__(coordinator, suffix=suffix)
        self._kind = kind
        self._attr_translation_key = suffix
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{suffix}"

    @property
    def _itinerary(self) -> Itinerary | None:
        """Return the fixed plan this sensor tracks, if the feed found one."""
        if (data := self.coordinator.data) is None:
            return None
        return data.walk if self._kind == MODE_WALK else data.bicycle

    @property
    def native_value(self) -> int | None:
        """Return how long the trip takes, in minutes."""
        if (itinerary := self._itinerary) is None:
            return None
        return round(itinerary.duration / 60)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the legs and totals of the tracked plan."""
        attributes = self._base_attributes
        if (itinerary := self._itinerary) is None:
            return attributes
        return attributes | _itinerary_attributes(itinerary)
