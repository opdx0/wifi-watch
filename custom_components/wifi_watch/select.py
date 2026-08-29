"""Select entities for removing a device from the allowlist or the denied
list, and for targeting the decide buttons at a specific pending device
when more than one is waiting at once - ship with the integration, no
dashboard required, and show up on the device's own page."""
from __future__ import annotations

import time

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import logic
from .const import DOMAIN
from .coordinator import WifiWatchCoordinator

PLACEHOLDER = "(pick a device to remove)"


def _label(name: str, mac: str) -> str:
    # Names are already cleaned at capture time (coordinator.py), but this
    # stays defensive for anything persisted before that existed and not
    # yet touched by async_load's one-time cleanup pass (e.g. a name typed
    # directly into storage by hand).
    return f"{logic.clean_device_name(name)} - {mac}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WifiWatchCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WifiWatchAllowlistSelect(coordinator, entry),
            WifiWatchDeniedSelect(coordinator, entry),
            WifiWatchPendingSelect(coordinator, entry),
        ]
    )


class _RemoveSelect(CoordinatorEntity[WifiWatchCoordinator], SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str, object_id: str, name: str, state_key: str):
        super().__init__(coordinator)
        self._state_key = state_key
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}
        self._object_id = object_id

    @property
    def suggested_object_id(self) -> str:
        # See button.py: overriding this pins entity_id independent of
        # _attr_name so a fresh install matches the README's hardcoded
        # dashboard YAML the same way an upgraded install does.
        return self._object_id

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
        # A MAC is always exactly 17 chars ("xx:xx:xx:xx:xx:xx") and always
        # the label's trailing suffix (see _label) - slicing is immune to a
        # device name containing " - ", unlike splitting on the separator.
        mac = option[-17:]
        await self._remove(mac)

    async def _remove(self, mac: str) -> None:
        raise NotImplementedError


class WifiWatchAllowlistSelect(_RemoveSelect):
    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator, entry, "allowlist_selection", "remove_from_allowlist", "Remove From Allowlist", "allowlist"
        )

    async def _remove(self, mac: str) -> None:
        await self.coordinator.async_allowlist_remove(mac)


class WifiWatchDeniedSelect(_RemoveSelect):
    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(
            coordinator,
            entry,
            "denied_selection",
            "remove_from_currently_blocked",
            "Remove From Currently Blocked",
            "denied",
        )

    async def _remove(self, mac: str) -> None:
        await self.coordinator.async_denied_remove(mac, False)


PENDING_PLACEHOLDER = "(none selected - buttons act on oldest)"


class WifiWatchPendingSelect(CoordinatorEntity[WifiWatchCoordinator], SelectEntity):
    """Points the four decide buttons (Approve + Save/Approve 24h/Approve
    Once/Deny) at a specific pending device when more than one is waiting - unlike the
    remove-selects above, this one is sticky: picking an option stays
    selected until a button consumes it, it expires, or it's no longer
    pending, at which point it silently reverts to the placeholder and the
    buttons resume acting on the oldest pending device (unchanged default
    behavior)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_name = "Pending Device"
        self._attr_unique_id = f"{entry.entry_id}_pending_selection"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def suggested_object_id(self) -> str:
        return "pending_device"

    def _pending(self) -> dict:
        now = time.time()
        tokens = self.coordinator.data.get("tokens", {})
        return {t: v for t, v in tokens.items() if not v.get("consumed") and v["expires"] > now}

    @property
    def options(self) -> list[str]:
        # Oldest first, not alphabetical - matches the fallback behavior
        # (buttons act on oldest when nothing's picked), so the top of the
        # list is always the one that's already the default.
        ordered = sorted(self._pending().values(), key=lambda v: v["created"])
        labels = [_label(v["name"], v["mac"]) for v in ordered]
        return [PENDING_PLACEHOLDER] + labels

    @property
    def current_option(self) -> str:
        token = self.coordinator.selected_pending_token()
        if token is None:
            return PENDING_PLACEHOLDER
        return _label(self.coordinator.data["tokens"][token]["name"], self.coordinator.data["tokens"][token]["mac"])

    async def async_select_option(self, option: str) -> None:
        if option == PENDING_PLACEHOLDER:
            await self.coordinator.async_set_selected_pending(None)
            return
        mac = option[-17:]
        await self.coordinator.async_set_selected_pending(mac)
