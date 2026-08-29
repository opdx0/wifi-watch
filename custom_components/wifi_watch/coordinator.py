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
from homeassistant.helpers import issue_registry as ir
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

# How long update failures must persist before surfacing a Repairs issue -
# well above one bad poll (default poll interval is 7s, a single UniFi
# hiccup shouldn't page anyone), but well below the "several minutes of
# silent unavailability" that happened for real before this existed.
UPDATE_FAILING_THRESHOLD_SECONDS = 60

# Fixed, not an option - "trust for 24h" is the whole point of guest mode;
# making it configurable would just be a slower way to reach for auto_block
# or a permanent Allow + Save instead.
GUEST_TRUST_SECONDS = 24 * 60 * 60


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
        self._failing_since: float | None = None
        self._issue_id = f"update_failing_{entry.entry_id}"
        # Which pending device the "Pending Device" select currently points
        # the three decide buttons at - UI-only, deliberately not part of
        # self._state (not persisted, not pruned/saved to disk).
        self._selected_pending_mac: str | None = None
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
        Does NOT deep-merge nested structure, only top-level keys.

        allowlist entries are additionally normalized here to the current
        {"name", "first_seen", "last_seen"} dict shape - older storage (pre
        first/last-seen tracking) has plain "mac: name" strings, and this is
        the one place that needs to know that, so every other read site can
        assume dict shape unconditionally."""
        stored = await self._store.async_load() or {}
        merged = _default_state()
        for key in merged:
            if key in stored:
                merged[key] = stored[key]
        merged["allowlist"] = {
            mac: (v if isinstance(v, dict) else {"name": v, "first_seen": None, "last_seen": None})
            for mac, v in merged["allowlist"].items()
        }
        self._state = merged

        needs_backfill = [mac for mac, v in merged["allowlist"].items() if v.get("first_seen") is None]
        if needs_backfill:
            # One-time per device, not per poll - stat/alluser is a legacy
            # call, only worth paying for once to fill in history for
            # devices allowlisted before first_seen tracking existed.
            try:
                first_seen_by_mac = await self._unifi.get_first_seen_by_mac()
            except UnifiApiError as err:
                _LOGGER.warning("first_seen backfill skipped, will retry next restart: %s", err)
            else:
                for mac in needs_backfill:
                    fs = first_seen_by_mac.get(mac)
                    if fs is not None:
                        merged["allowlist"][mac]["first_seen"] = fs
                self._save_soon()
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
            now = time.time()
            if self._failing_since is None:
                self._failing_since = now
            elif now - self._failing_since >= UPDATE_FAILING_THRESHOLD_SECONDS:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="update_failing",
                    translation_placeholders={"error": str(err)},
                )
            raise UpdateFailed(str(err)) from err

        if self._failing_since is not None:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            self._failing_since = None

        now = time.time()
        allowlist = self._state["allowlist"]
        seen_connections = self._state["seen_connections"]

        # Guest-mode entries (expires set) that have run out get actually
        # removed, not just marked - same cleanup async_allowlist_remove
        # does, so an expired guest is indistinguishable from one manually
        # removed. Note: this only affects the *next new session* - a
        # guest still mid-session when their trust window closes isn't
        # kicked off the network or re-prompted until they actually
        # reconnect (is_new_session compares against the same
        # connected_epoch either way).
        for mac in [m for m, v in allowlist.items() if v.get("expires") and v["expires"] < now]:
            del allowlist[mac]
            seen_connections.pop(mac, None)
            self._state["last_notify"].pop(mac, None)
            _LOGGER.info("guest trust expired: mac=%s", mac)

        for c in wireless:
            mac = c.get("macAddress", "").lower()
            if not mac:
                continue
            if mac in allowlist:
                # Update from every poll where an allowlisted device is
                # actually seen - this is the cheap, zero-extra-API-call
                # source for last_seen (see get_wireless_clients: already
                # fetched every cycle). Must happen before the rest of this
                # loop, which is only for MACs not yet on the allowlist.
                allowlist[mac]["last_seen"] = now
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
                allowlist[mac] = {"name": c.get("name") or "(unnamed)", "first_seen": None, "last_seen": now}
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
                # Blocking and notifying are decoupled here on purpose: a
                # reconnect while a decision is still open shouldn't nag
                # with a second push, but if auto_block gets turned on
                # while a device's prompt is sitting unanswered, it should
                # still start blocking on the very next reconnect instead
                # of waiting for the original token to expire (up to 24h)
                # before a fresh detection re-evaluates auto_block at all.
                if self._opt(OPT_AUTO_BLOCK, DEFAULT_AUTO_BLOCK) and not tok.get("blocked"):
                    try:
                        tok["blocked"] = await self._unifi.block_client(mac)
                    except UnifiApiError as err:
                        _LOGGER.error("retroactive auto-block failed mac=%s: %s", mac, err)
                    else:
                        self._save_soon()
                        self.async_set_updated_data(self._state)
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

    def _pending_tokens(self) -> dict[str, dict]:
        now = time.time()
        return {t: v for t, v in self._state["tokens"].items() if not v.get("consumed") and v["expires"] > now}

    def selected_pending_token(self) -> str | None:
        """Token for whichever device the "Pending Device" select currently
        points at, or None if nothing's selected / the selection is stale
        (already decided, expired, or reconnected under a new token) - the
        buttons fall back to oldest-pending in that case, same as before
        this select existed."""
        if self._selected_pending_mac is None:
            return None
        for t, v in self._pending_tokens().items():
            if v["mac"] == self._selected_pending_mac:
                return t
        return None

    async def async_set_selected_pending(self, mac: str | None) -> None:
        self._selected_pending_mac = mac
        self.async_set_updated_data(self._state)

    async def async_handle_action(self, token: str, action: str) -> None:
        """action: "allow" | "approve" | "guest" | "deny". Mirrors
        wifi_watch.py's /action endpoint handler - only consumes the token
        once the actual UniFi call succeeds, so a failed unblock/block
        leaves it usable for retry instead of silently stranding the
        device."""
        entry = self._state["tokens"].get(token)
        if not entry or entry.get("consumed") or entry["expires"] < time.time():
            _LOGGER.warning("action=%s for invalid/expired/consumed token", action)
            return

        now = time.time()
        if action in ("allow", "approve", "guest"):
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
                self._state["allowlist"][entry["mac"]] = {"name": entry["name"], "first_seen": None, "last_seen": now}
                hist_action = "unblocked + allowed" if entry.get("blocked") else "allowed"
                _LOGGER.info(
                    "ALLOWLISTED mac=%s name=%s unblocked=%s", entry["mac"], entry["name"], entry.get("blocked", False)
                )
            elif action == "guest":
                self._state["allowlist"][entry["mac"]] = {
                    "name": entry["name"], "first_seen": None, "last_seen": now, "expires": now + GUEST_TRUST_SECONDS,
                }
                hist_action = "unblocked + trusted for 24h" if entry.get("blocked") else "trusted for 24h"
                _LOGGER.info(
                    "GUEST-TRUSTED mac=%s name=%s unblocked=%s", entry["mac"], entry["name"], entry.get("blocked", False)
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

        if self._selected_pending_mac == entry["mac"]:
            self._selected_pending_mac = None
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
            self._state["allowlist"][mac] = {"name": name, "first_seen": None, "last_seen": now}
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
