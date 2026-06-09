# Aruba Instant AP — Home Assistant Integration

A custom integration for Home Assistant that tracks devices connected to an Aruba Instant AP using the local REST API.

> **Disclaimer:** This is an unofficial integration and is not affiliated with or endorsed by Aruba Networks. Use at your own risk.

## Features

- **Device Tracker** — marks devices home/away based on Wi-Fi association
- **Extra attributes per device:**
  - `MAC` — Client MAC address
  - `Host name` — Client hostname
  - `access_point` — which AP the device is connected to
  - `essid` — the SSID/network name
  - `ip_address` — current IP address
  - `os` — operating system detected by the IAP
  - `channel` — Wi-Fi channel
  - `signal` — signal strength
  - `speed` — link speed
- **Config Flow** — set up entirely from the HA UI, no YAML required
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
4. **Step 2 — Tracking & Polling:**
   - **Track new devices by default** — when on, newly discovered devices are immediately tracked; when off, their entities are created but disabled until you enable them manually
   - **Poll interval** — how often the IAP is queried in seconds (default 30s)
   - **Auto-Remove Stale Devices** — automatically remove entities for devices not seen for a set number of days (default: on)
   - **Auto-Remove Stale Devices After** — number of days of inactivity before an entity is removed (default: 30 days)

## Options

All settings are editable after setup via **Configure** on the integration card, including IP address and credentials. Changing the IP or credentials will trigger a reconnection test before saving.

The poll interval, track new devices toggle, and stale device cleanup settings are also available as entities on the IAP device card for quick changes without opening the options flow.

## Renaming Devices

Go to **Settings → Devices & Services → Entities**, find the device tracker entity, click it, then click the pencil icon to give it a friendly name. This is stored in the HA entity registry and persists across restarts.

## Auto-Remove Stale Devices

When enabled, device tracker entities that have not been seen for the configured number of days are automatically removed after each poll cycle. The last-seen timestamp for each device is stored persistently and survives HA restarts.

- **Auto-Remove Stale Devices** switch — enable or disable the feature
- **Auto-Remove Stale Devices After** number — days threshold (1–365, default 30)

Both are configurable during setup, via the options flow, or directly on the IAP device card.

> [!NOTE]
> Auto-remove defaults to **on** with a 30-day threshold. Devices are only removed if they haven't appeared in any poll result for the full threshold period. If a device reconnects, its last-seen timestamp resets and the countdown starts again.

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