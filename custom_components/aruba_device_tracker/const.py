"""Constants for the Aruba Device Tracker integration."""

DOMAIN = "aruba_device_tracker"

CONF_TRACK_NEW = "track_new_devices"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_CLEANUP_ENABLED = "cleanup_enabled"
CONF_CLEANUP_DAYS = "cleanup_days"
CONF_VERIFY_SSL = "verify_ssl"

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

# Stale-device cleanup measures staleness in days, so hourly is ample.
CLEANUP_INTERVAL_HOURS = 1
