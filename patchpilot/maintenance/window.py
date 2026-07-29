from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchpilot.inventory.models import MaintenanceConfig, MaintenanceWindowDef


class MaintenanceWindow:
    def __init__(
        self,
        timezone_str: str = "UTC",
        windows: list[tuple[dt_time, dt_time, set[int]]] | None = None,
    ) -> None:
        self.timezone_str = timezone_str
        self.windows = windows or []
        self._tz = self._load_timezone()

    @classmethod
    def from_config(cls, config: MaintenanceConfig) -> "MaintenanceWindow":
        windows: list[tuple[dt_time, dt_time, set[int]]] = []
        day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }

        for w in config.windows:
            start_parts = w.start.split(":")
            end_parts = w.end.split(":")
            start = dt_time(int(start_parts[0]), int(start_parts[1]))
            end = dt_time(int(end_parts[0]), int(end_parts[1]))
            days = {day_map[d.lower()] for d in w.days}
            windows.append((start, end, days))

        return cls(
            timezone_str=config.timezone,
            windows=windows,
        )

    def _load_timezone(self) -> object:
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo(self.timezone_str)
        except Exception:
            import warnings
            warnings.warn(f"Could not load timezone '{self.timezone_str}', falling back to UTC")
            import datetime as dt_mod
            return dt_mod.timezone.utc

    def is_open(self, dt: datetime | None = None) -> bool:
        if dt is None:
            dt = datetime.utcnow()

        if not self.windows:
            return True

        # Convert to configured timezone
        try:
            local_dt = dt.astimezone(self._tz) if hasattr(dt, 'astimezone') else dt
        except (ValueError, OSError):
            local_dt = dt

        try:
            weekday = local_dt.weekday() if hasattr(local_dt, 'weekday') else 0
            local_time = local_dt.time() if hasattr(local_dt, 'time') else dt.time()
        except (AttributeError, ValueError):
            weekday = 0
            local_time = dt.time()

        for start, end, days in self.windows:
            if weekday not in days:
                continue

            if start <= end:
                if start <= local_time <= end:
                    return True
            else:
                # Window crosses midnight
                if local_time >= start or local_time <= end:
                    return True

        return False

    def next_open(self, from_dt: datetime | None = None) -> datetime | None:
        if from_dt is None:
            from_dt = datetime.utcnow()

        if not self.windows:
            return from_dt

        for i in range(7 * 24 * 2):  # Look ahead up to 2 weeks
            check = from_dt + timedelta(minutes=30 * i)
            if self.is_open(check):
                return check

        return None
