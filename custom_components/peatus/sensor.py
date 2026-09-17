"""Departure time sensors for the Peatus.ee integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .api import Departure
from .const import (
    CONF_DESTINATION_NAME,
    CONF_STOP_CODE,
    CONF_STOP_DESC,
    CONF_STOP_ID,
    CONF_STOP_MODE,
    CONF_STOP_NAME,
    DOMAIN,
    MODE_BUS,
    MODE_FERRY,
    MODE_RAIL,
    MODE_TRAM,
    MODE_TROLLEYBUS,
    NUM_DEPARTURES,
)
from .coordinator import PeatusConfigEntry, PeatusCoordinator

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
    """Set up the "next departure" sensor plus the numbered departure sensors."""
    coordinator = entry.runtime_data

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
        return attributes
