"""Expected occupancy source: a reservation takes precedence over the weekly schedule."""
from datetime import datetime, time

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database_sql import Base  # noqa: E402
import app.models.admin  # noqa: E402,F401  (registers the ORM models)
from app.models.admin import Room, Schedule, SensorDevice, Reservation, User  # noqa: E402
from app.services.data_service import get_room_context  # noqa: E402

# Readings are stamped in naive UTC; Lima is UTC-5 (LOCAL_UTC_OFFSET_HOURS default).
# Monday 2026-10-26 10:00 local == 15:00 UTC.
READING_UTC = datetime(2026, 10, 26, 15, 0)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        User(id=1, username="prof", hashed_password="x", role="collaborator", status="active"),
        Room(id="A403", name="A403 Aula", max_capacity=46, target_temp=24.0),
        SensorDevice(id="s-A403", room_id="A403", is_active=True, control_enabled=True),
        # Monday 10:00–12:00, 30 people every week
        Schedule(room_id="A403", day_of_week=0, start_time=time(10, 0), end_time=time(12, 0), expected_people=30),
    ])
    session.commit()
    yield session
    session.close()


def test_schedule_is_used_when_there_is_no_reservation(db):
    ctx = get_room_context(db, "s-A403", READING_UTC)
    assert ctx["expected_people"] == 30
    assert ctx["expected_source"] == "schedule"


def test_registered_room_dimensions_reach_predictor_context(db):
    room = db.query(Room).filter(Room.id == "A403").one()
    room.area_m2 = 60
    room.ceiling_height_m = 3.2
    db.commit()
    ctx = get_room_context(db, "s-A403", READING_UTC)
    assert ctx["volume_m3"] == 192


def test_room_dimensions_must_be_complete_and_positive():
    from pydantic import ValidationError
    from app.routes.admin import RoomCreateIn

    base = {"id": "B201", "name": "B201", "max_capacity": 30, "target_temp": 24}
    valid = RoomCreateIn(**base, area_m2=60, ceiling_height_m=3)
    assert valid.area_m2 * valid.ceiling_height_m == 180
    with pytest.raises(ValidationError):
        RoomCreateIn(**base, area_m2=60)
    with pytest.raises(ValidationError):
        RoomCreateIn(**base, area_m2=-1, ceiling_height_m=3)


def test_room_fan_defaults_and_rejects_invalid_configuration():
    from pydantic import ValidationError
    from app.routes.admin import RoomCreateIn

    base = {"id": "B201", "name": "B201", "max_capacity": 30, "target_temp": 24}
    room = RoomCreateIn(**base)
    assert room.fan_type == "pwm_12v" and room.fan_power_w == 2.4
    plug = RoomCreateIn(**base, fan_type="smart_plug", fan_power_w=40)
    assert plug.fan_power_w == 40
    with pytest.raises(ValidationError):
        RoomCreateIn(**base, fan_type="smart_plug", fan_power_w=0)
    with pytest.raises(ValidationError):
        RoomCreateIn(**base, fan_type="unknown")


def test_room_fan_preset_switch_and_custom_power(db):
    from app.routes.admin import RoomUpdateIn, _updated_fan_power, _room_out

    room = db.query(Room).filter(Room.id == "A403").one()
    assert _room_out(room)["fan_power_w"] == 2.4
    base = {"name": room.name, "max_capacity": room.max_capacity, "target_temp": room.target_temp}
    smart = RoomUpdateIn(**base, fan_type="smart_plug")
    assert _updated_fan_power(smart, room) == 40.0
    custom = RoomUpdateIn(**base, fan_type="smart_plug", fan_power_w=32)
    assert _updated_fan_power(custom, room) == 32


def test_reservation_overrides_schedule(db):
    db.add(Reservation(room_id="A403", user_id=1, expected_occupancy=12,
                       start_time=datetime(2026, 10, 26, 10, 0), end_time=datetime(2026, 10, 26, 11, 0)))
    db.commit()
    ctx = get_room_context(db, "s-A403", READING_UTC)
    assert ctx["expected_people"] == 12
    assert ctx["expected_source"] == "reservation"


def test_upcoming_reservation_within_lookahead_enables_precooling(db):
    # Starts 20 min after the reading (inside the 30 min pre-cooling window)
    db.add(Reservation(room_id="A403", user_id=1, expected_occupancy=40,
                       start_time=datetime(2026, 10, 26, 10, 20), end_time=datetime(2026, 10, 26, 11, 0)))
    db.commit()
    ctx = get_room_context(db, "s-A403", READING_UTC)
    assert ctx["expected_people"] == 40


def test_no_plan_means_no_expected_people(db):
    ctx = get_room_context(db, "s-A403", datetime(2026, 10, 27, 20, 0))  # Tuesday 15:00 local
    assert ctx["expected_people"] is None
    assert ctx["expected_source"] is None


def test_occupancy_forecast_follows_schedule_and_reservations(db):
    from app.services.data_service import occupancy_forecast

    # 09:00 local: the 10:00–12:00 schedule starts in 60 min.
    fc = occupancy_forecast(db, "A403", datetime(2026, 10, 26, 14, 0), 195)
    assert fc[59] == (False, 0.0)
    assert fc[60] == (True, 30.0)
    assert fc[179] == (True, 30.0)     # 11:59
    assert fc[180] == (False, 0.0)     # 12:00, the class is over
    db.add(Reservation(room_id="A403", user_id=1, expected_occupancy=12,
                       start_time=datetime(2026, 10, 26, 10, 30), end_time=datetime(2026, 10, 26, 11, 0)))
    db.commit()
    fc = occupancy_forecast(db, "A403", datetime(2026, 10, 26, 14, 0), 195)
    assert fc[90] == (True, 12.0)      # 10:30, the reservation wins
    assert fc[125] == (True, 30.0)     # 11:05, back to the schedule
