"""Constants for the Aruba IAP Device Tracker integration."""

DOMAIN = "aruba_iap"

CONF_TRACK_NEW = "track_new_devices"

DEFAULT_TRACK_NEW = False
DEFAULT_PORT = 4343

# How often to poll the IAP (seconds)
SCAN_INTERVAL_SECONDS = 30

# Client data keys
ATTR_ACCESS_POINT = "access_point"
ATTR_ESSID = "essid"
ATTR_IP_ADDRESS = "ip_address"
ATTR_OS = "os"
ATTR_CHANNEL = "channel"
ATTR_SIGNAL = "signal"
ATTR_SPEED = "speed"
