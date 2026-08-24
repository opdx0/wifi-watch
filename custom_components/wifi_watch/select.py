"""Native select entities for removing a device from the allowlist or the
denied list. Replaces the old dashboard package's template select entities
- these ship automatically with the integration via HACS, no YAML package
required, and show up on the device's own page with zero dashboard setup.
"""
from __future__ import annotations

import re

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WifiWatchCoordinator

PLACEHOLDER = "(pick a device to remove)"
# Some UniFi client names end in a trailing "-e8:f7"-style octet pair
# (its own default-hostname convention for unnamed devices) - stripped so
# the dropdown label reads as a clean name, matching the old template's
# regex_replace behavior.
_TRAILING_MAC_SUFFIX = re.compile(r" [0-9a-fA-F]{2}:[0-9a-fA-F]{2}$")


def _label(name: str, mac: str) -> str:
    return f"{_TRAILING_MAC_SUFFIX.sub('', name)} — {mac}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WifiWatchCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WifiWatchAllowlistSelect(coordinator, entry),
            WifiWatchDeniedSelect(coordinator, entry),
        ]
    )


class _RemoveSelect(CoordinatorEntity[WifiWatchCoordinator], SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str, name: str, state_key: str):
        super().__init__(coordinator)
        self._state_key = state_key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    def _items(self) -> dict:
        return self.coordinator.data.get(self._state_key, {})

    @property
    def options(self) -> list[str]:
        items = self._items()
        labels = sorted(
            (_label(v if isinstance(v, str) else v.get("name", mac), mac) for mac, v in items.items()),
            key=str.casefold,
        )
        return [PLACEHOLDER] + labels

    @property
    def current_option(self) -> str:
        # Always the placeholder - picking a real option acts immediately
        # (see async_select_option) rather than staying "selected", so the
        # coordinator push after removal naturally resets this back to the
        # placeholder along with everything else.
        return PLACEHOLDER

    async def async_select_option(self, option: str) -> None:
        if option == PLACEHOLDER:
            return
        mac = option.rsplit(" — ", 1)[-1]
        await self._remove(mac)

    async def _remove(self, mac: str) -> None:
        raise NotImplementedError


class WifiWatchAllowlistSelect(_RemoveSelect):
    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry, "allowlist_selection", "Remove From Allowlist", "allowlist")

    async def _remove(self, mac: str) -> None:
        await self.coordinator.async_allowlist_remove(mac)


class WifiWatchDeniedSelect(_RemoveSelect):
    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator, entry, "denied_selection", "Remove From Currently Blocked", "denied")

    async def _remove(self, mac: str) -> None:
        await self.coordinator.async_denied_remove(mac, False)
