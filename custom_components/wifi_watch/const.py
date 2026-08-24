"""Constants for wifi_watch."""

DOMAIN = "wifi_watch"

CONF_UNIFI_HOST = "unifi_host"
CONF_UNIFI_SITE_ID = "unifi_site_id"
CONF_UNIFI_SITE_NAME = "unifi_site_name"
CONF_UNIFI_API_KEY = "unifi_api_key"
CONF_UNIFI_USERNAME = "unifi_username"
CONF_UNIFI_PASSWORD = "unifi_password"

DEFAULT_UNIFI_SITE_NAME = "default"

OPT_POLL_INTERVAL_SECONDS = "poll_interval_seconds"
OPT_TOKEN_EXPIRE_SECONDS = "token_expire_seconds"
OPT_RETENTION_WINDOW_SECONDS = "retention_window_seconds"
OPT_NOTIFY_DEBOUNCE_SECONDS = "notify_debounce_seconds"
OPT_AUTO_BLOCK = "auto_block"
OPT_EXCLUDED_NOTIFY_TARGETS = "excluded_notify_targets"

DEFAULT_POLL_INTERVAL_SECONDS = 7
DEFAULT_TOKEN_EXPIRE_SECONDS = 24 * 3600
DEFAULT_RETENTION_WINDOW_SECONDS = 30 * 24 * 3600
DEFAULT_NOTIFY_DEBOUNCE_SECONDS = 90
DEFAULT_AUTO_BLOCK = False
DEFAULT_EXCLUDED_NOTIFY_TARGETS: list[str] = []

EVENT_NOTIFICATION_ACTION = "mobile_app_notification_action"
ACTION_PREFIX = "WIFI_WATCH::"

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "wifi_watch"
