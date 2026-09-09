"""BLE connection to a LiTime battery, status query, disconnect."""

import asyncio
import logging
import struct
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError

from config import (
    BLE_MIN_RESPONSE_LENGTH,
    BLE_TIMEOUT_SECONDS,
    CMD_QUERY_STATUS,
)

logger = logging.getLogger(__name__)

# Per-battery lock: prevents concurrent BLE connections to the same device.
# A command (send_battery_command) and a poll (read_battery) must never run
# simultaneously on the same battery — BLE allows only one central connection.
_battery_locks: dict[int, asyncio.Lock] = {}


def _get_lock(battery_id: int) -> asyncio.Lock:
    """Return (creating if needed) the asyncio Lock for a given battery."""
    if battery_id not in _battery_locks:
        _battery_locks[battery_id] = asyncio.Lock()
    return _battery_locks[battery_id]


# Valid response marker: byte[2] == 0x65
RESPONSE_MARKER_OFFSET = 2
RESPONSE_MARKER_VALUE  = 0x65

# Protection flags (from HACS integration const.py)
PROTECTION_FLAGS = {
    0x00000004: "Overcharge",
    0x00000020: "Over-discharge",
    0x00000040: "Charge overcurrent",
    0x00000080: "Discharge overcurrent",
    0x00000100: "High temp 1",
    0x00000200: "High temp 2",
    0x00000400: "Low temp 1",
    0x00000800: "Low temp 2",
    0x00004000: "Short circuit",
}

MAX_CELLS = 16


def _parse_response(data: bytes) -> dict[str, Any]:
    """
    Parses BMS response (104+ bytes, little-endian).
    Offsets verified from HACS integration source code.
    """
    if len(data) < BLE_MIN_RESPONSE_LENGTH:
        raise ValueError(
            f"Response too short: {len(data)} bytes, minimum {BLE_MIN_RESPONSE_LENGTH}"
        )

    result: dict[str, Any] = {}

    # Total voltage (bytes 12-15, uint32_le, mV -> V)
    total_voltage = struct.unpack_from("<I", data, 12)[0] / 1000.0
    result["total_voltage"] = round(total_voltage, 3)

    # Individual cell voltages (bytes 16-47, 16x uint16_le, mV -> V)
    min_cell = 99.0
    max_cell = 0.0
    cell_count = 0
    cell_voltages: list[float | None] = [None] * MAX_CELLS

    for i in range(MAX_CELLS):
        raw = struct.unpack_from("<H", data, 16 + i * 2)[0]
        if raw == 0:
            continue
        cell_v = raw / 1000.0
        cell_voltages[i] = round(cell_v, 3)
        cell_count += 1
        min_cell = min(min_cell, cell_v)
        max_cell = max(max_cell, cell_v)

    result["cell_voltages"] = cell_voltages
    if cell_count > 0:
        result["min_cell_voltage"]   = round(min_cell, 3)
        result["max_cell_voltage"]   = round(max_cell, 3)
        result["delta_cell_voltage"] = round(max_cell - min_cell, 3)
    else:
        result["min_cell_voltage"]   = None
        result["max_cell_voltage"]   = None
        result["delta_cell_voltage"] = None

    # Current (bytes 48-51, int32_le, mA -> A) - negative = discharging
    current = struct.unpack_from("<i", data, 48)[0] / 1000.0
    result["current"] = round(current, 3)

    # Power (calculated)
    result["power"] = round(total_voltage * current, 1)

    # Cell temperature (bytes 52-53, int16_le, C)
    result["cell_temperature"] = struct.unpack_from("<h", data, 52)[0]

    # MOSFET temperature (bytes 54-55, int16_le, C)
    result["mosfet_temperature"] = struct.unpack_from("<h", data, 54)[0]

    # Remaining capacity (bytes 62-63, uint16_le, x0.01 Ah -> Ah)
    result["remaining_capacity"] = struct.unpack_from("<H", data, 62)[0] / 100.0

    # Full charge capacity (bytes 64-65, uint16_le, x0.01 Ah -> Ah)
    result["full_charge_capacity"] = struct.unpack_from("<H", data, 64)[0] / 100.0

    # Heat / discharge enabled state (bytes 68-71, uint32_le, bit 0x80 = discharge disabled)
    heat_state = struct.unpack_from("<I", data, 68)[0]
    result["discharge_enabled"] = not bool(heat_state & 0x00000080)

    # Protection flags (bytes 76-79, uint32_le)
    protection_flags = struct.unpack_from("<I", data, 76)[0]
    if protection_flags == 0:
        result["protection_status"] = "OK"
    else:
        active = [name for val, name in PROTECTION_FLAGS.items() if protection_flags & val]
        result["protection_status"] = ", ".join(active) if active else "OK"

    # Failure flags (bytes 80-83, uint32_le)
    failure_flags = struct.unpack_from("<I", data, 80)[0]
    result["failure_status"] = "OK" if failure_flags == 0 else f"Error: 0x{failure_flags:08X}"

    # Balancing state (bytes 84-87, uint32_le)
    balancing_state = struct.unpack_from("<I", data, 84)[0]
    result["balancing"] = balancing_state != 0

    # Battery state (bytes 88-89, uint16_le): 0=discharge, 1=charge, 4=charge_disabled
    battery_state = struct.unpack_from("<H", data, 88)[0]
    result["charging"]       = battery_state == 0x0001
    result["discharging"]    = battery_state == 0x0000 and current < 0
    result["charge_enabled"] = battery_state != 0x0004

    # SOC (bytes 90-91, uint16_le, %)
    result["state_of_charge"] = struct.unpack_from("<H", data, 90)[0]

    # SOH (bytes 92-93, uint16_le, %)
    result["state_of_health"] = struct.unpack_from("<H", data, 92)[0]

    # Discharge cycles (bytes 96-99, uint32_le)
    result["discharge_cycles"] = struct.unpack_from("<I", data, 96)[0]

    # Total discharged (bytes 100-103, uint32_le, mAh -> Ah)
    result["total_discharge_ah"] = struct.unpack_from("<I", data, 100)[0] / 1000.0

    result["online"] = True
    return result


async def read_battery(mac: str, battery_id: int) -> dict[str, Any] | None:
    """
    Connects to the battery, reads BMS data, disconnects.
    Returns dict with data or None on failure.
    Uses a per-battery lock to prevent concurrent BLE connections.
    """
    async with _get_lock(battery_id):
        response_buffer = bytearray()
        response_event  = asyncio.Event()
        response_data: bytes | None = None

        def notification_handler(characteristic, data: bytearray) -> None:
            nonlocal response_data
            # Detect start of a valid response (byte[2] == 0x65)
            if (
                len(data) > RESPONSE_MARKER_OFFSET
                and data[RESPONSE_MARKER_OFFSET] == RESPONSE_MARKER_VALUE
            ):
                response_buffer.clear()
                response_buffer.extend(data)
            elif len(response_buffer) > 0:
                response_buffer.extend(data)

            # Check if we have the complete response
            if len(response_buffer) >= BLE_MIN_RESPONSE_LENGTH:
                response_data = bytes(response_buffer)
                response_buffer.clear()
                response_event.set()

        logger.debug("Battery %d (%s): connecting...", battery_id, mac)

        try:
            async with BleakClient(mac, timeout=BLE_TIMEOUT_SECONDS) as client:
                if not client.is_connected:
                    logger.warning("Battery %d (%s): connection failed", battery_id, mac)
                    return None

                logger.debug("Battery %d (%s): connected, sending query...", battery_id, mac)

                # Find notify characteristic (FFE1)
                notify_char = None
                write_char  = None
                for service in client.services:
                    if "ffe0" in service.uuid.lower():
                        for char in service.characteristics:
                            if "ffe1" in char.uuid.lower() and "notify" in char.properties:
                                notify_char = char
                            if "ffe2" in char.uuid.lower() and (
                                "write" in char.properties
                                or "write-without-response" in char.properties
                            ):
                                write_char = char

                if not notify_char or not write_char:
                    logger.error(
                        "Battery %d (%s): characteristics FFE1/FFE2 not found", battery_id, mac
                    )
                    return None

                # Subscribe to notifications
                await client.start_notify(notify_char, notification_handler)

                # Send query status command
                use_response = "write-without-response" not in write_char.properties
                await client.write_gatt_char(write_char, CMD_QUERY_STATUS, response=use_response)

                # Wait for response (timeout 10s)
                try:
                    await asyncio.wait_for(response_event.wait(), timeout=BLE_TIMEOUT_SECONDS)
                except TimeoutError:
                    logger.warning(
                        "Battery %d (%s): timeout waiting for response", battery_id, mac
                    )
                    return None

                if response_data is None:
                    logger.warning("Battery %d (%s): no data in response", battery_id, mac)
                    return None

                # Parse response
                try:
                    result = _parse_response(response_data)
                    logger.debug(
                        "Battery %d (%s): clean disconnect after successful read",
                        battery_id, mac
                    )
                    return result
                except (ValueError, struct.error) as err:
                    logger.error("Battery %d (%s): parsing error - %s", battery_id, mac, err)
                    return None

        except (BleakError, TimeoutError, OSError) as err:
            logger.warning("Battery %d (%s): BLE error - %s", battery_id, mac, err)
            return None


def _build_command(cmd: int) -> bytes:
    checksum = 0x04 + cmd
    return bytes([0x00, 0x00, 0x04, 0x01, cmd, 0x55, 0xAA, checksum & 0xFF])


async def send_battery_command(mac: str, battery_id: int, cmd: int) -> dict[str, Any] | None:
    """Send a control command and immediately read back BMS status in the same BLE session.

    LiTime BMS resets charge/discharge state when the BLE connection closes.
    By keeping the connection open between the control command and the status
    query, we read the real post-command state -- exactly as the HACS integration
    does via its persistent connection + immediate coordinator refresh.

    Returns the parsed BMS data dict on success, or None on failure.
    Uses the per-battery lock to prevent race conditions with the polling loop.
    """
    logger.info("Battery %d (%s): sending command 0x%02X...", battery_id, mac, cmd)

    async with _get_lock(battery_id):
        response_buffer = bytearray()
        response_event  = asyncio.Event()
        response_data: bytes | None = None

        def notification_handler(characteristic, data: bytearray) -> None:
            nonlocal response_data
            if (
                len(data) > RESPONSE_MARKER_OFFSET
                and data[RESPONSE_MARKER_OFFSET] == RESPONSE_MARKER_VALUE
            ):
                response_buffer.clear()
                response_buffer.extend(data)
            elif len(response_buffer) > 0:
                response_buffer.extend(data)

            if len(response_buffer) >= BLE_MIN_RESPONSE_LENGTH:
                response_data = bytes(response_buffer)
                response_buffer.clear()
                response_event.set()

        try:
            async with BleakClient(mac, timeout=BLE_TIMEOUT_SECONDS) as client:
                if not client.is_connected:
                    logger.warning("Battery %d: connect failed for command", battery_id)
                    return None

                notify_char = None
                write_char  = None
                for service in client.services:
                    if "ffe0" in service.uuid.lower():
                        for char in service.characteristics:
                            if "ffe1" in char.uuid.lower() and "notify" in char.properties:
                                notify_char = char
                            if "ffe2" in char.uuid.lower() and (
                                "write" in char.properties
                                or "write-without-response" in char.properties
                            ):
                                write_char = char

                if not notify_char or not write_char:
                    logger.error("Battery %d: characteristics not found for command", battery_id)
                    return None

                use_response = "write-without-response" not in write_char.properties

                # Subscribe to notifications before sending anything
                await client.start_notify(notify_char, notification_handler)

                # Step 1: send the control command (charge on/off, discharge on/off)
                frame = _build_command(cmd)
                await client.write_gatt_char(write_char, frame, response=use_response)
                logger.debug("Battery %d: command 0x%02X sent", battery_id, cmd)

                # Step 2: brief pause so the BMS can process the command.
                # Any notification the BMS sends in response to the control command
                # won't have the 0x65 marker, so it will be ignored by the handler.
                await asyncio.sleep(0.5)

                # Step 3: reset response state, then query actual BMS status.
                # This mirrors what the HACS coordinator does: send command ->
                # clear state -> send query -> wait for notification.
                response_buffer.clear()
                response_event.clear()
                response_data = None

                await client.write_gatt_char(write_char, CMD_QUERY_STATUS, response=use_response)

                # Step 4: wait for the status notification
                try:
                    await asyncio.wait_for(response_event.wait(), timeout=BLE_TIMEOUT_SECONDS)
                except TimeoutError:
                    logger.warning(
                        "Battery %d: timeout waiting for status after command", battery_id
                    )
                    return None

                if response_data is None:
                    logger.warning("Battery %d: no status data after command", battery_id)
                    return None

                try:
                    result = _parse_response(response_data)
                    logger.info(
                        "Battery %d: BMS confirmed -- charge_enabled=%s discharge_enabled=%s",
                        battery_id,
                        result.get("charge_enabled"),
                        result.get("discharge_enabled"),
                    )
                    return result
                except (ValueError, struct.error) as err:
                    logger.error("Battery %d: parse error after command: %s", battery_id, err)
                    return None

        except (BleakError, TimeoutError, OSError) as err:
            logger.error("Battery %d: BLE error during command: %s", battery_id, err)
            return None
