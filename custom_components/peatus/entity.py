"""Shared plumbing for the controls of a journey board."""

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import slugify

from .const import CONF_DESTINATION_NAME, CONF_ORIGIN_NAME, DOMAIN
from .coordinator import PeatusJourneyCoordinator


class PeatusJourneyControl(RestoreEntity):
    """A setting of one journey board that a dashboard changes as it is used.

    Kept in the entity rather than the config entry: changing an entry reloads
    the board, and choosing a time should only re-plan it. The choice survives
    a restart by restoring the entity's last state.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(
        self, coordinator: PeatusJourneyCoordinator, platform: str, suffix: str
    ) -> None:
        """Attach the control to the board's device, named like its sensors."""
        self.coordinator = coordinator
        entry = coordinator.config_entry
        self._attr_translation_key = suffix
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self.entity_id = f"{platform}." + slugify(
            " ".join(
                part
                for part in (
                    DOMAIN,
                    entry.data.get(CONF_ORIGIN_NAME),
                    entry.data.get(CONF_DESTINATION_NAME),
                    suffix,
                )
                if part
            )
        )
        # The sensors describe the device; naming it once is enough to join it.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
