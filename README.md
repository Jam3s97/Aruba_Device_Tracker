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
(Instant AP)(config)# configure
(Instant AP)(config)# allow-rest-api
(Instant AP)(config)# end
(Instant AP)# commit apply
```

## Installation

### HACS (recommended)
1. Add this repository as a custom repository in HACS
2. Search for "Aruba Device Tracker" and install
3. Restart Home Assistant
4. Add the integration via Settings > Devices & Services > Add Integration

### Manual
1. Copy the `custom_components/aruba_device_tracker` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

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

## Default away timer behaviour

The default Aruba IAP client inactivity timer is 1000 seconds (16 minutes 40 seconds). This means when a client disconnects from the wireless network, the session will remain in the client table for 1000 seconds.
- Time for a device to show as away: 1000 seconds + time until next poll
- Time for a device to show as home: Time until next poll after the client connects (default under 30 seconds).

You may want to reduce the inactivity timer. For example, to 300 seconds (5 minutes):

>[!NOTE]
Consider the impact of lowering this value in your environment. Be cautious of going to low.
> The inactivity timeout controls how long a client session remains active after disconnecting.

Via Web GUI
1. Navigate to Configuration > Networks, select your network and click Edit (pencil icon)
2. Click Show Advanced
3. Under Miscellaneous, update Inactivity timeout to the desired value
<img width="1084" height="295" alt="Screenshot from 2026-06-04 13-54-01" src="https://github.com/user-attachments/assets/a0dac1a7-bd69-4b8b-b3bf-f9cb838e3035" />

4. Scroll to the bottom. Next > Next > Next Finish

Via CLI
```
Instant AP (config) # wlan ssid-profile <name>
Instant AP (SSID Profile "<name>") # inactivity-timeout <interval>    (60-86400 seconds)
Instant AP (SSID Profile "<name>") # inactivity-timeout 300
Instant AP (SSID Profile "<name>") # end
Instant AP# commit apply
```

