"""Per-battery cache with timeout and offline marking logic."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config import MAX_FAILURES_BEFORE_OFFLINE

logger = logging.getLogger(__name__)


@dataclass
class BatteryCache:
    """State and cached data for a single battery."""

    battery_id: int
    name: str

    # BMS data - None means unknown / offline
    data: dict[str, Any] = field(default_factory=dict)

    # Connection state
    consecutive_failures: int = 0
    last_seen: datetime | None = None
    online: bool = False

    @property
    def is_offline(self) -> bool:
        """True if the battery is considered offline (data cleared)."""
        return self.consecutive_failures >= MAX_FAILURES_BEFORE_OFFLINE

    @property
    def status(self) -> str:
        """Textual state of the battery."""
        if self.is_offline:
            # Show 'offline' whether the battery was manually disabled (force_offline)
            # or was never seen — is_offline is True in both cases.
            return "offline"
        if self.last_seen is None:
            return "never_seen"  # online but no successful read yet
        if self.consecutive_failures > 0:
            return "degraded"  # has old data, but limit not exceeded
        return "online"

    def record_success(self, data: dict[str, Any]) -> None:
        """Records a successful read and updates the cache."""
        self.data = data
        self.consecutive_failures = 0
        self.last_seen = datetime.now(timezone.utc)
        self.online = True
        logger.info(
            "Battery %s (%s): successful read - SOC=%s%%, V=%.2fV",
            self.battery_id,
            self.name,
            data.get("state_of_charge", "?"),
            data.get("total_voltage", 0),
        )

    def record_failure(self) -> None:
        """Records a connection/read failure."""
        self.consecutive_failures += 1
        logger.warning(
            "Battery %s (%s): failure %d/%d",
            self.battery_id,
            self.name,
            self.consecutive_failures,
            MAX_FAILURES_BEFORE_OFFLINE,
        )
        if self.is_offline:
            if self.data:
                logger.error(
                    "Battery %s (%s): OFFLINE - cache cleared (stale data from %s)",
                    self.battery_id,
                    self.name,
                    self.last_seen.strftime("%H:%M:%S") if self.last_seen else "never",
                )
            self.data = {}
            self.online = False

    def force_offline(self) -> None:
        """Immediately mark battery as offline due to user-disabled connection.
        Unlike record_failure(), this skips the gradual failure counter and
        clears the cache instantly so system counters reflect the correct state.
        """
        self.data = {}
        self.online = False
        self.consecutive_failures = MAX_FAILURES_BEFORE_OFFLINE
        logger.info(
            "Battery %s (%s): forced offline (connection disabled by user)",
            self.battery_id,
            self.name,
        )

    def get_publish_data(self) -> dict[str, Any] | None:
        """
        Returns data to be published to MQTT.
        - None if battery is offline (publish nothing / unavailable)
        - dict with data if online or degraded (cached data)
        """
        if self.is_offline:
            return None
        return self.data if self.data else None


class CacheManager:
    """Manages cache for all batteries."""

    def __init__(self, batteries: list[dict]) -> None:
        self.batteries: dict[int, BatteryCache] = {
            b["id"]: BatteryCache(battery_id=b["id"], name=b["name"])
            for b in batteries
        }

    def get(self, battery_id: int) -> BatteryCache:
        return self.batteries[battery_id]

    def offline_count(self) -> int:
        """Number of offline batteries."""
        return sum(1 for b in self.batteries.values() if b.is_offline or b.last_seen is None)

    def all_online(self) -> bool:
        """True if all batteries are online."""
        return all(
            not b.is_offline and b.last_seen is not None
            for b in self.batteries.values()
        )

    def summary(self) -> str:
        """Text summary of all batteries states."""
        parts = []
        for b in self.batteries.values():
            parts.append(f"Bat{b.battery_id}={b.status}")
        return " | ".join(parts)
