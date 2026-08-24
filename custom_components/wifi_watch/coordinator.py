"""Coordinator: owns the poll loop, pending-approval tokens, and the
allowlist/denied/history state. Thin orchestration shell over logic.py
(pure decisions) and unifi_api.py (I/O) - see those modules for the actual
dedup/debounce/retention rules and UniFi calls.
"""
from __future__ import annotations

import logging
import secrets
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import logic
from .const import (
    DEFAULT_AUTO_BLOCK,
    DEFAULT_NOTIFY_DEBOUNCE_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RETENTION_WINDOW_SECONDS,
    DEFAULT_TOKEN_EXPIRE_SECONDS,
    DOMAIN,
    OPT_AUTO_BLOCK,
    OPT_NOTIFY_DEBOUNCE_SECONDS,
    OPT_POLL_INTERVAL_SECONDS,
    OPT_RETENTION_WINDOW_SECONDS,
    OPT_TOKEN_EXPIRE_SECONDS,
    STORAGE_VERSION,
)
from .unifi_api import UnifiApiError, UnifiAuthError, UnifiClient

_LOGGER = logging.getLogger(__name__)


def _default_state() -> dict:
    return {
        "allowlist": {},
        "denied": {},
        "notified": {},
        "tokens": {},
        "history": [],
        "seen_connections": {},
        "last_notify": {},
        "last_poll_epoch": None,
    }


class WifiWatchCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unifi: UnifiClient) -> None:
        self._entry = entry
        self._unifi = unifi
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"wifi_watch_{entry.entry_id}")
        self._state: dict = _default_state()
        self._first_run = True
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # set from options after loading state in async_config_entry_first_refresh
        )

    def _opt(self, key: str, default):
        return self._entry.options.get(key, default)

    async def async_load(self) -> None:
        """Load persisted state before the first poll. Guarantees every
        top-level key from _default_state() is present even if the stored
        data predates that key, so adding a new field later can't KeyError.
        Does NOT deep-merge nested structure, only top-level keys."""
        stored = await self._store.async_load() or {}
        merged = _default_state()
        for key in merged:
            if key in stored:
                merged[key] = stored[key]
        self._state = merged
        self._first_run = merged["last_poll_epoch"] is None

        from datetime import timedelta

        self.update_interval = timedelta(seconds=self._opt(OPT_POLL_INTERVAL_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS))

    def _save_soon(self) -> None:
        """Debounced save - we're on a poll loop, saving on every mutation
        would hammer disk I/O for no benefit."""
        self._store.async_delay_save(lambda: self._state, 5)

    async def _async_update_data(self) -> dict:
        try:
            wireless = await self._unifi.get_wireless_clients()
        except UnifiAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except UnifiApiError as err:
            raise UpdateFailed(str(err)) from err

        now = time.time()
        allowlist = self._state["allowlist"]
        seen_connections = self._state["seen_connections"]

        for c in wireless:
            mac = c.get("macAddress", "").lower()
            if not mac or mac in allowlist:
                continue
            connected_at = c.get("connectedAt")
            if not connected_at:
                continue
            try:
                connected_epoch = logic.parse_connected_at(connected_at)
            except ValueError:
                continue

            if self._first_run:
                # Trust whatever's already on the network at setup time -
                # otherwise every one of these devices would look genuinely
                # new the first time it reconnects (WiFi toggle, sleep/wake).
                seen_connections[mac] = {"connected_epoch": connected_epoch, "last_seen": now}
                allowlist[mac] = c.get("name") or "(unnamed)"
                continue

            if not logic.is_new_session(seen_connections, mac, connected_epoch):
                seen_connections.setdefault(mac, {})["last_seen"] = now
                continue

            await self._handle_new_client(c, mac, now)
            seen_connections[mac] = {"connected_epoch": connected_epoch, "last_seen": now}

        if self._first_run:
            _LOGGER.info(
                "first run - baseline established, %d wireless clients seen and pre-allowlisted, no notifications",
                len(wireless),
            )
            self._first_run = False

        self._state["last_poll_epoch"] = now
        self._state["tokens"] = {
            t: v for t, v in self._state["tokens"].items() if v["expires"] > now and not v.get("consumed")
        }
        retention = self._opt(OPT_RETENTION_WINDOW_SECONDS, DEFAULT_RETENTION_WINDOW_SECONDS)
        self._state["notified"] = logic.prune_flat_by_age(self._state["notified"], now, retention)
        self._state["denied"] = logic.prune_by_age(self._state["denied"], now, retention, "time")
        self._state["seen_connections"] = logic.prune_by_age(seen_connections, now, retention, "last_seen")
        self._state["last_notify"] = logic.prune_by_age(self._state["last_notify"], now, retention, "time")
        self._save_soon()

        return self._state

    async def _handle_new_client(self, client: dict, mac: str, now: float) -> None:
        name = client.get("name") or "(unnamed)"
        ip = client.get("ipAddress", "?")
        randomized = logic.is_randomized_mac(mac)
        ssid, vendor = await self._unifi.get_client_essid_and_vendor(mac)

        for tok in self._state["tokens"].values():
            if tok["mac"] == mac and not tok["consumed"]:
                _LOGGER.info("suppressing duplicate notify mac=%s - decision already pending", mac)
                return

        debounce = self._opt(OPT_NOTIFY_DEBOUNCE_SECONDS, DEFAULT_NOTIFY_DEBOUNCE_SECONDS)
        if logic.should_suppress_notify(self._state["last_notify"], mac, ssid, now, debounce):
            _LOGGER.info("suppressing duplicate notify mac=%s ssid=%s - within %ds debounce", mac, ssid, debounce)
            return

        auto_block = self._opt(OPT_AUTO_BLOCK, DEFAULT_AUTO_BLOCK)
        blocked_ok = False
        if auto_block:
            try:
                blocked_ok = await self._unifi.block_client(mac)
            except UnifiApiError as err:
                _LOGGER.error("auto-block failed mac=%s: %s", mac, err)

        token = secrets.token_urlsafe(24)
        token_expire = self._opt(OPT_TOKEN_EXPIRE_SECONDS, DEFAULT_TOKEN_EXPIRE_SECONDS)
        self._state["last_notify"][mac] = {"ssid": ssid, "time": now}
        self._state["tokens"][token] = {
            "mac": mac,
            "name": name,
            "ip": ip,
            "ssid": ssid,
            "vendor": vendor,
            "randomized": randomized,
            "created": now,
            "expires": now + token_expire,
            "consumed": False,
            "blocked": blocked_ok,
        }
        self._state["notified"][mac] = now
        action = (
            "blocked (pending review)"
            if blocked_ok
            else "block FAILED (pending review)" if auto_block else "notified (pending review)"
        )
        self._state["history"] = logic.record_history(self._state["history"], mac, name, action, now)

        _LOGGER.info(
            "NEW CLIENT mac=%s name=%s ip=%s ssid=%s vendor=%s randomized=%s blocked=%s",
            mac, name, ip, ssid, vendor, randomized, blocked_ok,
        )
        await self.async_notify_new_client(name, mac, ip, ssid, vendor, randomized, auto_block, blocked_ok, token)

    async def async_notify_new_client(
        self, name: str, mac: str, ip: str, ssid: str | None, vendor: str | None, randomized: bool, auto_block: bool, blocked_ok: bool, token: str
    ) -> None:
        """Sends the actionable push. Overridden by __init__.py's wiring to
        the real notify.* service call - kept as a coordinator method (not
        inlined) so it's the one seam a test can stub out."""
        raise NotImplementedError  # replaced by __init__.py at setup

    async def async_handle_action(self, token: str, action: str) -> None:
        """action: "allow" | "approve" | "deny". Mirrors wifi_watch.py's
        /action endpoint handler - only consumes the token once the actual
        UniFi call succeeds, so a failed unblock/block leaves it usable for
        retry instead of silently stranding the device."""
        entry = self._state["tokens"].get(token)
        if not entry or entry.get("consumed") or entry["expires"] < time.time():
            _LOGGER.warning("action=%s for invalid/expired/consumed token", action)
            return

        now = time.time()
        if action in ("allow", "approve"):
            ok = True
            if entry.get("blocked"):
                try:
                    ok = await self._unifi.unblock_client(entry["mac"])
                except UnifiApiError as err:
                    _LOGGER.error("unblock failed mac=%s: %s", entry["mac"], err)
                    ok = False
            if not ok:
                _LOGGER.info("UNBLOCK FAILED (%s) mac=%s - token left usable for retry", action, entry["mac"])
                return
            entry["consumed"] = True
            if action == "allow":
                self._state["allowlist"][entry["mac"]] = entry["name"]
                hist_action = "unblocked + allowed" if entry.get("blocked") else "allowed"
                _LOGGER.info(
                    "ALLOWLISTED mac=%s name=%s unblocked=%s", entry["mac"], entry["name"], entry.get("blocked", False)
                )
            else:
                hist_action = "unblocked once" if entry.get("blocked") else "approved once"
                _LOGGER.info(
                    "APPROVED-ONCE mac=%s name=%s unblocked=%s", entry["mac"], entry["name"], entry.get("blocked", False)
                )
            self._state["history"] = logic.record_history(self._state["history"], entry["mac"], entry["name"], hist_action, now)
        elif action == "deny":
            try:
                ok = await self._unifi.block_client(entry["mac"])
            except UnifiApiError as err:
                _LOGGER.error("block failed mac=%s: %s", entry["mac"], err)
                ok = False
            if not ok:
                _LOGGER.info("DENY FAILED mac=%s - token left usable for retry", entry["mac"])
                return
            entry["consumed"] = True
            self._state["denied"][entry["mac"]] = {"name": entry["name"], "time": now, "blocked": True}
            hist_action = "kept blocked" if entry.get("blocked") else "denied/blocked"
            self._state["history"] = logic.record_history(self._state["history"], entry["mac"], entry["name"], hist_action, now)
            _LOGGER.info("DENIED/BLOCKED mac=%s name=%s", entry["mac"], entry["name"])
        else:
            _LOGGER.warning("unknown action=%s", action)
            return

        self._save_soon()
        self.async_set_updated_data(self._state)

    async def async_allowlist_remove(self, mac: str) -> bool:
        """Removes a MAC from the allowlist and clears its session/notify
        state, so a device that's mid-session when removed doesn't keep
        silently matching its old connected_epoch and never re-prompt."""
        mac = mac.lower().strip()
        removed = self._state["allowlist"].pop(mac, None)
        if removed is None:
            return False
        self._state["seen_connections"].pop(mac, None)
        self._state["last_notify"].pop(mac, None)
        self._save_soon()
        self.async_set_updated_data(self._state)
        _LOGGER.info("allowlist remove: mac=%s name=%s", mac, removed)
        return True

    async def async_denied_remove(self, mac: str, allowlist_after: bool) -> bool:
        """Un-denies a currently-blocked device: unblocks it, and either
        allowlists it (allowlist_after=True, "I denied that by accident")
        or leaves it unknown so it gets a fresh approval prompt next time
        (allowlist_after=False). Requires the MAC to actually be blocked
        right now per UniFi - ground truth, not just a self._state["denied"]
        entry, since auto_block can block a device with no such entry."""
        mac = mac.lower().strip()
        try:
            blocked = await self._unifi.list_blocked()
        except UnifiApiError as err:
            _LOGGER.error("denied-remove blocked-list query failed mac=%s: %s", mac, err)
            return False
        client = next((c for c in blocked if c.get("mac", "").lower() == mac), None)
        if not client:
            return False
        try:
            ok = await self._unifi.unblock_client(mac)
        except UnifiApiError as err:
            _LOGGER.error("denied-remove unblock failed mac=%s: %s", mac, err)
            ok = False
        if not ok:
            return False

        now = time.time()
        meta = self._state["denied"].pop(mac, {})
        name = meta.get("name") or client.get("hostname") or client.get("name") or mac
        if allowlist_after:
            self._state["allowlist"][mac] = name
            hist_action = "un-denied + allowed"
        else:
            self._state["last_notify"].pop(mac, None)
            self._state["seen_connections"].pop(mac, None)
            hist_action = "removed from blocked list (unblocked, not allowlisted)"
        self._state["history"] = logic.record_history(self._state["history"], mac, name, hist_action, now)
        self._save_soon()
        self.async_set_updated_data(self._state)
        _LOGGER.info("%s: mac=%s name=%s", hist_action, mac, name)
        return True

    async def async_remove_data(self) -> None:
        """Called from async_remove_entry - deletes persisted state on
        uninstall instead of leaving an orphaned storage file behind."""
        await self._store.async_remove()
