# LiTime BLE Battery Monitor for Home Assistant

A robust Python service that monitors and controls multiple **LiTime LiFePO4 Bluetooth batteries** simultaneously, publishing all data to **Home Assistant** via MQTT Auto-Discovery.

## The Problem This Solves

LiTime batteries expose a BLE (Bluetooth Low Energy) interface for monitoring. The official LiTime app connects to one battery at a time. The [HACS LiTime integration](https://github.com/ItzBenoitXD/litime) works great for 1–2 batteries, but hits a hard wall when you have **more than 2–3 batteries on the same Bluetooth adapter**: the OS BLE stack struggles with concurrent connections, leading to dropped connections and missing data.

This service solves the problem by:
- Connecting to each battery **sequentially**, one at a time, with configurable time offsets
- Using **per-battery asyncio locks** to prevent any concurrent BLE access
- Running as a **systemd service** in the background, polling every 60 seconds
- Publishing everything to Home Assistant via **MQTT with Auto-Discovery** — no manual entity configuration needed

## Features

- 🔋 **Supports any number of batteries** — configured dynamically via `settings.json`
- 🔵 **Sequential BLE polling** with configurable offsets to avoid adapter saturation
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
  "batteries": [
    {"id": 1, "name": "Solar_Batt_1", "mac": "XX:XX:XX:XX:XX:XX"},
    {"id": 2, "name": "Solar_Batt_2", "mac": "XX:XX:XX:XX:XX:XX"},
    {"id": 3, "name": "Solar_Batt_3", "mac": "XX:XX:XX:XX:XX:XX"},
    {"id": 4, "name": "Solar_Batt_4", "mac": "XX:XX:XX:XX:XX:XX"}
  ],
  "log_file": "/var/log/battery_monitor.log"
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

[Service]
Type=simple
User=root
WorkingDirectory=/opt/litime-ble-monitor
ExecStart=/opt/litime-ble-monitor/venv/bin/python main.py
Restart=always
RestartSec=10
StartLimitBurst=5
StartLimitIntervalSec=120
StartLimitBurst=5
StartLimitIntervalSec=120

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
| `POLL_CYCLE_SECONDS` | `60` | How often to poll all batteries (seconds) |
| `BATTERY_OFFSET_SECONDS` | `15` | Delay between consecutive battery polls (seconds) |
| `BLE_TIMEOUT_SECONDS` | `10` | BLE connection/response timeout per battery |
| `MAX_FAILURES_BEFORE_OFFLINE` | `3` | Consecutive failures before marking battery offline |

> **Scaling tip:** With N batteries, the total time occupied by offsets is `(N-1) × BATTERY_OFFSET_SECONDS`. Make sure this is less than `POLL_CYCLE_SECONDS`. For example, with 8 batteries and a 15s offset, you need at least `7 × 15 = 105s` cycle. A service startup warning will be logged if the configuration is invalid.

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
- Try increasing `BLE_TIMEOUT_SECONDS` in `config.py` for slower adapters

**Sensors not appearing in Home Assistant:**
- Check that MQTT integration is enabled and connected in HA
- Verify MQTT broker credentials in `settings.json`
- Check logs: `journalctl -u solar-battery-monitor -f`

**Multiple batteries going offline:**
- Increase `BATTERY_OFFSET_SECONDS` to give each battery more time
- Check for BLE adapter saturation: `dmesg | grep -i bluetooth`

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
