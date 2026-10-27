# Aruba Instant AP - Home Assistant Integration

[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz/)
[![GitHub Release](https://img.shields.io/github/v/release/Jam3s97/Aruba_Device_Tracker)](https://github.com/Jam3s97/Aruba_Device_Tracker/releases)


A custom integration for Home Assistant that tracks devices connected to Aruba Instant APs using the local REST API.

### 🎉 Now in the HACS default store!

No more adding a custom repository: just search for **Aruba Device Tracker** in HACS.

> **Disclaimer:** This is an unofficial integration and is not affiliated with or endorsed by Aruba Networks. Use at your own risk.

> [!NOTE]
> This is a modern replacement for Home Assistant's built-in `aruba` integration, which is deprecated and polls Instant APs over SSH. This integration instead uses the IAP's local REST API (config flow, no YAML, no SSH).

## Features

- **Device Tracker** - marks devices home/away based on Wi-Fi association
- **Extra attributes per device** - while the device is **home**:
  - `MAC` - Client MAC address
  - `Host name` - Client hostname
  - `ip` - current IP address (standard Home Assistant tracker attribute)
  - `access_point` - which AP the device is connected to
  - `essid` - the SSID/network name
  - `ip_address` - current IP address
  - `os` - operating system detected by the IAP
  - `channel` - Wi-Fi channel

  ...and while the device is **away**:
  - `last_seen` - when the IAP last saw it (survives restarts)
  - `days_until_cleanup` - days left before auto-removal, if enabled
- **Config Flow** - set up entirely from the HA UI, no YAML required
- **Re-authentication** - if the IAP rejects the stored credentials (e.g. the password was rotated), Home Assistant prompts you to re-enter them instead of failing silently
- **Track new devices toggle** - choose whether newly discovered devices are tracked by default (off by default)
- **Configurable poll interval** - how often the IAP is queried (default 30s, range 10–300s)
- **Auto-remove stale devices** - automatically remove entities for devices not seen for a configurable number of days
- **Friendly name renaming** - rename any device via the HA entity registry
- **Offline devices stay away** - devices that are away when HA restarts correctly restore their away state; no unavailable flash or "entity no longer provided" warnings
- **Access points as devices** - each AP in your cluster appears as a device with a **Connectivity** sensor (is the AP online?) and a **Clients** sensor (how many devices are connected to it), plus a **Total clients** sensor for the whole cluster. See [Access Points](#access-points)
- **Clients per network** - a sensor for each Wi-Fi network (SSID) on your controller, counting the devices connected to it. See [Clients per Network](#clients-per-network)
- **Area sensors** - an optional sensor per device showing the area of the access point it's connected to. See [Area Sensors](#area-sensors)
- **Diagnostics download** - one click gives you an anonymised file to attach to bug reports, with device names, MAC addresses, IP addresses, SSIDs and serial numbers removed. See [Reporting a Problem or a New AP Model](#reporting-a-problem-or-a-new-ap-model)
- **AP outages show as unavailable** - a single failed poll is ignored and the last known state is kept. If the IAP fails to answer 3 polls in a row, trackers become `unavailable` (rather than showing stale home/away) until it responds again. The outage and the recovery are each logged once. A device that has simply left is always `not_home`, never `unavailable`

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

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Jam3s97&repository=Aruba_Device_Tracker&category=integration)

Aruba Device Tracker is in the HACS default store, so there's no custom repository to add.

1. Click the button above, or open **HACS** in Home Assistant and search for **Aruba Device Tracker**
2. Click **Download**
3. Restart Home Assistant

### Manual
1. Copy the `custom_components/aruba_device_tracker` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

## Setup

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aruba_device_tracker)

1. Click the button above, or go to **Settings → Devices & Services → Add Integration**
2. Search for **Aruba Device Tracker**
3. **Step 1 - Connection:**
   - **IP Address** - your IAP or Virtual Controller IP (e.g. `192.168.1.10`)
   - **Username** - IAP admin username
   - **Password** - IAP admin password
   - **Verify SSL certificate** - leave **off** (the default) unless you have installed a trusted certificate on the IAP; Instant APs ship with a self-signed certificate that cannot be verified
4. **Step 2 - Tracking & Polling:**
   - **Track new devices by default** - when on, newly discovered devices are immediately tracked; when off, their entities are created but disabled until you enable them manually
   - **Poll interval** - how often the IAP is queried in seconds (default 30s)
   - **Auto-Remove Stale Devices** - automatically remove entities for devices not seen for a set number of days (default: on)
   - **Auto-Remove Stale Devices After** - number of days of inactivity before an entity is removed (default: 30 days)
   - **Access points as devices** - show each access point as a device with its own sensors (default: on). See [Access Points](#access-points)
   - **Area sensors** - give each tracked device a sensor showing its access point's area (default: off). See [Area Sensors](#area-sensors)

## Options

Tracking and polling settings (track new devices, poll interval, stale device cleanup) are editable after setup via **Configure** on the integration card. They take effect immediately, without a reload.

The poll interval, track new devices toggle, and stale device cleanup settings are also available as entities in the **Configuration** section of the IAP device page, for quick changes without opening **Configure**. Being configuration entities, they're left out of auto-generated dashboards and of voice commands such as "turn off all switches".

Turning **Track new devices** on from the switch or **Configure** also enables trackers that were created disabled while it was off. Trackers you disabled yourself stay disabled.

**Access points as devices** and **Area sensors** are also under **Configure**. They are the only settings that reload the integration when you change them, because they add or remove a set of devices and entities. Your trackers are not affected.

## Changing the IP Address or Credentials

To change the IP address, username, password, or certificate verification, open the integration card's **⋮** menu and choose **Reconfigure**. The connection is tested before anything is saved, then the integration reloads.

The password field is not pre-filled - leave it blank to keep the stored password.

## Multiple Clusters or Standalone APs

One config entry covers a whole Instant cluster: add the Virtual Controller's IP once, and devices roaming between APs in that cluster stay a single tracker.

If you also run a separate cluster or a standalone AP, add it as its own entry. A device seen by both gets one tracker per entry (for example `device_tracker.phone` and `device_tracker.phone_2`). Rename them to tell them apart, e.g. "Phone (Workshop)". To get a single home/away answer, add both trackers to the same [person](https://www.home-assistant.io/integrations/person/): a person is home if any of their network trackers is home.

## Randomised (Private) MAC Addresses

Many phones and tablets use a **private MAC address that's randomised per SSID**: the device gives each Wi-Fi network a different MAC address. The integration tells devices apart by MAC address, so a phone that connects to more than one of your SSIDs (for example a main network and a separate 2.4 GHz one) shows up as **two devices with two trackers**. The second gets a `_2` on the end, for example `device_tracker.jos_phone` and `device_tracker.jos_phone_2`.

To have one tracker per device when connecting to multiple SSIDs, turn off the private address for your home networks on the device itself:

- **Android:** **Settings → Wi-Fi**, tap the network, then **Privacy** (or **MAC address type**) → **Use device MAC**
- **iPhone / iPad:** **Settings → Wi-Fi**, tap **ⓘ** next to the network, then set **Private Wi-Fi Address** to **Off**

Do this for each of your SSIDs the device uses, then delete the tracker you no longer need.

If you'd rather keep private addresses on, add both trackers to the same [person](https://www.home-assistant.io/integrations/person/): a person is home if any of their trackers is home.

> [!NOTE]
> Some devices can also change their private address over time, not just per SSID. Each new address is a new tracker. **Auto-remove stale devices** clears out the old ones once they haven't been seen for the set number of days.

## Access Points

> [!NOTE]
> New in 2.2.0. It is **on by default**, including for existing installs when they upgrade. Turn it off under **Configure → Access points as devices** if you don't want it.

Each access point in your cluster becomes a device in Home Assistant, named as it is on the controller (for example *LoungeAP*), with its model and serial number. On the integration's page they're listed together with the IAP device under a **Controller and access points** group.

Each AP device has:

- **Connectivity** - **Connected** while the AP is part of the cluster, **Disconnected** when it drops out of the cluster (for example, powered off or rebooting)
- **Clients** - how many devices are connected to that AP right now

The IAP device also gets a **Total clients** sensor for the whole cluster.

**How quickly it updates.** In testing on a AP cluster, an AP that lost power dropped out of the cluster within 1–1½ minutes, so **Connectivity** shows **Disconnected** within about 2 minutes, counting the time until the next poll. A rebooting AP rejoins about 3 minutes after power returns, and shows **Connected** as soon as it does.

**Use the Virtual Controller IP.** The access point list is read from the cluster's conductor (the AP currently running the Virtual Controller). Configure the integration with the **Virtual Controller IP** rather than a single AP's address, so it keeps working if a different AP takes over as conductor. To change it, use **Reconfigure** (see above).

**What it costs.** One extra request to the AP per poll. The sensors only write to the recorder database when a value actually changes: an AP going offline or coming back, or a device joining or leaving.

**Removing an AP for good.** An AP that goes offline keeps its device, so a reboot doesn't lose its name, area or customisations. To remove an AP you have retired, open its device page and choose **Delete**. Home Assistant only allows this once the AP is no longer in the cluster.

**Turning it off** removes the AP devices and their sensors.
Device trackers and **Total clients** are not touched.

> [!NOTE]
> Clients are matched to APs by AP name. If two APs share a name their counts can't be told apart, and a warning is logged: give each AP a unique name on the controller.

## Clients per Network

The IAP device gets a **Clients on …** sensor for each Wi-Fi network configured on your controller, for example *Clients on Home* or *Clients on Guest*. It counts the devices connected to that network across the whole cluster.

- Every network configured on the controller gets a sensor, including ones nobody is connected to right now (they read 0).
- A network that's **disabled** on the controller shows as unavailable.
- A network you **delete** on the controller has its sensor removed. Renaming a network's SSID renames its sensor.
- The list of networks is read when Home Assistant starts and then every 30 minutes, so a newly added network appears within half an hour. The counts update on every poll.

This works whether or not **Access points as devices** is turned on.

## Area Sensors

Every **enabled** device tracker also gets an **Area** sensor, for example *Jo's phone area*. It shows the area of the access point the device is connected to. These sensors are **turned off by default**: enable the ones you want, usually just phones and tablets, from the device's entity settings (**Settings → Entities**, search for "area", open one and turn on **Enabled**).

A tracker that's disabled has no Area sensor. When you enable a tracker, its Area sensor appears shortly after, once Home Assistant has reloaded the integration. If you disable a tracker, its Area sensor is removed.

Area sensors are **off by default**. Turning on **Configure → Area sensors** creates them, each one disabled, so you then enable just the ones you want. Turning the option off again removes them all.

> [!IMPORTANT]
> The area is the area of the **access point** the device is connected to. That is usually the nearest one, but it doesn't mean the device is in that room: devices often stay connected to a farther access point, and one access point can cover several rooms. Treat it as "near", not "in".

To set it up:

1. Turn on **Access points as devices** (it's on by default) and turn on **Area sensors** (off by default), both under **Configure**.
2. Give each access point an area: open the AP's device page and set its **Area**. If any are missing once you've enabled an Area sensor, a notice under **Settings → Repairs** lists them.
3. Enable the Area sensors you want.

The sensor shows **unknown** while the device is away, and when its access point has no area. When a device moves to an access point in another area, the sensor updates on the next poll. It also follows you renaming an area, or moving an access point to a different area.

Each Area sensor is named after its device when the sensor is first created. If you rename the device tracker later, rename the Area sensor too. Every Area sensor has a `mac` attribute, the same as its tracker's, so you can always tell which tracker it belongs to, even when two share a name (see [Randomised MAC Addresses](#randomised-private-mac-addresses)). It also has an `area_id` attribute while the area is known.

## Reporting a Problem or a New AP Model

The integration has been tested on a few AP models and firmware versions, so reports from other hardware are very welcome, whether everything works or not.

1. Go to **Settings → Devices & services → Aruba Device Tracker**
2. Open the **⋮** menu on your entry and choose **Download diagnostics**
3. Open a [new issue](https://github.com/Jam3s97/Aruba_Device_Tracker/issues/new/choose): choose **Report an AP model or firmware**, or **Bug report** if something is broken, and attach the file

The diagnostics file contains the raw client, access point and network tables from your AP, with device names, MAC addresses, IP addresses, IPv6 addresses, SSIDs, AP names and serial numbers replaced by placeholders. Your host, username and password are removed. It's worth a quick look before you attach it.

## Renaming Devices

Go to **Settings → Devices & Services → Entities**, find the device tracker entity, click it, then click the pencil icon to give it a friendly name. This is stored in the HA entity registry and persists across restarts.

## Auto-Remove Stale Devices

When enabled, device tracker entities that have not been seen for the configured number of days are automatically removed. The check runs at startup and roughly hourly thereafter. The last-seen timestamp for each device is stored persistently and survives HA restarts.

- **Auto-Remove Stale Devices** switch - enable or disable the feature
- **Auto-Remove Stale Devices After** number - days threshold (1–365, default 30)

Both are configurable during setup, via **Configure**, or directly on the IAP device card.

> [!NOTE]
> Auto-remove defaults to **on** with a 30-day threshold. Devices are only removed if they haven't appeared in any poll result for the full threshold period. If a device reconnects, its last-seen timestamp resets and the countdown starts again.

## Recorder Database Size

Home Assistant writes a row to the recorder database whenever an entity's state **or any of its attributes** changes. A device tracker's state (`home`/`not_home`) changes rarely, so attributes are what drive database growth - and an attribute that ticks on every poll costs one row per device per poll. At the default 30-second interval that is ~2,880 rows per day per device.

This integration is designed so that a device sitting still costs **nothing**:

- **`signal` and `speed` are not exposed.** The IAP reports both and they are still parsed (they show up in debug logs), but their values change almost continuously, making them pure database churn.

- **`last_seen` and `days_until_cleanup` are only published while a device is away.** For a connected device "last seen" is always ~now, so it changed every poll while telling you nothing the `home` state didn't already.

What remains - `access_point`, `essid`, `ip_address`, `os`, `channel` - only changes when something actually changes: the device roams to another AP, gets a new IP, or switches band. So a device that stays connected to the same AP writes one row when it arrives and one when it leaves.

The [access point](#access-points) and [network](#clients-per-network) sensors follow the same rule. They have no attributes, and only write a row when a count or an AP's connectivity actually changes. An [Area sensor](#area-sensors) writes a row each time its device moves to an access point in a different area, so a device that keeps switching between two access points in different areas writes more.

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
