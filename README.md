# wifi-watch

A Home Assistant custom integration: get notified the moment an unrecognized
device joins your WiFi, and approve, approve-once, or block it from an
actionable notification (within Home Assistant Companion on your phone, or
Home Assistant itself).

## How it works

Every few seconds (configurable), the integration polls your UniFi
controller's wireless client list. A client whose MAC isn't already on your
allowlist and whose connection session looks new (not just "still seen this
poll") triggers a push notification with three actions:

- **Allow + save** — adds it to the allowlist permanently; you won't be
  asked about it again.
- **Approve once** — lets it on this one time; you'll be asked again next
  time it connects.
- **Deny (block)** — blocks the device at the UniFi controller.

If `auto_block` is enabled (off by default), a new device is blocked
immediately on detection instead of waiting for you to act — it stays
blocked until you tap Allow or Approve.

## Requirements

- A UniFi Network controller reachable from Home Assistant.
- **Two separate UniFi credentials**:
  - An **integration API key** — used for the poll loop that checks for
    new/unrecognized devices every few seconds.
  - A **dedicated local UniFi account** (username + password) — used only
    for blocking/unblocking and SSID lookups, which are only available
    through UniFi's legacy cookie-session API, not the official one. Give
    it **Full Management** under the Network role dropdown (required for
    blocking/unblocking) and leave the other role categories at **None**
    — don't reuse your own admin login.

  To create both:

  - **API key**: the **Integrations** icon in the left sidebar → **Create
    New API Key** → name it (e.g. "Wi-Fi Watch") → copy the key
    immediately, UniFi typically only shows it once.
  - **Dedicated account**: the **People** icon at the bottom of the left
    sidebar → **Create New → Create New User** → fill in
    First/Last Name → check **Admin** (reveals more fields) → check
    **Restrict to Local Access Only** (local username/password login, not
    a cloud/email invite) → set **Username** and **Password** (12
    characters minimum) → set the **Network** role dropdown to **Full
    Management**, leave the other role dropdowns at **None** →
    **Create**.
- The Home Assistant mobile app installed on whichever phone(s) should get
  the approval notifications, with notifications enabled for it.
- Home Assistant 2026.1.0 or newer.
- Home Assistant reachable from wherever you'll be tapping notification
  actions from — see External reachability below if that's not just "at
  home on your own WiFi" for you.

## Install

**Via HACS**: HACS → Integrations → ⋮ → Custom repositories → add this
repo's URL as type "Integration" → install "Wi-Fi Watch" → restart Home
Assistant.

**Manually**: copy the `custom_components/wifi_watch/`
directory from this repo into your Home Assistant config's
`custom_components/` directory, then restart Home Assistant.

Either way, after restart: **Settings → Devices & Services → Add
Integration → search "Wi-Fi Watch"**.

## Setup

The config flow is two short steps:

1. **Connection**: controller host/IP, integration API key, dedicated
   account username/password (see Requirements above for both
   credentials). Validated before continuing.
2. **Site**: skipped automatically if your controller only has one site;
   otherwise pick it from a dropdown, no UUID-hunting required.

If setup fails with "couldn't connect," it's almost always the API key or
the dedicated account's credentials.

Credentials rotate or expire eventually. If they do, Home Assistant will
surface this integration as needing reauthentication rather than silently
failing — click through it and re-enter the UniFi account's new password.

## Notify targets

Every notification broadcasts automatically to `persistent_notification`
plus every paired phone's `mobile_app_*` service — no configuration
needed. Pair a phone with the HA Companion App after setup and it starts
getting notified immediately; unpair it and it drops off on its own. To
stop notifying a specific target without unpairing it, exclude it in
Options below.

## Options

**Settings → Devices & Services → Wi-Fi Watch → Configure**, changeable
without removing and re-adding the integration:

| Option | Default | Notes |
|---|---|---|
| Notify targets to exclude | none | Opt specific targets out of the broadcast described above |
| Poll interval | 7s | How often the UniFi client list is checked |
| Notify debounce | 90s | Suppresses a duplicate notification for the same MAC+SSID within this window — a slow-joining client can otherwise trigger the poll loop's "is this a new session" check more than once for what's really one physical join |
| Retention window | 30 days | How long old sessions/denials/notify history are kept before pruning |
| Approval link expiry | 24h | How long a pending token stays valid before it silently expires |
| Auto-block new devices | off | See "How it works" above |

## Entities

One device ("Wi-Fi Watch") with everything needed to act on it, no
dashboard required — Home Assistant's own auto-generated device page is a
working control surface on its own:

- **Pending Approvals**, **Allowlist**, **Denied**, **Recent Activity** —
  diagnostic sensors, each a count with an attribute listing the actual
  entries (device name, MAC, IP, SSID, vendor, block status, etc.).
- **Allow + Save**, **Approve Once**, **Deny** — buttons that act on the
  oldest pending device, same effect as tapping a notification action.
- **Remove From Allowlist**, **Remove From Currently Blocked** — dropdowns
  listing the real allowlist/denied devices as always-current options;
  picking one removes it immediately.

## Services

- `wifi_watch.decide` — act on a pending device by token: `token` +
  `action` (`allow` | `approve` | `deny`). Same effect as tapping a
  notification button; useful for building your own dashboard controls
  (see below) or automations.
- `wifi_watch.allowlist_remove` — takes a device off the allowlist; it'll
  get a fresh approval prompt next time it reconnects.
- `wifi_watch.denied_remove` — unblocks a currently-blocked device.
  `allowlist: true` also allowlists it in the same call ("I denied that by
  accident"); `allowlist: false` (default) leaves it unknown, so it'll get
  a fresh prompt on reconnect.
- `wifi_watch.test_notification` — sends a plain test push to every notify
  target (see Notify targets above), no actions, just to confirm delivery
  works.

## Dashboard (optional)

The push notification alone is a complete approve/deny flow, and every
entity above already shows up on the integration's own device page
(Settings → Devices & Services → Wi-Fi Watch → the device) — you don't
need a dashboard for wifi-watch to work.

If you'd rather have a dedicated view (e.g. to see the pending device at a
glance without opening the device page), this repo ships one:
`dashboard/dashboard.yaml`, loaded straight from the file (not pasted into
the UI), so future updates to this file take effect on the next restart
with nothing to redo. Copy the `dashboard/` folder into your config
directory, then add:
```yaml
lovelace:
  dashboards:
    wifi-watch:
      mode: yaml
      title: Wi-Fi Approval
      icon: mdi:wifi-check
      show_in_sidebar: true
      filename: dashboard/dashboard.yaml
```

## Diagnostics

**Settings → Devices & Services → Wi-Fi Watch → Download Diagnostics** —
UniFi credentials and the dedicated account's password are redacted
automatically.

## Uninstall

- **Integration**: Settings → Devices & Services → Wi-Fi Watch → ⋮ →
  Delete — also deletes its stored state (allowlist, denials, history).
- **Dashboard** (if installed): remove the `lovelace: dashboards:` block
  from `configuration.yaml` and delete the `dashboard/` folder from your
  config.

The dashboard is your own `configuration.yaml` entry, not something the
integration owns — deleting the integration alone won't remove it.

## External reachability

Receiving a push notification doesn't require Home Assistant to be
reachable from outside your home network — that leg is Home Assistant
pushing out to Apple/Google's push service regardless. Acting on it
(tapping a notification action button) does: the tap has to reach back to
Home Assistant's API, which only works from outside your LAN if Home
Assistant is externally reachable somehow. At home on your own WiFi this
isn't a factor either way. Options, easiest first:

- **[Nabu Casa Cloud](https://www.nabucasa.com/)** — paid subscription,
  official, handles remote access and TLS with no networking setup on your
  end.
- **A VPN back to your home network** (Tailscale, WireGuard) — free, phone
  reaches Home Assistant as if it were on the LAN.
- **Your own reverse proxy with TLS**, port-forwarded — free, but you own
  the security exposure of putting Home Assistant's web UI on the
  internet.
