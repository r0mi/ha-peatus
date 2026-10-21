"""The time a journey board plans for, when it is not planning from now."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import PeatusConfigEntry, PeatusJourneyCoordinator
from .entity import PeatusJourneyControl


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PeatusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the chosen time of a journey board; a stop board has none."""
    coordinator = entry.runtime_data
    if isinstance(coordinator, PeatusJourneyCoordinator):
        async_add_entities([PeatusTimeDateTime(coordinator)])


class PeatusTimeDateTime(PeatusJourneyControl, DateTimeEntity):
    """When to leave, or when to be there by, as the time mode says.

    Starts unset, which plans from now whatever the mode, and is kept after it
    passes: the board falls back to now on its own, and the value is still
    there to be moved to the next day.
    """

    def __init__(self, coordinator: PeatusJourneyCoordinator) -> None:
        """Initialise the time for one journey board."""
        super().__init__(coordinator, "datetime", "time")

    @property
    def native_value(self) -> datetime | None:
        """Return the chosen time, if one has been set."""
        return self.coordinator.chosen_time

    async def async_added_to_hass(self) -> None:
        """Restore the last chosen time, re-planning if it changes the board."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        if (restored := dt_util.parse_datetime(last.state)) is not None:
            self.coordinator.chosen_time = restored
            await self.coordinator.async_replan_if_moved()

    async def async_set_value(self, value: datetime) -> None:
        """Choose a new time and re-plan for it."""
        self.coordinator.chosen_time = value
        self.async_write_ha_state()
        await self.coordinator.async_replan_if_moved()
