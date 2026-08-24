"""Buttons that act on the oldest pending approval.

Multiple devices can be pending approval at once; these always act on the
oldest unresolved one. Use the push notification directly for any other
pending device - it's unaffected by these buttons either way.
"""
from __future__ import annotations

import time

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WifiWatchCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WifiWatchCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WifiWatchDecideButton(coordinator, entry, "allow_save", "Allow + Save", "allow"),
            WifiWatchDecideButton(coordinator, entry, "approve_once", "Approve Once", "approve"),
            WifiWatchDecideButton(coordinator, entry, "deny", "Deny", "deny"),
        ]
    )


class WifiWatchDecideButton(CoordinatorEntity[WifiWatchCoordinator], ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str, name: str, action: str):
        super().__init__(coordinator)
        self._action = action
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_decide_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    def _oldest_pending_token(self) -> str | None:
        now = time.time()
        pending = [
            (t, v)
            for t, v in self.coordinator.data.get("tokens", {}).items()
            if not v.get("consumed") and v["expires"] > now
        ]
        if not pending:
            return None
        return min(pending, key=lambda item: item[1]["created"])[0]

    async def async_press(self) -> None:
        token = self._oldest_pending_token()
        if token is None:
            return
        await self.coordinator.async_handle_action(token, self._action)
