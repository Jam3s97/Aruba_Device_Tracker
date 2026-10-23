"""Constants for the Aruba Device Tracker integration."""

DOMAIN = "aruba_device_tracker"

# The config subentry that groups this integration's own devices (the IAP
# device and the AP devices) on the integration page, apart from the devices
# HA links to trackers by MAC. It always exists, AP option on or off. The
# type id predates the current title; changing it would orphan existing
# subentries, so it stays.
CLUSTER_SUBENTRY_TYPE = "access_points"
CLUSTER_SUBENTRY_TITLE = "Controller and access points"

# Shared by the IAP control device and the AP devices.
MANUFACTURER = "Aruba Networks (HPE)"

CONF_TRACK_NEW = "track_new_devices"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_CLEANUP_ENABLED = "cleanup_enabled"
CONF_CLEANUP_DAYS = "cleanup_days"
CONF_VERIFY_SSL = "verify_ssl"
CONF_ENABLE_AP_DEVICES = "enable_ap_devices"

DEFAULT_TRACK_NEW = False
DEFAULT_SCAN_INTERVAL = 30  # seconds
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300

DEFAULT_CLEANUP_ENABLED = True
DEFAULT_CLEANUP_DAYS = 30
MIN_CLEANUP_DAYS = 1
MAX_CLEANUP_DAYS = 365

# Instant APs ship with a self-signed certificate, so verification is off by
# default. Users who have provisioned a trusted certificate can opt in.
DEFAULT_VERIFY_SSL = False

# Each access point in the cluster becomes a device with a client-count sensor.
# Costs one extra 'show aps' request per poll. On by default for the 2.2 beta;
# revisit before the stable release.
DEFAULT_ENABLE_AP_DEVICES = True

# Client data attribute keys.
#
# Deliberately excludes signal and speed. The IAP reports both, and the client
# still parses them, but exposing them as state attributes made every tracked
# device write a recorder row on every poll (~2,880/day/device at the default
# 30s interval) because their values change almost continuously. See the
# "Recorder write volume" note in CLAUDE.md before re-adding either.
ATTR_ACCESS_POINT = "access_point"
ATTR_ESSID = "essid"
ATTR_IP_ADDRESS = "ip_address"
ATTR_OS = "os"
ATTR_CHANNEL = "channel"

# Last-seen / cleanup attribute keys. Only published while a device is away —
# see ArubaClientEntity.extra_state_attributes.
ATTR_LAST_SEEN = "last_seen"
ATTR_DAYS_UNTIL_CLEANUP = "days_until_cleanup"

# Storage for last-seen timestamps. The live key is built per config entry as
# f"{DOMAIN}.{entry_id}.last_seen"; LEGACY_STORAGE_KEY is the pre-2.0 shared
# key, still read once to migrate existing installs.
STORAGE_VERSION = 1
LEGACY_STORAGE_KEY = f"{DOMAIN}.last_seen"

# Coalesce last-seen writes instead of rewriting the file on every poll.
STORAGE_SAVE_DELAY = 60  # seconds

# A failed poll keeps last-known data, so a single blip is invisible. This many
# failures in a row means the AP is genuinely down: the coordinator then raises
# UpdateFailed and the trackers go unavailable until a poll succeeds.
UNAVAILABLE_AFTER_FAILED_POLLS = 3

# Stale-device cleanup measures staleness in days, so hourly is ample.
CLEANUP_INTERVAL_HOURS = 1
