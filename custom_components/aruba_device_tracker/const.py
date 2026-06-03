"""Constants for the Aruba IAP Device Tracker integration."""

DOMAIN = "aruba_iap"

CONF_TRACK_NEW = "track_new_devices"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_TRACK_NEW = False
DEFAULT_PORT = 4343
DEFAULT_SCAN_INTERVAL = 30  # seconds
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300

# Client data attribute keys
ATTR_ACCESS_POINT = "access_point"
ATTR_ESSID = "essid"
ATTR_IP_ADDRESS = "ip_address"
ATTR_OS = "os"
ATTR_CHANNEL = "channel"
ATTR_SIGNAL = "signal"
ATTR_SPEED = "speed"
