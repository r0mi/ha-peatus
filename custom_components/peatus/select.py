"""Whether a journey board plans from now, to leave at a time, or to arrive by one."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import TIME_MODES, TIME_NOW
from .coordinator import PeatusConfigEntry, PeatusJourneyCoordinator
from .entity import PeatusJourneyControl


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PeatusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the time mode of a journey board; a stop board has none."""
    coordinator = entry.runtime_data
    if isinstance(coordinator, PeatusJourneyCoordinator):
        async_add_entities([PeatusTimeModeSelect(coordinator)])


class PeatusTimeModeSelect(PeatusJourneyControl, SelectEntity):
    """What the board's chosen time means, if anything."""

    _attr_options = TIME_MODES

    def __init__(self, coordinator: PeatusJourneyCoordinator) -> None:
        """Initialise the select for one journey board."""
        super().__init__(coordinator, "select", "time_mode")

    @property
    def current_option(self) -> str:
        """Return the mode the board is set to."""
        return self.coordinator.time_mode

    async def async_added_to_hass(self) -> None:
        """Restore the last mode, re-planning if it changes the board."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in TIME_MODES and last.state != TIME_NOW:
            self.coordinator.time_mode = last.state
            await self.coordinator.async_replan_if_moved()

    async def async_select_option(self, option: str) -> None:
        """Choose a new mode and re-plan for it."""
        self.coordinator.time_mode = option
        self.async_write_ha_state()
        await self.coordinator.async_replan_if_moved()
