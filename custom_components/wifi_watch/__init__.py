"""wifi-watch: get notified the moment an unrecognized device joins your
WiFi, and approve or block it from an actionable phone notification.
"""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import aiohttp_client, device_registry as dr, issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    ACTION_PREFIX,
    CONF_UNIFI_API_KEY,
    CONF_UNIFI_HOST,
    CONF_UNIFI_PASSWORD,
    CONF_UNIFI_SITE_ID,
    CONF_UNIFI_SITE_NAME,
    CONF_UNIFI_USERNAME,
    DEFAULT_EXCLUDED_NOTIFY_TARGETS,
    DOMAIN,
    EVENT_NOTIFICATION_ACTION,
    OPT_EXCLUDED_NOTIFY_TARGETS,
    STORAGE_VERSION,
)
from .coordinator import WifiWatchCoordinator
from .unifi_api import UnifiClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "select", "button"]


def _notify_targets(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Broadcasts to every actionable-capable notify target, dynamically -
    persistent_notification plus every paired phone's mobile_app_* service.
    No config needed by default: a phone paired to HA after setup starts
    getting notified immediately, nothing to go back and reconfigure.
    Excludes "send_message" and any other non-mobile_app/
    persistent_notification service (e.g. email, Slack) - those aren't
    guaranteed to render data.actions and could silently mangle the
    approval buttons. Individual targets can still be opted out via the
    excluded_notify_targets option."""
    services = hass.services.async_services().get("notify", {})
    excluded = set(entry.options.get(OPT_EXCLUDED_NOTIFY_TARGETS, DEFAULT_EXCLUDED_NOTIFY_TARGETS))
    return sorted(
        n for n in services if (n == "persistent_notification" or n.startswith("mobile_app_")) and n not in excluded
    )

SERVICE_ALLOWLIST_REMOVE = "allowlist_remove"
SERVICE_DENIED_REMOVE = "denied_remove"
SERVICE_TEST_NOTIFICATION = "test_notification"
SERVICE_DECIDE = "decide"

MAC_SCHEMA = vol.Schema({vol.Required("mac"): str})
DENIED_REMOVE_SCHEMA = vol.Schema({vol.Required("mac"): str, vol.Optional("allowlist", default=False): bool})
DECIDE_SCHEMA = vol.Schema({vol.Required("token"): str, vol.Required("action"): vol.In(["allow", "approve", "guest", "deny"])})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Dedicated session, not HA's shared one: the controller's self-signed
    # cert means verify_ssl=False, and we need a DummyCookieJar because
    # aiohttp's default jar silently drops cookies for IP-address hosts
    # (which is how UniFi controllers are usually addressed) - manual
    # Cookie-header management in unifi_api.py instead. Called from inside
    # async_setup_entry, async_create_clientsession reads the active config
    # entry from its own contextvar and self-registers cleanup (.detach(),
    # not .close()) on entry unload/reload automatically - do NOT also call
    # session.close() ourselves, HA's frame helper flags that as misuse.
    session = aiohttp_client.async_create_clientsession(
        hass, verify_ssl=False, cookie_jar=aiohttp.DummyCookieJar()
    )

    unifi = UnifiClient(
        session=session,
        host=entry.data[CONF_UNIFI_HOST],
        site_id=entry.data[CONF_UNIFI_SITE_ID],
        site_name=entry.data[CONF_UNIFI_SITE_NAME],
        api_key=entry.data[CONF_UNIFI_API_KEY],
        username=entry.data[CONF_UNIFI_USERNAME],
        password=entry.data[CONF_UNIFI_PASSWORD],
    )

    coordinator = WifiWatchCoordinator(hass, entry, unifi)

    async def _notify(name, mac, ip, ssid, vendor, randomized, auto_block, blocked_ok, token):
        tag = " [randomized/private MAC - may be a known device reconnecting]" if randomized else ""
        ssidline = f"\nSSID: {ssid}" if ssid else ""
        vendorline = f"\nVendor: {vendor}" if vendor else ""
        if auto_block:
            status = "BLOCKED - no network access until you act." if blocked_ok else "⚠️ Auto-block FAILED - device still has access, review needed."
            actions = [
                ("Unblock + allow", f"{ACTION_PREFIX}ALLOW::{token}"),
                ("Unblock once", f"{ACTION_PREFIX}APPROVE::{token}"),
                ("Keep blocked", f"{ACTION_PREFIX}DENY::{token}"),
            ]
        else:
            status = "Has full network access (notify-only mode - nothing is blocked unless you tap Deny)."
            actions = [
                ("Allow + save", f"{ACTION_PREFIX}ALLOW::{token}"),
                ("Approve once", f"{ACTION_PREFIX}APPROVE::{token}"),
                ("Deny (block)", f"{ACTION_PREFIX}DENY::{token}"),
            ]
        message = f"New WiFi client: {name}\nMAC: {mac}{tag}{vendorline}{ssidline}\nIP: {ip}\n{status}"

        # Bare legacy notify service names (e.g. "mobile_app_iphone"),
        # called directly - NOT the generic entity-based notify.send_message,
        # whose typed signature has no `data` parameter at all and would
        # silently drop the actions payload (confirmed against mobile_app's
        # own NotifyEntity implementation).
        for target in _notify_targets(hass, entry):
            await hass.services.async_call(
                "notify",
                target,
                {
                    "title": "New WiFi Client",
                    "message": message,
                    "data": {"actions": [{"action": a, "title": t} for t, a in actions], "push": {"sound": "default"}},
                },
            )

    coordinator.async_notify_new_client = _notify

    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Wi-Fi Watch",
        manufacturer="UniFi",
        model=entry.data[CONF_UNIFI_HOST],
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _on_notification_action(event) -> None:
        action = event.data.get("action", "")
        if not action.startswith(ACTION_PREFIX):
            return  # not ours - this event fires for every notification action in the whole instance
        try:
            _, verb, token = action.split("::", 2)
        except ValueError:
            _LOGGER.warning("malformed wifi-watch action string: %s", action)
            return
        verb_map = {"ALLOW": "allow", "APPROVE": "approve", "DENY": "deny"}
        if verb not in verb_map:
            _LOGGER.warning("unknown wifi-watch action verb: %s", verb)
            return
        await coordinator.async_handle_action(token, verb_map[verb])

    async def _svc_decide(call: ServiceCall) -> None:
        # Same entry point the notification-action buttons use - lets a
        # dashboard (or anything else) act on a pending token directly,
        # not just a push notification.
        await coordinator.async_handle_action(call.data["token"], call.data["action"])

    entry.async_on_unload(hass.bus.async_listen(EVENT_NOTIFICATION_ACTION, _on_notification_action))

    async def _svc_allowlist_remove(call: ServiceCall) -> None:
        await coordinator.async_allowlist_remove(call.data["mac"])

    async def _svc_denied_remove(call: ServiceCall) -> None:
        await coordinator.async_denied_remove(call.data["mac"], call.data["allowlist"])

    async def _svc_test_notification(call: ServiceCall) -> None:
        for target in _notify_targets(hass, entry):
            await hass.services.async_call(
                "notify", target, {"title": "Wi-Fi Watch test", "message": "This is a test notification from Wi-Fi Watch."}
            )

    hass.services.async_register(DOMAIN, SERVICE_ALLOWLIST_REMOVE, _svc_allowlist_remove, schema=MAC_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DENIED_REMOVE, _svc_denied_remove, schema=DENIED_REMOVE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_TEST_NOTIFICATION, _svc_test_notification)
    hass.services.async_register(DOMAIN, SERVICE_DECIDE, _svc_decide, schema=DECIDE_SCHEMA)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options changed (poll interval, debounce, etc.) - reload so the
    coordinator picks up the new update_interval and every option read via
    self._opt() reflects the new values."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        ir.async_delete_issue(hass, DOMAIN, f"update_failing_{entry.entry_id}")
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_ALLOWLIST_REMOVE)
            hass.services.async_remove(DOMAIN, SERVICE_DENIED_REMOVE)
            hass.services.async_remove(DOMAIN, SERVICE_TEST_NOTIFICATION)
            hass.services.async_remove(DOMAIN, SERVICE_DECIDE)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deletes persisted state so uninstalling doesn't leave an orphaned
    storage file behind."""
    store: Store = Store(hass, STORAGE_VERSION, f"wifi_watch_{entry.entry_id}")
    await store.async_remove()
