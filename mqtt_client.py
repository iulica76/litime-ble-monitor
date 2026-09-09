"""MQTT Client with Home Assistant auto-discovery support."""

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

from config import (
    BATTERIES,
    BATTERY_MODEL,
    MQTT_CLIENT_ID,
    MQTT_DISCOVERY_PREFIX,
    MQTT_HOST,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_TOPIC_PREFIX,
    MQTT_USERNAME,
)

logger = logging.getLogger(__name__)


def _topic(battery_id: int, field: str) -> str:
    return f"{MQTT_TOPIC_PREFIX}/{battery_id}/{field}"


def _availability_topic(battery_id: int) -> str:
    return _topic(battery_id, "availability")


# Sensor definitions published to HA via MQTT Discovery
# Format: (field_key, friendly_name, unit, device_class, icon)
SENSOR_DEFINITIONS = [
    ("state_of_charge",     "State of Charge",          "%",   "battery",     "mdi:battery"),
    ("total_voltage",       "Total Voltage",             "V",   "voltage",     "mdi:flash"),
    ("current",             "Current",                   "A",   "current",     "mdi:current-dc"),
    ("power",               "Power",                     "W",   "power",       "mdi:solar-power"),
    ("cell_temperature",    "Cell Temperature",          "°C",  "temperature", "mdi:thermometer"),
    ("mosfet_temperature",  "MOSFET Temperature",        "°C",  "temperature", "mdi:thermometer"),
    ("remaining_capacity",  "Remaining Capacity",        "Ah",  None,          "mdi:battery-charging"),
    ("full_charge_capacity","Full Charge Capacity",      "Ah",  None,          "mdi:battery-check"),
    ("state_of_health",     "State of Health",           "%",   None,          "mdi:heart-pulse"),
    ("discharge_cycles",    "Discharge Cycles",          None,  None,          "mdi:recycle"),
    ("total_discharge_ah",  "Total Discharge",           "Ah",  "energy",      "mdi:battery-minus"),
    ("min_cell_voltage",    "Min Cell Voltage",          "V",   "voltage",     "mdi:battery-low"),
    ("max_cell_voltage",    "Max Cell Voltage",          "V",   "voltage",     "mdi:battery-high"),
    ("delta_cell_voltage",  "Delta Cell Voltage",        "V",   "voltage",     "mdi:delta"),
    ("protection_status",   "Protection Status",         None,  None,          "mdi:shield"),
    ("failure_status",      "Failure Status",            None,  None,          "mdi:alert-circle"),
]


SWITCH_DEFINITIONS = [
    ("charge", "Charge Enabled", "mdi:battery-charging"),
    ("discharge", "Discharge Enabled", "mdi:battery-arrow-down-outline"),
    ("connection", "Connection", "mdi:bluetooth-connect"),
]

BINARY_SENSOR_DEFINITIONS = [
    ("online",           "Online",            "connectivity", "mdi:bluetooth-connect"),
    ("charging",         "Charging",          "battery_charging", "mdi:battery-charging"),
    ("discharging",      "Discharging",       None,           "mdi:battery-arrow-down"),
    ("charge_enabled",   "Charge Enabled",    None,           "mdi:battery-plus"),
    ("discharge_enabled","Discharge Enabled", None,           "mdi:battery-minus"),
    ("balancing",        "Balancing",         None,           "mdi:battery-sync"),
]


class MqttClient:
    """Wrapper for paho-mqtt with HA Discovery support."""

    def __init__(self) -> None:
        self.command_callback = None
        self._client = mqtt.Client(client_id=MQTT_CLIENT_ID, protocol=mqtt.MQTTv5)
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        self._client.on_message    = self._on_message


        if MQTT_USERNAME:
            self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        # Will message: if script crashes, HA knows monitor is offline
        self._client.will_set(
            f"{MQTT_TOPIC_PREFIX}/monitor/status",
            payload="offline",
            qos=1,
            retain=True,
        )


    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        if rc == 0:
            logger.info("MQTT: connected to %s:%d", MQTT_HOST, MQTT_PORT)
            client.publish(f"{MQTT_TOPIC_PREFIX}/monitor/status", "online", qos=1, retain=True)
            client.subscribe(f"{MQTT_TOPIC_PREFIX}/+/set/+")
        else:
            logger.error("MQTT: connection error, code %d", rc)

    def _on_disconnect(self, client, userdata, rc, properties=None) -> None:
        logger.warning("MQTT: disconnected (code %d)", rc)

    def connect(self) -> None:
        """Connect to broker and start background loop."""
        self._client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        """Clean disconnect."""
        self._client.publish(
            f"{MQTT_TOPIC_PREFIX}/monitor/status", "offline", qos=1, retain=True
        )
        self._client.loop_stop()
        self._client.disconnect()

    
    def _on_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split("/")
            if len(parts) >= 4 and parts[-2] == "set":
                bid = int(parts[-3])
                switch_id = parts[-1]
                payload = msg.payload.decode().upper()
                if self.command_callback:
                    self.command_callback(bid, switch_id, payload)
        except Exception as e:
            logger.error("Error processing MQTT message: %s", e)

    def publish_discovery(self) -> None:
        """
        Publishes Discovery messages for HA.
        HA will automatically create all entities after receiving these.
        Called once at startup.
        """
        for battery in BATTERIES:
            bid  = battery["id"]
            name = battery["name"]

            device_info = {
                "identifiers": [f"litime_battery_{bid}"],
                "name": name,
                "manufacturer": "LiTime",
                "model": BATTERY_MODEL,
                "sw_version": "monitor-v1",
            }

            avail_topic = _availability_topic(bid)

            # Numeric / text sensors
            for field, friendly, unit, dev_class, icon in SENSOR_DEFINITIONS:
                unique_id = f"litime_bat{bid}_{field}"
                config = {
                    "unique_id":           unique_id,
                    "name":                friendly,
                    "state_topic":         _topic(bid, field),
                    "availability_topic":  avail_topic,
                    "payload_available":   "online",
                    "payload_not_available": "offline",
                    "device":              device_info,
                    "icon":                icon,
                }
                if unit:
                    config["unit_of_measurement"] = unit
                if dev_class:
                    config["device_class"] = dev_class
                if field not in ["protection_status", "failure_status"]:
                    config["state_class"] = "measurement"

                disc_topic = (
                    f"{MQTT_DISCOVERY_PREFIX}/sensor/{unique_id}/config"
                )
                self._client.publish(
                    disc_topic, json.dumps(config), qos=1, retain=True
                )

            
            # Switches
            for switch_id, friendly, icon in SWITCH_DEFINITIONS:
                unique_id = f"litime_bat{bid}_{switch_id}"
                
                # Connection switch uses monitor status for availability, others use battery availability
                av_topic = f"{MQTT_TOPIC_PREFIX}/monitor/status" if switch_id == "connection" else avail_topic
                
                config = {
                    "unique_id": unique_id,
                    "name": friendly,
                    "state_topic": _topic(bid, switch_id),
                    "command_topic": f"{MQTT_TOPIC_PREFIX}/{bid}/set/{switch_id}",
                    "availability_topic": av_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device": device_info,
                    "icon": icon,

                }
                disc_topic = f"{MQTT_DISCOVERY_PREFIX}/switch/{unique_id}/config"
                self._client.publish(disc_topic, json.dumps(config), qos=1, retain=True)

            # Binary sensors (on/off)
            for field, friendly, dev_class, icon in BINARY_SENSOR_DEFINITIONS:
                unique_id = f"litime_bat{bid}_{field}"
                config = {
                    "unique_id":             unique_id,
                    "name":                  friendly,
                    "state_topic":           _topic(bid, field),
                    "availability_topic":    avail_topic,
                    "payload_available":     "online",
                    "payload_not_available": "offline",
                    "payload_on":            "true",
                    "payload_off":           "false",
                    "device":                device_info,
                    "icon":                  icon,
                }
                if dev_class:
                    config["device_class"] = dev_class
                # Note: binary sensors do not use state_class (only numeric sensors do)

                disc_topic = (
                    f"{MQTT_DISCOVERY_PREFIX}/binary_sensor/{unique_id}/config"
                )
                self._client.publish(
                    disc_topic, json.dumps(config), qos=1, retain=True
                )

            logger.info("MQTT Discovery published for battery %d (%s)", bid, name)

        # Global sensor: offline batteries count
        global_device = {
            "identifiers": ["litime_solar_system"],
            "name": "Solar Battery System",
            "manufacturer": "LiTime",
        }
        self._client.publish(
            f"{MQTT_DISCOVERY_PREFIX}/sensor/litime_system_offline_count/config",
            json.dumps({
                "unique_id":   "litime_system_offline_count",
                "name":        "Batteries Offline",
                "state_topic": f"{MQTT_TOPIC_PREFIX}/system/batteries_offline",
                "icon":        "mdi:battery-alert",
                "device":      global_device,
            }),
            qos=1, retain=True,
        )

        # Global binary sensor: all online
        self._client.publish(
            f"{MQTT_DISCOVERY_PREFIX}/binary_sensor/litime_system_all_ok/config",
            json.dumps({
                "unique_id":    "litime_system_all_ok",
                "name":         "All Batteries Online",
                "state_topic":  f"{MQTT_TOPIC_PREFIX}/system/all_ok",
                "payload_on":   "true",
                "payload_off":  "false",
                "device_class": "connectivity",
                "icon":         "mdi:battery-check",
                "device":       global_device,
            }),
            qos=1, retain=True,
        )

        logger.info("MQTT Discovery completely published")

    def publish_battery(self, battery_id: int, data: dict[str, Any] | None) -> None:
        """
        Publishes data for a battery.
        - data=None  → marks battery offline (availability=offline)
        - data=dict  → publishes each field separately + availability=online
        """
        avail_topic = _availability_topic(battery_id)

        if data is None:
            self._client.publish(avail_topic, "offline", qos=1, retain=True)
            logger.debug("Battery %d: published as OFFLINE in MQTT", battery_id)
            return

        # Publish each field as a separate topic
        for field, *_ in SENSOR_DEFINITIONS + BINARY_SENSOR_DEFINITIONS:
            value = data.get(field)
            if value is None:
                continue
            # Booleans → "true"/"false" for payload_on/payload_off
            if isinstance(value, bool):
                payload = "true" if value else "false"
            else:
                payload = str(value)
            self._client.publish(_topic(battery_id, field), payload, qos=0, retain=True)

        
        # Publish switch state topics (solar/battery/{id}/charge, /discharge, /connection).
        # These are DIFFERENT from the binary sensor topics (/charge_enabled, /discharge_enabled)
        # published in the loop above. Switches use ON/OFF payloads; binary sensors use true/false.
        if data:
            if "charge_enabled" in data:
                self._client.publish(_topic(battery_id, "charge"), "ON" if data["charge_enabled"] else "OFF", retain=True)
            if "discharge_enabled" in data:
                self._client.publish(_topic(battery_id, "discharge"), "ON" if data["discharge_enabled"] else "OFF", retain=True)
            if "connection_enabled" in data:
                self._client.publish(_topic(battery_id, "connection"), "ON" if data["connection_enabled"] else "OFF", retain=True)

        # Mark battery online
        self._client.publish(avail_topic, "online", qos=1, retain=True)

    def publish_system(self, offline_count: int, all_ok: bool) -> None:
        """Publishes global system state."""
        self._client.publish(
            f"{MQTT_TOPIC_PREFIX}/system/batteries_offline",
            str(offline_count),
            qos=1,
            retain=True,
        )
        self._client.publish(
            f"{MQTT_TOPIC_PREFIX}/system/all_ok",
            "true" if all_ok else "false",
            qos=1,
            retain=True,
        )
