"""Battery monitor configuration - modify settings.json instead of this file."""

import json
import os
import sys
import logging

logger = logging.getLogger(__name__)

# Determine directory of this config file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

if not os.path.exists(SETTINGS_PATH):
    print(f"ERROR: {SETTINGS_PATH} not found. Please copy settings.example.json to settings.json and edit it.")
    sys.exit(1)

with open(SETTINGS_PATH, "r") as f:
    settings = json.load(f)

# ─── Dynamic Settings from settings.json ───────────────────────────────────────
BATTERIES = settings.get("batteries", [])
MQTT_HOST = settings.get("mqtt_host", "127.0.0.1")
MQTT_PORT = settings.get("mqtt_port", 1883)
MQTT_USERNAME = settings.get("mqtt_username", "")
MQTT_PASSWORD = settings.get("mqtt_password", "")
MQTT_CLIENT_ID = settings.get("mqtt_client_id", "litime-battery-monitor")
BATTERY_MODEL = settings.get("battery_model", "LiFePO4")
LOG_FILE = settings.get("log_file", "battery_monitor.log")

# ─── Static / Dynamic Settings ─────────────────────────────────────────────────
# Delay between full polling cycles
CYCLE_DELAY_SECONDS = settings.get("cycle_delay_seconds", 5)

# Delay before moving to next battery IF the current one fails
BATTERY_OFFSET_SECONDS = 15

# ─── Runtime Validation ────────────────────────────────────────────────────────
def validate_config() -> None:
    """Validates configuration after logging is set up.
    Must be called from main() after setup_logging() so warnings reach the log file.
    """
    # Check for duplicate battery IDs
    ids = [b['id'] for b in BATTERIES]
    if len(ids) != len(set(ids)):
        duplicates = [bid for bid in set(ids) if ids.count(bid) > 1]
        logger.error(
            "CONFIGURATION ERROR: duplicate battery IDs found: %s. "
            "Each battery must have a unique id. Fix settings.json and restart.",
            duplicates,
        )
        sys.exit(1)

    if MAX_CONCURRENT_BLE_CONNECTIONS < 1:
        logger.error(
            "CONFIGURATION ERROR: max_concurrent_ble_connections must be at least 1. "
            "A value of %d would prevent all BLE connections. Fix settings.json and restart.",
            MAX_CONCURRENT_BLE_CONNECTIONS
        )
        sys.exit(1)

# Maximum concurrent BLE connections. Increase only if you know your Bluetooth adapter
# can handle multiple connections simultaneously without dropping them.
MAX_CONCURRENT_BLE_CONNECTIONS = settings.get("max_concurrent_ble_connections", 1)

# Pause after a successful BLE disconnect, before connecting to the next device.
# Helps the Bluetooth adapter clean up state.
BLE_ADAPTER_SETTLE_SECONDS = settings.get("ble_adapter_settle_seconds", 1.0)

# Pause after sending a BLE control command, before querying BMS status.
# Increase if charge/discharge commands fail intermittently on your BMS firmware.
BLE_COMMAND_SETTLE_SECONDS = settings.get("ble_command_settle_seconds", 0.5)
BLE_TIMEOUT_SECONDS = settings.get("ble_timeout_seconds", 10)
MAX_FAILURES_BEFORE_OFFLINE = 3

MQTT_TOPIC_PREFIX = "solar/battery"
MQTT_DISCOVERY_PREFIX = "homeassistant"

# ─── LiTime BLE Protocol ───────────────────────────────────────────────────────
BLE_SERVICE_UUID    = "0000ffe0-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_UUID     = "0000ffe1-0000-1000-8000-00805f9b34fb"
BLE_WRITE_UUID      = "0000ffe2-0000-1000-8000-00805f9b34fb"

# Query status command: {0x00, 0x00, 0x04, 0x01, 0x13, 0x55, 0xAA, 0x17}
CMD_QUERY_STATUS = bytes([0x00, 0x00, 0x04, 0x01, 0x13, 0x55, 0xAA, 0x17])
BLE_MIN_RESPONSE_LENGTH = 104

# --- BLE Commands ---
CMD_CHARGE_ON = 0x0A
CMD_CHARGE_OFF = 0x0B
CMD_DISCHARGE_ON = 0x0C
CMD_DISCHARGE_OFF = 0x0D

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_MAX_BYTES  = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3
