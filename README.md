# Aruba Instant AP — Home Assistant Integration

A custom integration for Home Assistant that tracks devices connected to an Aruba Instant AP using the local REST API.

Disclaimer: This is an unofficial integration and is not affiliated with or endorsed by Aruba Networks. Use at your own risk.

## 🚧 Current Status

**BETA** - This integration is in active development and testing.

## Features

- **Device Tracker** — marks devices home/away based on Wi-Fi association
- **Extra attributes per device:**
  - `MAC` — Client MAC Address
  - `Host name` — Client host name
  - `access_point` — which AP the device is connected to
  - `essid` — the SSID/network name
  - `ip_address` — current IP address
  - `os` — operating system detected by the IAP
  - `channel` — Wi-Fi channel
  - `signal` — signal strength
  - `speed` — link speed
- **Config Flow** — set up entirely from the HA UI, no YAML required
- **Track new devices toggle** — choose whether newly discovered devices are tracked by default (off by default)
- **Friendly name renaming** — rename any device via the HA entity registry

## Requirements

- Aruba Instant AOS 8.5.0+ (REST API support)
- Admin account for API
- REST API must be enabled on the IAP:

```
(Instant AP)(config)# allow-rest-api
(Instant AP)(config)# end
(Instant AP)# commit apply
```

## Installation

### Manual
1. Copy the `custom_components/aruba_device_tracker` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

### ⚠️HACS (recommended)
### ⚠️ Not published in HACS (YET)
1. Add this repo as a custom repository in HACS
2. Install "Aruba Device Tracker"
3. Restart Home Assistant

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Aruba Device Tracker**
3. Enter:
   - **IP Address** — your IAP/VC IP (e.g. `192.168.1.10`)
   - **Username** — IAP admin username
   - **Password** — IAP admin password
   - **Track new devices by default** — toggle on/off
   - **Polling Interval** - Devices are polled every 30 seconds by default. Update if required.

## Options

After setup, click **Configure** on the integration to change the track-new-devices default.

## Renaming Devices

Go to **Settings → Devices & Services → Aruba Device Tracker**, click a device entity, then click the pencil icon to give it a friendly name. This is stored in the HA entity registry and persists across restarts.

