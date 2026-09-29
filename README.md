# Aruba Instant AP — Home Assistant Integration

A custom integration for Home Assistant that tracks devices connected to an Aruba Instant AP using the local REST API.

> **Disclaimer:** This is an unofficial integration and is not affiliated with or endorsed by Aruba Networks. Use at your own risk.

> [!NOTE]
> This is a modern replacement for Home Assistant's built-in `aruba` integration, which is deprecated and polls Instant APs over SSH. This integration instead uses the IAP's local REST API (config flow, no YAML, no SSH).

## Features

- **Device Tracker** — marks devices home/away based on Wi-Fi association
- **Extra attributes per device** — while the device is **home**:
  - `MAC` — Client MAC address
  - `Host name` — Client hostname
  - `ip` — current IP address (standard Home Assistant tracker attribute)
  - `access_point` — which AP the device is connected to
  - `essid` — the SSID/network name
  - `ip_address` — current IP address
  - `os` — operating system detected by the IAP
  - `channel` — Wi-Fi channel

  ...and while the device is **away**:
  - `last_seen` — when the IAP last saw it (survives restarts)
  - `days_until_cleanup` — days left before auto-removal, if enabled
- **Config Flow** — set up entirely from the HA UI, no YAML required
- **Re-authentication** — if the IAP rejects the stored credentials (e.g. the password was rotated), Home Assistant prompts you to re-enter them instead of failing silently
- **Track new devices toggle** — choose whether newly discovered devices are tracked by default (off by default)
- **Configurable poll interval** — how often the IAP is queried (default 30s, range 10–300s)
- **Auto-remove stale devices** — automatically remove entities for devices not seen for a configurable number of days
- **Friendly name renaming** — rename any device via the HA entity registry
- **Offline devices stay away** — devices that are away when HA restarts correctly restore their away state; no unavailable flash or "entity no longer provided" warnings

## Requirements

- Aruba Instant AOS 8.5.0+
- Admin account on the IAP
- REST API enabled on the IAP:

```
Instant AP# configure
Instant AP(config)# allow-rest-api
Instant AP(config)# end
Instant AP# commit apply
```

## Installation

### HACS (recommended)
1. Add this repository as a custom repository in HACS
2. Search for **Aruba Device Tracker** and install
3. Restart Home Assistant

### Manual
1. Copy the `custom_components/aruba_device_tracker` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Aruba Device Tracker**
3. **Step 1 — Connection:**
   - **IP Address** — your IAP or Virtual Controller IP (e.g. `192.168.1.10`)
   - **Username** — IAP admin username
   - **Password** — IAP admin password
   - **Verify SSL certificate** — leave **off** (the default) unless you have installed a trusted certificate on the IAP; Instant APs ship with a self-signed certificate that cannot be verified
4. **Step 2 — Tracking & Polling:**
   - **Track new devices by default** — when on, newly discovered devices are immediately tracked; when off, their entities are created but disabled until you enable them manually
   - **Poll interval** — how often the IAP is queried in seconds (default 30s)
   - **Auto-Remove Stale Devices** — automatically remove entities for devices not seen for a set number of days (default: on)
   - **Auto-Remove Stale Devices After** — number of days of inactivity before an entity is removed (default: 30 days)

## Options

All settings are editable after setup via **Configure** on the integration card, including IP address and credentials. Changing the IP, credentials, or the certificate-verification setting will trigger a reconnection test before saving.

The password field is not pre-filled — leave it blank to keep the stored password.

The poll interval, track new devices toggle, and stale device cleanup settings are also available as entities on the IAP device card for quick changes without opening the options flow.

## Renaming Devices

Go to **Settings → Devices & Services → Entities**, find the device tracker entity, click it, then click the pencil icon to give it a friendly name. This is stored in the HA entity registry and persists across restarts.

## Auto-Remove Stale Devices

When enabled, device tracker entities that have not been seen for the configured number of days are automatically removed. The check runs at startup and roughly hourly thereafter. The last-seen timestamp for each device is stored persistently and survives HA restarts.

- **Auto-Remove Stale Devices** switch — enable or disable the feature
- **Auto-Remove Stale Devices After** number — days threshold (1–365, default 30)

Both are configurable during setup, via the options flow, or directly on the IAP device card.

> [!NOTE]
> Auto-remove defaults to **on** with a 30-day threshold. Devices are only removed if they haven't appeared in any poll result for the full threshold period. If a device reconnects, its last-seen timestamp resets and the countdown starts again.

## Recorder Database Size

Home Assistant writes a row to the recorder database whenever an entity's state **or any of its attributes** changes. A device tracker's state (`home`/`not_home`) changes rarely, so attributes are what drive database growth — and an attribute that ticks on every poll costs one row per device per poll. At the default 30-second interval that is ~2,880 rows per day per device.

This integration is designed so that a device sitting still costs **nothing**:

- **`signal` and `speed` are not exposed.** The IAP reports both and they are still parsed (they show up in debug logs), but their values change almost continuously, making them pure database churn. See the note below if you were using them.
- **`last_seen` and `days_until_cleanup` are only published while a device is away.** For a connected device "last seen" is always ~now, so it changed every poll while telling you nothing the `home` state didn't already.

What remains — `access_point`, `essid`, `ip_address`, `os`, `channel` — only changes when something actually changes: the device roams to another AP, gets a new IP, or switches band. So a device that stays connected to the same AP writes one row when it arrives and one when it leaves.

No `recorder:` configuration is needed. If you still want to trim history further, raising the poll interval reduces how quickly arrivals and departures are detected but does not otherwise affect row volume, since rows are now driven by real changes rather than by polling.

> [!IMPORTANT]
> **Breaking change in 2.0.0:** the `signal` and `speed` attributes were removed, and `last_seen` / `days_until_cleanup` are no longer present while a device is home. If you have automations or templates reading `state_attr('device_tracker.x', 'signal')` or reading `last_seen` on a home device, they will now return `None`. For "how long has this device been home", use the entity's built-in `last_changed` instead.

## Default Away Timer Behaviour

The default Aruba IAP client inactivity timer is 1000 seconds (~16 minutes). When a client disconnects, its session remains in the client table until the timer expires.

- **Time to show away:** inactivity timeout + time until next poll
- **Time to show home:** time until next poll after the client reconnects (under 30 seconds by default)

You may want to reduce the inactivity timer. For example, to 300 seconds (5 minutes):

> [!NOTE]
> Consider the impact of lowering this value in your environment. The inactivity timeout controls how long a client session remains active after disconnecting. Values below 300 seconds may cause re-authentication events on some devices.

**Via Web GUI:**
1. Navigate to **Configuration → Networks**, select your network and click **Edit** (pencil icon)
2. Click **Show Advanced**
3. Under **Miscellaneous**, update **Inactivity timeout** to the desired value
<img width="1084" height="295" alt="Inactivity timeout setting" src="https://github.com/user-attachments/assets/a0dac1a7-bd69-4b8b-b3bf-f9cb838e3035" />
4. Click **Next → Next → Next → Finish**

**Via CLI:**
```
Instant AP# configure
Instant AP (config) # wlan ssid-profile <name>
Instant AP (SSID Profile "<name>") # inactivity-timeout 300    (60–86400 seconds)
Instant AP (SSID Profile "<name>") # end
Instant AP# commit apply
```