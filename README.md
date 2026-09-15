# LiTime BLE Battery Monitor for Home Assistant

A robust Python service that monitors and controls multiple **LiTime LiFePO4 Bluetooth batteries** simultaneously, publishing all data to **Home Assistant** via MQTT Auto-Discovery.

## The Problem This Solves

LiTime batteries expose a BLE (Bluetooth Low Energy) interface for monitoring. The official LiTime app connects to one battery at a time. The [HACS LiTime integration](https://github.com/ItzBenoitXD/litime) works great for 1–2 batteries, but hits a hard wall when you have **more than 2–3 batteries on the same Bluetooth adapter**: the OS BLE stack struggles with concurrent connections, leading to dropped connections and missing data.

This service solves the problem by:
- Connecting to each battery **sequentially**, one at a time, to guarantee maximum BLE stability
- Using a **global asyncio semaphore** to prevent any concurrent BLE connections on the same adapter
- Running as a **systemd service** in the background, polling continuously with a configurable small delay between cycles
- Publishing everything to Home Assistant via **MQTT with Auto-Discovery** — no manual entity configuration needed

## Features

- 🔋 **Supports any number of batteries** — configured dynamically via `settings.json`
- 🔵 **Sequential dynamic BLE polling** with configurable backoff on errors
- 🏠 **Home Assistant Auto-Discovery** — all sensors, binary sensors and switches appear automatically
- ⚡ **Charge / Discharge control** — toggle BMS charge and discharge directly from HA
- 🔌 **Connection switch** — disable/enable individual battery monitoring from HA without stopping the service
- 📊 **Rich sensor data** per battery:
  - State of Charge (%), Voltage (V), Current (A), Power (W)
  - Cell temperatures, MOSFET temperature
  - Individual cell voltages, min/max/delta cell voltage
  - Remaining capacity, full charge capacity, State of Health
  - Discharge cycles, total discharged (Ah)
  - Protection status, Failure status, Balancing state
- 🔄 **Auto-recovery** — graceful handling of BLE timeouts with configurable failure threshold before marking offline
- 📝 **Rotating log file** — with configurable size and backup count

## Requirements

- Linux with BlueZ (tested on Ubuntu 24.04)
- Python 3.10+
- Bluetooth adapter accessible to the service user
- MQTT broker (e.g. Mosquitto) reachable from the host
- Home Assistant with MQTT integration enabled

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/iulica76/litime-ble-monitor.git
cd litime-ble-monitor
```

### 2. Create a Python virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp settings.example.json settings.json
nano settings.json
```

Edit `settings.json` with your values:

```json
{
  "mqtt_host": "192.168.1.100",
  "mqtt_port": 1883,
  "mqtt_username": "your_mqtt_user",
  "mqtt_password": "your_mqtt_password",
  "mqtt_client_id": "litime-battery-monitor",
  "battery_model": "24V 100Ah LiFePO4",
  "batteries": [
    {"id": 1, "name": "Solar_Batt_1", "mac": "XX:XX:XX:XX:XX:XX"},
    {"id": 2, "name": "Solar_Batt_2", "mac": "XX:XX:XX:XX:XX:XX"}
  ],
  "log_file": "/var/log/battery_monitor.log",
  "cycle_delay_seconds": 5,
  "ble_timeout_seconds": 10,
  "ble_command_settle_seconds": 0.5,
  "ble_adapter_settle_seconds": 1.0,
  "max_concurrent_ble_connections": 1
}
```

> **Finding your battery MAC address:** Use `bluetoothctl scan on` and look for devices named `LiTime-*` or similar. You can also check the LiTime mobile app's device info screen.

### 4. Run manually (for testing)

```bash
source venv/bin/activate
python3 main.py
```

### 5. Install as a systemd service (recommended)

```bash
sudo nano /etc/systemd/system/solar-battery-monitor.service
```

```ini
[Unit]
Description=LiTime Solar Battery BLE Monitor
After=network.target bluetooth.target
StartLimitBurst=5
StartLimitIntervalSec=120

[Service]
Type=simple
User=root
WorkingDirectory=/opt/litime-ble-monitor
ExecStart=/opt/litime-ble-monitor/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable solar-battery-monitor
sudo systemctl start solar-battery-monitor
sudo systemctl status solar-battery-monitor
```

## Configuration Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `cycle_delay_seconds` | `5` | Delay between consecutive full polling cycles (in `settings.json`) |
| `BATTERY_OFFSET_SECONDS` | `15` | Delay/backoff to wait after a FAILED battery poll |
| `ble_timeout_seconds` | `10` | BLE connection/response timeout per battery (in `settings.json`) |
| `MAX_FAILURES_BEFORE_OFFLINE` | `3` | Consecutive failures before marking battery offline |
| `ble_command_settle_seconds` | `0.5` | Pause after a BLE control command before querying BMS status (in `settings.json`) |
| `ble_adapter_settle_seconds` | `1.0` | Pause after a successful BLE disconnect, before connecting to the next device (in `settings.json`) |
| `max_concurrent_ble_connections` | `1` | Max simultaneous BLE connections. Keep at 1 for adapter stability (in `settings.json`) |

> **Note on polling time:** The monitor polls batteries sequentially, one by one. In normal conditions, a full cycle takes about 1-2 seconds per battery. If batteries are offline, the total cycle time will automatically extend to accommodate timeouts and backoffs.

## Architecture

```
main.py          — asyncio main loop, MQTT command handler, poll orchestration
poller.py        — BLE connect → query → parse → disconnect (per battery)
mqtt_client.py   — paho-mqtt wrapper with HA Auto-Discovery support
cache.py         — per-battery state cache with failure counting
config.py        — loads settings.json, defines all constants and BLE protocol
```

## BLE Protocol

The service communicates with the LiTime BMS over the `FFE0` GATT service:
- **Write:** characteristic `FFE2` — sends query/command frames
- **Notify:** characteristic `FFE1` — receives BMS status responses (104+ bytes)

Response parsing offsets were verified against the [HACS LiTime integration](https://github.com/ItzBenoitXD/litime) source code.

## Troubleshooting

**Battery not connecting:**
- Ensure no other device (phone, another process) is currently connected to the battery — BLE allows only one central connection at a time
- Check that the Bluetooth adapter is up: `hciconfig`
- Try increasing `ble_timeout_seconds` in `settings.json` for slower adapters

**Sensors not appearing in Home Assistant:**
- Check that MQTT integration is enabled and connected in HA
- Verify MQTT broker credentials in `settings.json`
- Check logs: `journalctl -u solar-battery-monitor -f`

**Multiple batteries going offline:**
- Increase `ble_adapter_settle_seconds` in `settings.json` to give the Bluetooth adapter more time to recover between device connections
- Check for BLE adapter saturation: `dmesg | grep -i bluetooth`

**Service immediately crashes / restart loop:**
- Check for duplicate battery IDs in `settings.json`. The service will intentionally abort on startup if two batteries share the same ID to prevent cache corruption.

## Credits

This project was developed by **Iulian** to solve a real-world problem with multiple LiTime batteries and Home Assistant.

The implementation was built in collaboration with AI assistants:

- 🤖 **Google Gemini** — architecture design, BLE protocol analysis, asyncio patterns, MQTT discovery structure
- 🤖 **Anthropic Claude** — code review, bug fixes, robustness improvements, edge case handling

> *A real-world example of human–AI pair programming: the human brought the hardware knowledge and the problem, the AIs brought the code.*

## License

MIT License — feel free to use, modify and share.

---

*Tested with LiTime 24V 100Ah LiFePO4 batteries. May work with other LiTime models that use the same BMS BLE protocol.*
