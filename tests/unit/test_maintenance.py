"""Tests for maintenance window logic."""
from datetime import time as dt_time, datetime, timedelta, timezone

from patchpilot.maintenance.window import MaintenanceWindow


class TestMaintenanceWindow:
    def _window(
        self,
        start: str = "23:00",
        end: str = "02:00",
        days: list[str] | None = None,
    ) -> MaintenanceWindow:
        if days is None:
            days = ["saturday", "sunday"]
        windows = [
            {
                "start": start,
                "end": end,
                "days": days,
                "timezone": "UTC",
            }
        ]
        from patchpilot.inventory.models import MaintenanceConfig, MaintenanceWindowDef
        config = MaintenanceConfig(
            timezone="UTC",
            windows=[
                MaintenanceWindowDef(start=start, end=end, days=days)  # type: ignore[arg-type]
            ],
        )
        return MaintenanceWindow.from_config(config)

    def test_open_during_window(self) -> None:
        mw = self._window()
        # Saturday 23:30 UTC
        dt = datetime(2026, 7, 25, 23, 30, tzinfo=timezone.utc)
        assert mw.is_open(dt)

    def test_closed_outside_window(self) -> None:
        mw = self._window()
        # Monday 12:00 UTC
        dt = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        assert not mw.is_open(dt)

    def test_window_crosses_midnight(self) -> None:
        mw = self._window()
        # Sunday 01:00 UTC (inside the saturday 23:00 - sunday 02:00 window)
        dt = datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)
        assert mw.is_open(dt)

    def test_no_windows_always_open(self) -> None:
        mw = MaintenanceWindow()
        assert mw.is_open()

    def test_timezone_conversion(self) -> None:
        from patchpilot.inventory.models import MaintenanceConfig, MaintenanceWindowDef
        config = MaintenanceConfig(
            timezone="Europe/Warsaw",
            windows=[
                MaintenanceWindowDef(start="23:00", end="02:00", days=["saturday"])  # type: ignore[arg-type]
            ],
        )
        mw = MaintenanceWindow.from_config(config)
        # Sunday 00:30 CEST = Saturday 22:30 UTC
        # In Warsaw (CEST, UTC+2): Sunday 00:30 → Saturday 22:30 UTC
        # Window: Saturday 23:00-02:00 Warsaw time → should be CLOSED at Sunday 00:30 Warsaw
        # Actually 00:30 Sunday Warsaw is still inside the window (23:00 Sat - 02:00 Sun)
        dt = datetime(2026, 7, 26, 0, 30, tzinfo=timezone.utc)
        # Warsaw is UTC+2 in summer, so 00:30 UTC = 02:30 CEST → boundary
        # Let's pick a clearer case:
        # Saturday 21:00 UTC = 23:00 CEST → just opened
        dt_open = datetime(2026, 7, 25, 21, 0, tzinfo=timezone.utc)
        assert mw.is_open(dt_open)

        # Saturday 20:00 UTC = 22:00 CEST → still closed
        dt_closed = datetime(2026, 7, 25, 20, 0, tzinfo=timezone.utc)
        assert not mw.is_open(dt_closed)
