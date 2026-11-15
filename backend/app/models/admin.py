from sqlalchemy import Column, Integer, String, Float, Time, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from app.database_sql import Base

# Valid value sets (enforced in routes, not at DB level for portability)
ROLES = {"admin", "collaborator", "guest"}
STATUSES = {"pending", "active", "inactive"}
# Per-room cognitive policy:
#   auto      → ML model if loaded, else heuristic (default behavior)
#   heuristic → force the heuristic formula even when an ML model is loaded
#   manual    → no prediction; hold the room at its configured target_temp
CONTROL_POLICIES = {"auto", "heuristic", "manual"}
FAN_TYPES = {"pwm_12v", "smart_plug"}


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False, default="guest")      # admin | collaborator | guest
    status = Column(String, nullable=False, default="active")   # pending | active | inactive

    reservations = relationship("Reservation", back_populates="user", cascade="all, delete-orphan")


class Room(Base):
    __tablename__ = "rooms"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    max_capacity = Column(Integer, nullable=False)
    target_temp = Column(Float, nullable=False)
    area_m2 = Column(Float, nullable=True)
    ceiling_height_m = Column(Float, nullable=True)
    fan_type = Column(String, nullable=False, default="pwm_12v", server_default="pwm_12v")
    fan_power_w = Column(Float, nullable=False, default=2.4, server_default="2.4")
    control_policy = Column(String, nullable=False, default="auto", server_default="auto")
    # Location, used to group and filter rooms in the UI (building → floor → room).
    building = Column(String, nullable=True, index=True)
    floor = Column(Integer, nullable=True)

    schedules = relationship("Schedule", back_populates="room", cascade="all, delete-orphan")
    # SensorDevice and Reservation rows survive room deletion as historical audit orphans.
    # Relationships are read-only (viewonly) — no cascade, no back_populates on Room side.


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(String, ForeignKey("rooms.id"), nullable=False)
    day_of_week = Column(Integer, nullable=False)  # 0=Monday … 6=Sunday
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    expected_people = Column(Integer, nullable=False)

    room = relationship("Room", back_populates="schedules")


class SensorDevice(Base):
    __tablename__ = "sensor_devices"

    id = Column(String, primary_key=True)           # "sensor-001" — matches incoming sensor_id
    # No FK constraint — room_id preserved as an audit string even after room deletion.
    room_id = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    control_enabled = Column(Boolean, nullable=False, default=True)

    room = relationship(
        "Room",
        primaryjoin="foreign(SensorDevice.room_id) == Room.id",
        viewonly=True,
    )


class Reservation(Base):
    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True, index=True)
    # No FK constraint — room_id preserved as an audit string even after room deletion.
    room_id = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    expected_occupancy = Column(Integer, nullable=False)

    room = relationship(
        "Room",
        primaryjoin="foreign(Reservation.room_id) == Room.id",
        viewonly=True,
    )
    user = relationship("User", back_populates="reservations")
