"""Constants for the Aruba Device Tracker integration."""

DOMAIN = "aruba_device_tracker"

CONF_TRACK_NEW = "track_new_devices"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_CLEANUP_ENABLED = "cleanup_enabled"
CONF_CLEANUP_DAYS = "cleanup_days"

DEFAULT_TRACK_NEW = False
DEFAULT_PORT = 4343
DEFAULT_SCAN_INTERVAL = 30  # seconds
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300

DEFAULT_CLEANUP_ENABLED = True
DEFAULT_CLEANUP_DAYS = 30
MIN_CLEANUP_DAYS = 1
MAX_CLEANUP_DAYS = 365

# Client data attribute keys
ATTR_ACCESS_POINT = "access_point"
ATTR_ESSID = "essid"
ATTR_IP_ADDRESS = "ip_address"
ATTR_OS = "os"
ATTR_CHANNEL = "channel"
ATTR_SIGNAL = "signal"
ATTR_SPEED = "speed"

# Storage key for last-seen timestamps
STORAGE_KEY = f"{DOMAIN}.last_seen"
STORAGE_VERSION = 1
