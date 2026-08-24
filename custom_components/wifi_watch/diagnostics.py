"""Diagnostics support - redacts credentials and MACs before download."""
from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_UNIFI_API_KEY, CONF_UNIFI_PASSWORD, CONF_UNIFI_USERNAME, DOMAIN

TO_REDACT = {CONF_UNIFI_API_KEY, CONF_UNIFI_USERNAME, CONF_UNIFI_PASSWORD, "mac", "ip"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
        "state": async_redact_data(coordinator.data, TO_REDACT),
    }
