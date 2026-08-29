"""Pure decision logic for wifi_watch - no Home Assistant imports, no I/O.

Kept HA-free on purpose so the tricky session-identity/debounce/retention
logic can be unit tested with plain pytest, no hass fixture required.
"""
from __future__ import annotations

import re
from datetime import datetime

# UniFi's own default-hostname convention for an unnamed device is the
# vendor/model name plus a trailing "-e8:f7"-style octet pair (its last two
# MAC bytes) - stripped once here, at name capture, so every downstream
# consumer (history, notifications, sensors, dropdowns) already sees the
# clean name instead of each display site needing to know to strip it.
_TRAILING_MAC_SUFFIX = re.compile(r" [0-9a-fA-F]{2}:[0-9a-fA-F]{2}$")


def clean_device_name(name: str) -> str:
    return _TRAILING_MAC_SUFFIX.sub("", name)


def parse_connected_at(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def is_randomized_mac(mac: str) -> bool:
    first_octet = int(mac.split(":")[0], 16)
    return bool(first_octet & 0b00000010)


def is_new_session(seen_connections: dict, mac: str, connected_epoch: float) -> bool:
    """Per-MAC *session* identity, not a global clock comparison and not
    merely "have we seen this MAC" - seen_connections[mac] records the
    connected_epoch of the last session that didn't need a fresh
    notification (baseline-absorbed on first_run, or already notified).
    A global "last poll time" comparison would make a genuinely-new client
    look like old news, since the controller can take tens of seconds to
    surface a new association."""
    entry = seen_connections.get(mac)
    last_connected_epoch = entry.get("connected_epoch") if entry else None
    return last_connected_epoch != connected_epoch


def should_suppress_notify(last_notify: dict, mac: str, ssid: str | None, now: float, debounce_seconds: float) -> bool:
    """Same MAC + same SSID renotified within the debounce window is a
    continuation of one physical join (a slow/flaky client can produce
    several distinct connectedAt values while joining), not a new session.
    A different SSID still notifies immediately regardless of timing - a
    same-MAC reconnect to a materially different network is exactly the
    event this system exists to catch. No time-based cooldown beyond this:
    session identity (is_new_session) already determines "is this genuinely
    new" - a duplicate-suppression timer on top of it would wrongly eat a
    real reconnect-to-a-different-network event."""
    last = last_notify.get(mac)
    if not last or last.get("ssid") != ssid:
        return False
    return (now - last.get("time", 0)) < debounce_seconds


def prune_by_age(entries: dict, now: float, retention_seconds: float, age_key: str) -> dict:
    """Drop entries whose age_key timestamp is older than retention_seconds.
    For dict-shaped values only (e.g. seen_connections/denied/last_notify).
    age_key is "last_seen" for seen_connections (refreshed every cycle the
    MAC is observed, even when the session isn't new - so pruning reflects
    "haven't seen this MAC at all in N days", not "connected N days ago";
    otherwise a device continuously connected longer than the retention
    window gets pruned mid-session and falsely re-notified), or "time" for
    denied/last_notify. NOT for "notified" - see prune_flat_by_age below,
    that dict's values are plain floats, not nested dicts."""
    return {k: v for k, v in entries.items() if now - v.get(age_key, 0) < retention_seconds}


def prune_flat_by_age(entries: dict, now: float, retention_seconds: float) -> dict:
    """Same idea as prune_by_age, for a dict whose values are themselves the
    timestamp (a plain float), not a nested dict with an age_key field -
    this is state["notified"]'s shape (kept for audit/history purposes
    only, not as a notification gate - see logic.is_new_session)."""
    return {k: t for k, t in entries.items() if now - t < retention_seconds}


def prune_list_by_age(entries: list[dict], now: float, window_seconds: float, age_key: str) -> list[dict]:
    """Same idea as prune_by_age, for a list of dicts rather than a dict
    keyed by id - state["recent_new_client_events"]'s shape (burst-window
    tracking, where several events for the same mac within the window are
    all kept, unlike every other pruned collection here)."""
    return [e for e in entries if now - e.get(age_key, 0) < window_seconds]


def record_history(history: list, mac: str, name: str, action: str, now: float, max_entries: int = 20) -> list:
    history = [{"mac": mac, "name": name, "action": action, "time": now}, *history]
    return history[:max_entries]
