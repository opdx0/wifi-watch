import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "wifi_watch"))

from logic import (
    is_new_session,
    is_randomized_mac,
    parse_connected_at,
    prune_by_age,
    prune_flat_by_age,
    record_history,
    should_suppress_notify,
)


def test_parse_connected_at():
    assert parse_connected_at("2026-08-23T22:15:41.055Z") == parse_connected_at("2026-08-23T22:15:41.055+00:00")


def test_is_randomized_mac_locally_administered_bit():
    assert is_randomized_mac("02:00:00:00:00:00") is True
    assert is_randomized_mac("de:ad:be:ef:00:01") is True
    assert is_randomized_mac("4c:49:6c:e8:f7:0b") is False


def test_is_new_session_unseen_mac():
    assert is_new_session({}, "4c:49:6c:e8:f7:0b", 1000.0) is True


def test_is_new_session_same_connected_epoch_not_new():
    seen = {"4c:49:6c:e8:f7:0b": {"connected_epoch": 1000.0, "last_seen": 1010.0}}
    assert is_new_session(seen, "4c:49:6c:e8:f7:0b", 1000.0) is False


def test_is_new_session_different_connected_epoch_is_new():
    seen = {"4c:49:6c:e8:f7:0b": {"connected_epoch": 1000.0, "last_seen": 1010.0}}
    assert is_new_session(seen, "4c:49:6c:e8:f7:0b", 2000.0) is True


def test_should_suppress_notify_within_window_same_ssid():
    last_notify = {"mac1": {"ssid": "phatnet", "time": 1000.0}}
    assert should_suppress_notify(last_notify, "mac1", "phatnet", now=1050.0, debounce_seconds=90) is True


def test_should_suppress_notify_outside_window():
    last_notify = {"mac1": {"ssid": "phatnet", "time": 1000.0}}
    assert should_suppress_notify(last_notify, "mac1", "phatnet", now=1200.0, debounce_seconds=90) is False


def test_should_suppress_notify_different_ssid_never_suppressed():
    # A same-MAC reconnect to a materially different network must always
    # notify, regardless of timing - this is the case a blind time-based
    # cooldown would wrongly eat.
    last_notify = {"mac1": {"ssid": "phatnet", "time": 1000.0}}
    assert should_suppress_notify(last_notify, "mac1", "IoT-Honeypot", now=1005.0, debounce_seconds=90) is False


def test_should_suppress_notify_unseen_mac():
    assert should_suppress_notify({}, "mac1", "phatnet", now=1000.0, debounce_seconds=90) is False


def test_prune_by_age_drops_stale_keeps_fresh():
    entries = {
        "stale": {"last_seen": 0.0},
        "fresh": {"last_seen": 950.0},
    }
    pruned = prune_by_age(entries, now=1000.0, retention_seconds=100, age_key="last_seen")
    assert pruned == {"fresh": {"last_seen": 950.0}}


def test_prune_by_age_time_key_variant():
    entries = {"mac1": {"time": 1000.0}}
    assert prune_by_age(entries, now=1000.0, retention_seconds=100, age_key="time") == entries
    assert prune_by_age(entries, now=1200.0, retention_seconds=100, age_key="time") == {}


def test_prune_flat_by_age_drops_stale_keeps_fresh():
    # This is state["notified"]'s actual shape - {mac: float}, not
    # {mac: {"time": float}}. A real regression: prune_by_age (the
    # dict-shaped variant) was wrongly used on this dict in coordinator.py
    # and crashed every poll cycle with AttributeError: 'float' object has
    # no attribute 'get' - caught via live HA testing, not this test suite,
    # since no test previously covered the flat-value shape at all.
    entries = {"stale": 0.0, "fresh": 950.0}
    assert prune_flat_by_age(entries, now=1000.0, retention_seconds=100) == {"fresh": 950.0}


def test_record_history_prepends_and_caps():
    history = []
    for i in range(25):
        history = record_history(history, mac="m", name="n", action=f"action{i}", now=float(i), max_entries=20)
    assert len(history) == 20
    assert history[0]["action"] == "action24"
    assert history[-1]["action"] == "action5"
