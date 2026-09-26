"""The Peatus.ee public transport integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import BOARD_JOURNEY
from .coordinator import (
    PeatusConfigEntry,
    PeatusCoordinator,
    PeatusJourneyCoordinator,
    board_type,
)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: PeatusConfigEntry) -> bool:
    """Set up a departure or journey board from a config entry."""
    coordinator: PeatusCoordinator | PeatusJourneyCoordinator
    if board_type(entry) == BOARD_JOURNEY:
        coordinator = PeatusJourneyCoordinator(hass, entry)
    else:
        coordinator = PeatusCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PeatusConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: PeatusConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
