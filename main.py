"""Entry point - sequential polling loop."""

import asyncio
import logging
import logging.handlers
import signal
import sys

from cache import CacheManager
from config import (
    BATTERIES,
    BATTERY_OFFSET_SECONDS,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    POLL_CYCLE_SECONDS,
)
from mqtt_client import MqttClient
from poller import read_battery, send_battery_command
from config import CMD_CHARGE_ON, CMD_CHARGE_OFF, CMD_DISCHARGE_ON, CMD_DISCHARGE_OFF, validate_config


def setup_logging() -> None:
    """Configures logging with file rotation."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Console handler (useful when running manually)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)


logger = logging.getLogger(__name__)


# CONNECTION_STATE is only read/written from within the asyncio event loop
# (process_command and poll_one_battery both run on the loop via run_coroutine_threadsafe),
# so no additional locking is needed.
CONNECTION_STATE = {b['id']: True for b in BATTERIES}


async def process_command(bid: int, switch_id: str, payload: str, cache, mqtt):
    is_on = payload == "ON"
    battery = next((b for b in BATTERIES if b["id"] == bid), None)
    if not battery: return

    if switch_id == "connection":
        CONNECTION_STATE[bid] = is_on
        mqtt._client.publish(f"solar/battery/{bid}/connection", "ON" if is_on else "OFF", retain=True)
        if not is_on:
            # Mark battery as offline in cache so system counters reflect
            # the disabled connection correctly, then update HA immediately.
            cache.get(bid).force_offline()
            mqtt.publish_battery(bid, None)
        else:
            # Reconnected: trigger an immediate poll so data appears in HA
            # without waiting up to 60s for the next scheduled poll_cycle.
            await poll_one_battery(battery, cache, mqtt)

        # Update Solar Battery System counters immediately (Batteries Offline,
        # All Batteries Online) without waiting for the next poll_cycle.
        mqtt.publish_system(cache.offline_count(), cache.all_online())
        logger.info("Battery %d: Connection turned %s", bid, "ON" if is_on else "OFF")
        return

    cmd = None
    if switch_id == "charge":
        cmd = CMD_CHARGE_ON if is_on else CMD_CHARGE_OFF
    elif switch_id == "discharge":
        cmd = CMD_DISCHARGE_ON if is_on else CMD_DISCHARGE_OFF

    if cmd is not None:
        # Publish the commanded state immediately so HA shows the change at once,
        # without waiting for the BLE round-trip (~2-3 s). This avoids the brief
        # ON blip caused by the retained state_topic value still being in effect
        # while we are waiting. The BMS-confirmed state overwrites this shortly after.
        mqtt._client.publish(
            f"solar/battery/{bid}/{switch_id}", payload, retain=True
        )

        # send_battery_command sends the command AND immediately reads back
        # BMS status in the same BLE session, so the returned data reflects the
        # real post-command state (the BMS resets on disconnect).
        data = await send_battery_command(battery["mac"], bid, cmd)
        if data is not None:
            data["connection_enabled"] = CONNECTION_STATE.get(bid, True)
            bat_cache = cache.get(bid)
            bat_cache.record_success(data)
            # Publish BMS-confirmed state (overwrites the optimistic publish above)
            mqtt.publish_battery(bid, bat_cache.get_publish_data())
            logger.info(
                "Battery %d: %s switch confirmed by BMS (charge_enabled=%s discharge_enabled=%s)",
                bid, switch_id,
                data.get("charge_enabled"),
                data.get("discharge_enabled"),
            )
        else:
            # BLE failed: revert the optimistic publish by re-publishing last known state
            bat_cache = cache.get(bid)
            cached = bat_cache.get_publish_data()
            if cached:
                mqtt.publish_battery(bid, cached)
            logger.warning(
                "Battery %d: %s command failed or BMS read failed -- reverted to cached state",
                bid, switch_id,
            )

async def poll_one_battery(
    battery: dict,
    cache: CacheManager,
    mqtt: MqttClient,
) -> None:
    """Polling for a single battery: connect -> read -> cache -> MQTT -> disconnect."""
    bid = battery["id"]
    mac = battery["mac"]

    
    if not CONNECTION_STATE.get(bid, True):
        logger.debug("Battery %d: Connection disabled, skipping poll", bid)
        # Ensure connection switch state is published even when skipping
        mqtt._client.publish(f"solar/battery/{bid}/connection", "OFF", retain=True)
        return

    if not mac:
        logger.warning("Battery %d: MAC address not configured - skipping", bid)
        return

    data = await read_battery(mac, bid)

    # Re-check CONNECTION_STATE after BLE read: the user may have disabled the
    # connection while the read was in progress. If so, discard the data and
    # mark the battery offline so the UI stays consistent.
    if not CONNECTION_STATE.get(bid, True):
        logger.debug(
            "Battery %d: Connection was disabled during BLE read - discarding data",
            bid,
        )
        cache.get(bid).force_offline()
        mqtt._client.publish(f"solar/battery/{bid}/connection", "OFF", retain=True)
        mqtt.publish_battery(bid, None)
        return

    bat_cache = cache.get(bid)

    if data is not None:
        data["connection_enabled"] = CONNECTION_STATE.get(bid, True)
        bat_cache.record_success(data)

    else:
        bat_cache.record_failure()

    # Publish to MQTT - real data or None (-> offline)
    mqtt.publish_battery(bid, bat_cache.get_publish_data())


async def poll_cycle(cache: CacheManager, mqtt: MqttClient) -> None:
    """
    One full cycle: sequential polling for all batteries.
    We have a BATTERY_OFFSET_SECONDS offset between consecutive batteries.
    """
    logger.info("--- Start polling cycle --- %s", cache.summary())

    tasks = []
    for i, battery in enumerate(BATTERIES):
        # Create a task with delay for each battery
        delay = i * BATTERY_OFFSET_SECONDS
        tasks.append(_delayed_poll(battery, cache, mqtt, delay))

    await asyncio.gather(*tasks)

    # Publish global system state
    offline = cache.offline_count()
    all_ok  = cache.all_online()
    mqtt.publish_system(offline, all_ok)

    if offline > 0:
        logger.warning("SYSTEM: %d battery/batteries offline", offline)
    else:
        logger.info("SYSTEM: all batteries online")


async def _delayed_poll(
    battery: dict,
    cache: CacheManager,
    mqtt: MqttClient,
    delay: float,
) -> None:
    """Waits for delay seconds then polls the battery."""
    if delay > 0:
        await asyncio.sleep(delay)
    await poll_one_battery(battery, cache, mqtt)


async def main() -> None:
    """Main loop: polling every POLL_CYCLE_SECONDS seconds."""
    setup_logging()
    validate_config()
    logger.info("=" * 60)
    logger.info("Battery Monitor started")
    logger.info("Configured batteries: %d", len(BATTERIES))
    logger.info("Polling cycle: %ds, offset: %ds", POLL_CYCLE_SECONDS, BATTERY_OFFSET_SECONDS)
    logger.info("=" * 60)

    # Verify configuration
    unconfigured = [b["name"] for b in BATTERIES if not b["mac"]]
    if unconfigured:
        logger.warning(
            "Batteries without configured MAC address: %s",
            ", ".join(unconfigured),
        )
        logger.warning("Edit config.py and fill in the 'mac' fields")

    # Initialize MQTT
    mqtt = MqttClient()
    mqtt.connect()

    # Publish MQTT Discovery (once at startup)
    await asyncio.sleep(2)  # wait for MQTT connection (non-blocking)
    mqtt.publish_discovery()

    main_loop = asyncio.get_running_loop()

    # Initialize cache
    cache = CacheManager(BATTERIES)

    # Setup MQTT Command Handler
    def _mqtt_command_handler(bid, switch_id, payload):
        asyncio.run_coroutine_threadsafe(
            process_command(bid, switch_id, payload, cache, mqtt),
            main_loop
        )
    mqtt.command_callback = _mqtt_command_handler

    # Handle SIGTERM signal (for systemd stop)
    stop_event = asyncio.Event()

    def _handle_stop(signum, frame):
        logger.info("Signal %d received, shutting down cleanly...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    # Main loop
    try:
        while not stop_event.is_set():
            cycle_start = asyncio.get_running_loop().time()

            try:
                await poll_cycle(cache, mqtt)
            except Exception as err:
                logger.error("Unexpected error in cycle: %s", err, exc_info=True)

            # Wait for the remainder of the cycle (POLL_CYCLE_SECONDS total)
            elapsed = asyncio.get_running_loop().time() - cycle_start
            wait = max(0, POLL_CYCLE_SECONDS - elapsed)
            logger.debug("Cycle took %.1fs, next in %.1fs", elapsed, wait)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
            except TimeoutError:
                pass  # normal timeout, continue to next cycle

    finally:
        logger.info("Battery Monitor stopped")
        mqtt.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
