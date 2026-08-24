"""Pending/allowlist/denied entities - the same three the manual dashboard
(../homeassistant/dashboard.yaml) reads today via REST sensors, now backed
by the coordinator directly."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WifiWatchCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: WifiWatchCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WifiWatchCountSensor(coordinator, entry, "pending_approvals", "Pending Approvals", "tokens", pending_only=True),
            WifiWatchCountSensor(coordinator, entry, "allowlist", "Allowlist", "allowlist"),
            WifiWatchCountSensor(coordinator, entry, "denied", "Denied", "denied"),
            WifiWatchHistorySensor(coordinator, entry),
        ]
    )


class WifiWatchCountSensor(CoordinatorEntity[WifiWatchCoordinator], SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str, name: str, state_key: str, pending_only: bool = False):
        super().__init__(coordinator)
        self._key = key
        self._state_key = state_key
        self._pending_only = pending_only
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    def _items(self) -> dict:
        data = self.coordinator.data.get(self._state_key, {})
        if self._pending_only:
            import time

            now = time.time()
            return {t: v for t, v in data.items() if not v.get("consumed") and v["expires"] > now}
        return data

    @property
    def native_value(self) -> int:
        return len(self._items())

    @property
    def extra_state_attributes(self) -> dict:
        items = self._items()
        if self._state_key == "tokens":
            return {"pending": [{"token": t, **v} for t, v in items.items()]}
        return {self._state_key: [{"mac": mac, **v} if isinstance(v, dict) else {"mac": mac, "name": v} for mac, v in items.items()]}


class WifiWatchHistorySensor(CoordinatorEntity[WifiWatchCoordinator], SensorEntity):
    """Recent approval/denial history - coordinator.py's self._state["history"]
    is a list, not a dict, so it needs its own entity rather than reusing
    WifiWatchCountSensor's dict-shaped _items()."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_name = "Recent Activity"
        self._attr_unique_id = f"{entry.entry_id}_history"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.get("history", []))

    @property
    def extra_state_attributes(self) -> dict:
        return {"history": self.coordinator.data.get("history", [])}
