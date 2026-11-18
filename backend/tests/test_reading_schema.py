"""Response schema of stored readings: optional sensors must not break the endpoints."""
import pytest

pytest.importorskip("fastapi")

from app.routes.sensors import RoomLatestOut, SensorReadingOut  # noqa: E402


def test_reading_without_co_sensor_validates():
    reading = SensorReadingOut.model_validate({"sensor_id": "s-1", "timestamp": "2026-11-17T10:00:00", "co_ppm": None})
    assert reading.co_ppm is None


def test_latest_row_with_null_co_validates():
    row = RoomLatestOut.model_validate({"room_id": "A", "sensor_id": "s-1",
                                        "reading": {"sensor_id": "s-1", "timestamp": "2026-11-17T10:00:00", "co_ppm": None}})
    assert row.reading is not None and row.reading.co_ppm is None
