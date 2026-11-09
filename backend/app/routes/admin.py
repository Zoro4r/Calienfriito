import logging
import math
import re
from pathlib import Path
from datetime import datetime, time as dt_time, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.config import LOCAL_UTC_OFFSET_HOURS, NODE_MQTT_HOST, NODE_MQTT_PORT, NODE_MQTT_USER
from app.core.timeutils import ensure_local, to_local
from app.database import get_db
from app.database_sql import get_db_sql
from app.models.admin import Room, Schedule, SensorDevice, Reservation, User, STATUSES, CONTROL_POLICIES
# LÍNEA NUEVA — antes este import no existía. Trae el modelo de error compartido.
from app.models.common import ErrorDetail
from app.dependencies import get_current_user
from app.services.predictive_service import calculate_cooling_demand

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class ScheduleIn(BaseModel):
    day_of_week: int          # 0=Monday … 6=Sunday
    start_time: str           # "HH:MM" or "HH:MM:SS"
    end_time: str             # "HH:MM" or "HH:MM:SS"
    expected_people: int


class RoomIn(BaseModel):
    id: str
    name: str
    max_capacity: int
    target_temp: float
    building: Optional[str] = None
    floor: Optional[int] = None
    schedules: List[ScheduleIn] = []


class RoomCreateIn(BaseModel):
    id: str
    name: str
    max_capacity: int
    target_temp: float
    building: Optional[str] = None
    floor: Optional[int] = None


class SensorDeviceIn(BaseModel):
    sensor_id: str
    room_id: str
    is_active: bool = True
    control_enabled: bool = True


class SensorCreateIn(BaseModel):
    id: str
    room_id: str


class RoomUpdateIn(BaseModel):
    new_id: Optional[str] = None   # if set and differs from path param, renames the PK
    name: str
    max_capacity: int
    target_temp: float
    building: Optional[str] = None
    floor: Optional[int] = None


class RoomPolicyIn(BaseModel):
    control_policy: str   # auto | heuristic | manual


class SensorAssignIn(BaseModel):
    new_id: Optional[str] = None        # if set and differs from path param, renames the PK
    room_id: Optional[str] = None       # None / "" → detach sensor (room_id = NULL in DB)


class StatusPatch(BaseModel):
    status: str


class ReservationIn(BaseModel):
    room_id: str
    start_time: datetime
    end_time: datetime
    expected_occupancy: int


# ARCHIVO NUEVO — no existía antes. Solo hora ("HH:MM" o "HH:MM:SS"), sin
# fecha: pensado para que Watson pida nada más "¿a qué hora?" en vez de armar
# el datetime completo (fecha+hora en formato ISO), que es donde fallaba el
# formato que mandaba la action de editar reserva.
class ReservationTimeUpdateIn(BaseModel):
    start_time: str
    end_time: str


# ============================================================================
# BLOQUE NUEVO — todas estas clases (RoomOut, RoomPolicyOut, SensorDeviceOut,
# UserOut, ReservationOut) no existían antes. Antes cada endpoint devolvía un
# dict/list "a mano" sin declarar su forma, por eso Watson no podía mapear
# los campos de la respuesta en el panel "Response".
# ============================================================================
# Declared so Watson's extension "Response" panel can expose these fields as
# insertable variables — FastAPI otherwise emits an empty schema for plain
# dict/list returns, leaving Watson with nothing to map.

class RoomOut(BaseModel):
    id: str
    name: str
    max_capacity: int
    target_temp: float
    control_policy: Optional[str] = None
    building: Optional[str] = None
    floor: Optional[int] = None


def _room_out(r: Room) -> dict:
    return {
        "id": r.id, "name": r.name, "max_capacity": r.max_capacity, "target_temp": r.target_temp,
        "control_policy": r.control_policy, "building": r.building, "floor": r.floor,
    }


def _clean_building(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value.upper() if value else None


class RoomPolicyOut(BaseModel):
    id: str
    control_policy: str


class SensorDeviceOut(BaseModel):
    sensor_id: str
    room_id: Optional[str] = None
    is_active: bool
    control_enabled: bool


class UserOut(BaseModel):
    user_id: int
    username: str
    role: str
    status: str


class ReservationOut(BaseModel):
    id: int
    room_id: str
    room_name: Optional[str] = None
    user_id: int
    username: Optional[str] = None
    start_time: str
    end_time: str
    expected_occupancy: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_time(t: str) -> dt_time:
    parts = t.split(":")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    return dt_time(h, m, s)


def _require_admin(current_user: User):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")


def _require_admin_or_collaborator(current_user: User):
    if current_user.role not in ("admin", "collaborator"):
        raise HTTPException(status_code=403, detail="Admin or collaborator role required.")


# ── Room endpoints ────────────────────────────────────────────────────────────

# ANTES: @router.get("/rooms")
# AHORA: response_model=List[RoomOut] + responses={401} — Action 2 (Listar aulas).
@router.get("/rooms", response_model=List[RoomOut], responses={401: {"model": ErrorDetail}})
def list_rooms(
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    rooms = db.query(Room).order_by(Room.building, Room.floor, Room.id).all()
    return [_room_out(r) for r in rooms]


@router.get("/rooms/{room_id}/impact")
def get_room_impact(
    room_id: str,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Pre-deletion impact counts for a room that still exists."""
    _require_admin(current_user)
    if not db.query(Room).filter(Room.id == room_id).first():
        raise HTTPException(status_code=404, detail=f"Room id='{room_id}' not found.")
    reservations_count = db.query(Reservation).filter(Reservation.room_id == room_id).count()
    sensors_count = db.query(SensorDevice).filter(SensorDevice.room_id == room_id).count()
    return {"reservations_count": reservations_count, "sensors_count": sensors_count}


@router.get("/rooms/check-orphans/{room_id}")
def check_orphans(
    room_id: str,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Audit endpoint: count Reservation and SensorDevice rows whose room_id string
    matches room_id but whose parent Room row no longer exists (historical orphans).
    """
    _require_admin(current_user)
    room_exists = db.query(Room).filter(Room.id == room_id).first() is not None
    reservations_count = db.query(Reservation).filter(Reservation.room_id == room_id).count()
    sensors_count = db.query(SensorDevice).filter(SensorDevice.room_id == room_id).count()
    has_orphans = (not room_exists) and (reservations_count > 0 or sensors_count > 0)
    return {
        "has_orphans": has_orphans,
        "reservations_count": reservations_count,
        "sensors_count": sensors_count,
    }


@router.get("/rooms/{room_id}")
def get_room(
    room_id: str,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail=f"Room id='{room_id}' not found.")
    return _room_out(room)


# ANTES: @router.put("/rooms/{room_id}/policy")
# AHORA: response_model=RoomPolicyOut + responses={401,403,404} — Action 13
# (Política cognitiva del aula).
@router.put(
    "/rooms/{room_id}/policy",
    response_model=RoomPolicyOut,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
def update_room_policy(
    room_id: str,
    payload: RoomPolicyIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Set the per-room cognitive policy (auto | heuristic | manual).

    Admin-only. Dedicated endpoint so changing policy never touches the heavier
    rename/edit path in update_room.
    """
    _require_admin(current_user)
    if payload.control_policy not in CONTROL_POLICIES:
        raise HTTPException(status_code=422, detail=f"Invalid control_policy. Use one of: {sorted(CONTROL_POLICIES)}.")
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail=f"Room id='{room_id}' not found.")
    try:
        room.control_policy = payload.control_policy
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error setting room policy: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    logger.info("Admin '%s' set room '%s' control_policy='%s'.", current_user.username, room_id, payload.control_policy)
    return {"id": room.id, "control_policy": room.control_policy}


@router.post("/model/reload")
def reload_model(current_user: User = Depends(get_current_user)):
    """Reload the ML model from disk without restarting the backend.

    Run the training pipeline (ml/pipeline/extract_data.py → train_model.py) to
    regenerate model.joblib, then call this to pick it up live. Admin-only.
    """
    _require_admin(current_user)
    from app.services.predictive_service import load_model, active_engine
    load_model()
    engine = active_engine()
    logger.info("Admin '%s' reloaded ML model — engine now '%s'.", current_user.username, engine)
    return {"engine": engine, "model_loaded": engine == "ml"}


# ANTES: @router.post("/rooms", status_code=201)
# AHORA: response_model=RoomOut + responses={401,403,409} — Action 8 (Crear aula).
@router.post(
    "/rooms",
    status_code=201,
    response_model=RoomOut,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
)
def create_room(
    payload: RoomCreateIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    if db.query(Room).filter(Room.id == payload.id).first():
        raise HTTPException(status_code=409, detail=f"Room id='{payload.id}' already exists.")
    if db.query(Room).filter(Room.name == payload.name).first():
        raise HTTPException(status_code=409, detail=f"Room name='{payload.name}' already exists.")
    room = Room(
        id=payload.id,
        name=payload.name,
        max_capacity=payload.max_capacity,
        target_temp=payload.target_temp,
        building=_clean_building(payload.building),
        floor=payload.floor,
    )
    db.add(room)
    try:
        db.commit()
        db.refresh(room)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating room: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    logger.info("Admin '%s' created room '%s' (%s).", current_user.username, room.id, room.name)
    return _room_out(room)


# ANTES: @router.put("/rooms/{room_id}")
# AHORA: response_model=RoomOut + responses={401,403,404,409} — Action 9 (Editar aula).
@router.put(
    "/rooms/{room_id}",
    response_model=RoomOut,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
)
def update_room(
    room_id: str,
    payload: RoomUpdateIn,
    cascade_sensors: bool = True,
    cascade_reservations: bool = True,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Update room fields. If payload.new_id differs from room_id, performs a safe PK
    rename: inserts new Room, bulk-UPDATEs the chosen child tables to new_id, then
    deletes the old Room row — all inside a single transaction.

    cascade_sensors=False: SensorDevice rows keep the old room_id (become orphans).
    cascade_reservations=False: Reservation rows keep the old room_id (become orphans).
    Schedules are always re-parented — they are tightly coupled config, not audit data.
    """
    _require_admin(current_user)
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail=f"Room id='{room_id}' not found.")

    if payload.name != room.name:
        if db.query(Room).filter(Room.name == payload.name).first():
            raise HTTPException(status_code=409, detail=f"Room name='{payload.name}' already exists.")

    new_id = (payload.new_id or "").strip() or None

    try:
        if new_id and new_id != room_id:
            if db.query(Room).filter(Room.id == new_id).first():
                raise HTTPException(status_code=409, detail=f"Room id='{new_id}' already exists.")
            # When name is unchanged the old row still holds the unique name slot.
            # Temporarily rename it so the new row can be inserted without a constraint
            # violation. The old row is deleted in step 3 within the same transaction.
            if payload.name == room.name:
                room.name = f"__renaming__{room_id}"
                db.flush()
            # Step 1: insert new room so children can reference it immediately
            new_room = Room(
                id=new_id, name=payload.name, max_capacity=payload.max_capacity, target_temp=payload.target_temp,
                control_policy=room.control_policy, building=_clean_building(payload.building), floor=payload.floor,
            )
            db.add(new_room)
            db.flush()
            # Step 2: re-parent children — schedules always, others per cascade flags
            db.query(Schedule).filter(Schedule.room_id == room_id).update({"room_id": new_id}, synchronize_session="fetch")
            if cascade_sensors:
                db.query(SensorDevice).filter(SensorDevice.room_id == room_id).update({"room_id": new_id}, synchronize_session="fetch")
            if cascade_reservations:
                db.query(Reservation).filter(Reservation.room_id == room_id).update({"room_id": new_id}, synchronize_session="fetch")
            # Step 3: remove old room (schedules already re-parented; sensors/reservations
            # may intentionally remain as orphans pointing to the old room_id string)
            db.delete(room)
            db.commit()
            logger.info(
                "Admin '%s' renamed room '%s' → '%s' (cascade_sensors=%s, cascade_reservations=%s).",
                current_user.username, room_id, new_id, cascade_sensors, cascade_reservations,
            )
            return _room_out(new_room)

        # Normal update — no PK change
        room.name = payload.name
        room.max_capacity = payload.max_capacity
        room.target_temp = payload.target_temp
        room.building = _clean_building(payload.building)
        room.floor = payload.floor
        db.commit()
        db.refresh(room)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error updating room: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    logger.info("Admin '%s' updated room '%s'.", current_user.username, room.id)
    return _room_out(room)


# ANTES: @router.delete("/rooms/{room_id}", status_code=200)
# AHORA: se agrega responses={401,403,404} (sin response_model porque el
# mensaje de éxito de la Action 10 usa el slot {aula}, no el body).
@router.delete(
    "/rooms/{room_id}",
    status_code=200,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
def delete_room(
    room_id: str,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Delete a room. Schedules are cascade-deleted (tightly coupled config data).

    Reservation and SensorDevice rows are intentionally left intact: their room_id
    string is preserved as a historical audit reference (orphan records). No FK
    constraint exists on those columns, so the DELETE succeeds without nullifying
    or removing any child records.
    """
    _require_admin(current_user)
    room = db.query(Room).filter(Room.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail=f"Room id='{room_id}' not found.")

    try:
        db.query(Schedule).filter(Schedule.room_id == room_id).delete(synchronize_session="fetch")
        db.delete(room)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error deleting room '%s': %s", room_id, exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    logger.info("Admin '%s' deleted room '%s'.", current_user.username, room_id)
    return {"deleted": room_id}


@router.put("/sensors/{sensor_id}")
def reassign_sensor(
    sensor_id: str,
    payload: SensorAssignIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Reassign sensor to a room. If payload.new_id differs from sensor_id, performs a
    safe PK rename via delete-insert (no other tables have FK references to sensor_devices.id).
    """
    _require_admin(current_user)
    device = db.query(SensorDevice).filter(SensorDevice.id == sensor_id).first()
    if not device:
        raise HTTPException(status_code=404, detail=f"Sensor id='{sensor_id}' not found.")

    # Normalise: empty string → None (detach sensor from any room)
    room_id_val = (payload.room_id or "").strip() or None

    if room_id_val and not db.query(Room).filter(Room.id == room_id_val).first():
        raise HTTPException(status_code=404, detail=f"Room id='{room_id_val}' not found.")

    new_id = (payload.new_id or "").strip() or None

    try:
        if new_id and new_id != sensor_id:
            if db.query(SensorDevice).filter(SensorDevice.id == new_id).first():
                raise HTTPException(status_code=409, detail=f"Sensor id='{new_id}' already exists.")
            new_device = SensorDevice(
                id=new_id,
                room_id=room_id_val,
                is_active=device.is_active,
                control_enabled=device.control_enabled,
            )
            db.add(new_device)
            db.delete(device)
            db.commit()
            db.refresh(new_device)
            logger.info("Admin '%s' renamed sensor '%s' → '%s' (room '%s').", current_user.username, sensor_id, new_id, room_id_val)
            return {"sensor_id": new_device.id, "room_id": new_device.room_id, "is_active": new_device.is_active, "control_enabled": new_device.control_enabled}

        device.room_id = room_id_val
        db.commit()
        db.refresh(device)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error reassigning sensor: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    logger.info("Admin '%s' reassigned sensor '%s' → room '%s'.", current_user.username, device.id, room_id_val)
    return {"sensor_id": device.id, "room_id": device.room_id, "is_active": device.is_active, "control_enabled": device.control_enabled}


@router.delete("/sensors/{sensor_id}", status_code=200)
def delete_sensor(
    sensor_id: str,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Delete a physical sensor from PostgreSQL.

    Historical readings in MongoDB are intentionally preserved as an immutable
    audit trail of the IoT hardware that was installed.
    """
    _require_admin(current_user)
    device = db.query(SensorDevice).filter(SensorDevice.id == sensor_id).first()
    if not device:
        raise HTTPException(status_code=404, detail=f"Sensor id='{sensor_id}' not found.")
    try:
        db.delete(device)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error deleting sensor '%s': %s", sensor_id, exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    logger.info("Admin '%s' deleted sensor '%s'.", current_user.username, sensor_id)
    return {"deleted": sensor_id}


@router.post("/setup-rooms", status_code=201)
def setup_room(
    payload: RoomIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    try:
        room = db.query(Room).filter(Room.name == payload.name).first()
        if room:
            room.max_capacity = payload.max_capacity
            room.target_temp = payload.target_temp
            room.building = _clean_building(payload.building) or room.building
            room.floor = payload.floor if payload.floor is not None else room.floor
        else:
            room = Room(
                id=payload.id,
                name=payload.name,
                max_capacity=payload.max_capacity,
                target_temp=payload.target_temp,
                building=_clean_building(payload.building),
                floor=payload.floor,
            )
            db.add(room)
            db.flush()

        db.query(Schedule).filter(Schedule.room_id == room.id).delete()
        for s in payload.schedules:
            db.add(Schedule(
                room_id=room.id,
                day_of_week=s.day_of_week,
                start_time=_parse_time(s.start_time),
                end_time=_parse_time(s.end_time),
                expected_people=s.expected_people,
            ))

        db.commit()
        db.refresh(room)
        logger.info("Room '%s' upserted with %d schedules.", room.name, len(payload.schedules))
        return {
            "room_id": room.id,
            "name": room.name,
            "schedules_created": len(payload.schedules),
        }
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error on setup-rooms: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


# ── SensorDevice endpoints ────────────────────────────────────────────────────

@router.get("/devices")
def list_devices(
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    devices = db.query(SensorDevice).order_by(SensorDevice.id).all()
    return [
        {
            "sensor_id": d.id,
            "room_id": d.room_id,
            "room_name": d.room.name if d.room else None,
            "is_active": d.is_active,
            "control_enabled": d.control_enabled,
        }
        for d in devices
    ]


@router.post("/devices", status_code=201)
def register_device(
    payload: SensorDeviceIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    if not db.query(Room).filter(Room.id == payload.room_id).first():
        raise HTTPException(status_code=404, detail=f"Room id={payload.room_id} not found.")
    device = db.query(SensorDevice).filter(SensorDevice.id == payload.sensor_id).first()
    if device:
        device.room_id = payload.room_id
        device.is_active = payload.is_active
        device.control_enabled = payload.control_enabled
    else:
        device = SensorDevice(
            id=payload.sensor_id,
            room_id=payload.room_id,
            is_active=payload.is_active,
            control_enabled=payload.control_enabled,
        )
        db.add(device)
    try:
        db.commit()
        db.refresh(device)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"sensor_id": device.id, "room_id": device.room_id, "is_active": device.is_active, "control_enabled": device.control_enabled}


# ANTES: @router.post("/sensors", status_code=201)
# AHORA: response_model=SensorDeviceOut + responses={401,403,404,409} —
# Action 11 (Aprovisionar sensor).
@router.post(
    "/sensors",
    status_code=201,
    response_model=SensorDeviceOut,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
)
def provision_sensor(
    payload: SensorCreateIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Register a new physical sensor and assign it to a room.

    Security rule: regardless of the request payload, is_active and
    control_enabled are always forced to False. The admin must explicitly
    enable the sensor from the room dashboard after physical installation,
    preventing unintended AC control from sensors that are not yet calibrated.
    """
    _require_admin(current_user)
    if not db.query(Room).filter(Room.id == payload.room_id).first():
        raise HTTPException(status_code=404, detail=f"Room id='{payload.room_id}' not found.")
    if db.query(SensorDevice).filter(SensorDevice.id == payload.id).first():
        raise HTTPException(status_code=409, detail=f"Sensor id='{payload.id}' already exists.")
    device = SensorDevice(
        id=payload.id,
        room_id=payload.room_id,
        is_active=False,
        control_enabled=False,
    )
    db.add(device)
    try:
        db.commit()
        db.refresh(device)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    logger.info(
        "Admin '%s' provisioned sensor '%s' → room '%s' (inactive/disabled).",
        current_user.username, device.id, device.room_id,
    )
    return {"sensor_id": device.id, "room_id": device.room_id, "is_active": device.is_active, "control_enabled": device.control_enabled}


class NodeConfigOut(BaseModel):
    host: Optional[str] = None   # None: no public broker configured for nodes
    port: int
    user: str
    tls: bool


# The node firmware, as a template: the room and broker are filled in here, and the WiFi and
# broker password in the browser (they never reach the server). Room codes become MQTT topics.
NODE_FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "nodo.ino.tmpl"
ROOM_CODE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


@router.get(
    "/node-firmware",
    response_class=PlainTextResponse,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def node_firmware(room: str = Query(..., description="Room code, used as AULA on the node"), current_user: User = Depends(get_current_user)):
    """nodo.ino for one room, with its code and this server's broker. WiFi and broker password stay as placeholders."""
    _require_admin(current_user)
    if not ROOM_CODE.match(room):
        raise HTTPException(status_code=422, detail="Room code must be 1-20 letters, digits, - or _.")
    if not NODE_MQTT_HOST:
        raise HTTPException(status_code=409, detail="This server has no public broker for nodes (NODE_MQTT_HOST).")
    code = NODE_FIRMWARE.read_text(encoding="utf-8")
    for token, value in (("__AULA__", room), ("__MQTT_HOST__", NODE_MQTT_HOST),
                         ("__MQTT_PORT__", str(NODE_MQTT_PORT)), ("__MQTT_USER__", NODE_MQTT_USER)):
        code = code.replace(token, value)
    return code


@router.get("/node-config", response_model=NodeConfigOut, responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}})
def node_config(current_user: User = Depends(get_current_user)):
    """Broker settings a physical node needs to publish here ("Conectar nodo"). Never includes the password."""
    _require_admin(current_user)
    return {"host": NODE_MQTT_HOST or None, "port": NODE_MQTT_PORT, "user": NODE_MQTT_USER, "tls": NODE_MQTT_PORT == 8883}


# ── User management endpoints (admin-only) ────────────────────────────────────

# ANTES: @router.get("/users")
# AHORA: response_model=List[UserOut] + responses={401,403} — Action 14 (Listar usuarios).
@router.get("/users", response_model=List[UserOut], responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}})
def list_users(
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    users = db.query(User).order_by(User.id).all()
    return [
        {"user_id": u.id, "username": u.username, "role": u.role, "status": u.status}
        for u in users
    ]


# ANTES: @router.patch("/users/{user_id}/status")
# AHORA: response_model=UserOut + responses={400,401,403,404} — Action 14
# (Aprobar/activar usuario).
@router.patch(
    "/users/{user_id}/status",
    response_model=UserOut,
    responses={400: {"model": ErrorDetail}, 401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
def update_user_status(
    user_id: int,
    payload: StatusPatch,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Change a user's account status (pending → active → inactive).

    Self-modification is blocked (HTTP 400) to prevent an admin from accidentally
    locking themselves out. Status values are validated against the STATUSES set
    defined in models/admin.py.
    """
    _require_admin(current_user)
    if current_user.id == user_id:
        raise HTTPException(
            status_code=400,
            detail="An administrator cannot modify or self-lock their own account status.",
        )
    if payload.status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {sorted(STATUSES)}")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User id={user_id} not found.")
    user.status = payload.status
    db.commit()
    db.refresh(user)
    logger.info("Admin '%s' set user_id=%d status='%s'.", current_user.username, user_id, payload.status)
    return {"user_id": user.id, "username": user.username, "role": user.role, "status": user.status}


# ── Reservation endpoints ─────────────────────────────────────────────────────

# ANTES: @router.get("/reservations")
# AHORA: response_model=List[ReservationOut] + responses={401,403} — Action 4
# (Listar reservas).
@router.get("/reservations", response_model=List[ReservationOut], responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}})
def list_reservations(
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    # Reservations expose usernames/agendas — not for guests.
    _require_admin_or_collaborator(current_user)
    reservations = db.query(Reservation).order_by(Reservation.start_time.desc()).all()
    return [
        {
            "id": r.id,
            "room_id": r.room_id,
            "room_name": r.room.name if r.room else None,
            "user_id": r.user_id,
            "username": r.user.username if r.user else None,
            "start_time": r.start_time.isoformat(),
            "end_time": r.end_time.isoformat(),
            "expected_occupancy": r.expected_occupancy,
        }
        for r in reservations
    ]


class ReservationOccupancyOut(BaseModel):
    reservation_id: int
    samples: int                       # readings found in the reservation window
    actual_avg: Optional[float] = None # mean CO2-inferred headcount
    actual_peak: Optional[int] = None  # max CO2-inferred headcount


@router.get("/reservations/occupancy", response_model=List[ReservationOccupancyOut])
async def reservations_occupancy(
    ids: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Measured occupancy during each reservation (expected vs actual, §6.4).

    `ids` is a comma-separated list (max 50) so a page of reservations costs one
    request. Reservations are naive LOCAL times; readings are naive UTC strings.
    """
    _require_admin_or_collaborator(current_user)
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()][:50]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be comma-separated integers.")

    def _load():
        res = db_sql.query(Reservation).filter(Reservation.id.in_(wanted)).all()
        devices = db_sql.query(SensorDevice.id, SensorDevice.room_id).filter(
            SensorDevice.room_id.in_({r.room_id for r in res})
        ).all()
        return res, devices

    reservations, devices = await run_in_threadpool(_load)
    sensors_of: dict[str, list[str]] = {}
    for sensor_id, room_id in devices:
        sensors_of.setdefault(room_id, []).append(sensor_id)

    fmt = "%Y-%m-%dT%H:%M:%S.%f"
    to_utc = lambda dt: (dt - timedelta(hours=LOCAL_UTC_OFFSET_HOURS)).strftime(fmt)
    out = []
    for r in reservations:
        sensors = sensors_of.get(r.room_id, [])
        stats = []
        if sensors:
            stats = await db["sensor_readings"].aggregate([
                {"$match": {
                    "sensor_id": {"$in": sensors},
                    # Skip simulation-tool readings; node telemetry counts even from Wokwi.
                    "$or": [{"is_simulated": {"$ne": True}}, {"node_state": {"$ne": None}}],
                    "timestamp": {"$gte": to_utc(r.start_time), "$lt": to_utc(r.end_time)},
                    "cognitive_action.actual_occupancy": {"$ne": None},
                }},
                {"$group": {
                    "_id": None,
                    "n": {"$sum": 1},
                    "avg": {"$avg": "$cognitive_action.actual_occupancy"},
                    "peak": {"$max": "$cognitive_action.actual_occupancy"},
                }},
            ]).to_list(length=1)
        g = stats[0] if stats else None
        out.append({
            "reservation_id": r.id,
            "samples": g["n"] if g else 0,
            "actual_avg": round(g["avg"], 1) if g and g["avg"] is not None else None,
            "actual_peak": int(g["peak"]) if g and g["peak"] is not None else None,
        })
    return out


class ScheduleOut(BaseModel):
    day_of_week: int   # 0=Monday … 6=Sunday
    start_time: str    # HH:MM local
    end_time: str
    expected_people: int


@router.get("/rooms/{room_id}/schedules", response_model=List[ScheduleOut])
def room_schedules(
    room_id: str,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Weekly class schedule of a room: the other source of expected occupancy."""
    rows = (
        db.query(Schedule).filter(Schedule.room_id == room_id)
        .order_by(Schedule.day_of_week, Schedule.start_time).all()
    )
    return [{
        "day_of_week": s.day_of_week,
        "start_time": s.start_time.strftime("%H:%M"),
        "end_time": s.end_time.strftime("%H:%M"),
        "expected_people": s.expected_people,
    } for s in rows]


class WeekBlockOut(BaseModel):
    room_id: str
    day: int             # 0=Monday … 6=Sunday
    start: str           # HH:MM local
    end: str
    kind: str            # class | reservation
    people: int
    by: Optional[str] = None   # who reserved; hidden from guests


class WeekOut(BaseModel):
    week_start: str      # Monday, YYYY-MM-DD
    blocks: List[WeekBlockOut]


@router.get("/occupancy/week", response_model=WeekOut)
def occupancy_week(
    start: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="Any day of the week; default: this week"),
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """When every room is occupied in one week: weekly classes plus that week's
    reservations, in one request for all rooms (design rule 11)."""
    from datetime import date as date_cls
    from app.core.timeutils import local_now
    from app.services.week_schedule import build_week, monday_of

    monday = monday_of(date_cls.fromisoformat(start) if start else local_now().date())
    week_start = datetime.combine(monday, dt_time.min)
    schedules = db.query(Schedule).all()
    reservations = db.query(Reservation).filter(
        Reservation.start_time >= week_start, Reservation.start_time < week_start + timedelta(days=7),
    ).all()
    for r in reservations:
        r.username = r.user.username if r.user else None
    blocks = build_week(schedules, reservations, monday, show_users=current_user.role in ("admin", "collaborator"))
    return {"week_start": monday.isoformat(), "blocks": blocks}


# ANTES: @router.post("/reservations", status_code=201)
# AHORA: response_model=ReservationOut + responses={401,403,404,409} — Action 3
# (Crear reserva). El 409 es clave: significa choque de horario en el aula.
@router.post(
    "/reservations",
    status_code=201,
    response_model=ReservationOut,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
)
def create_reservation(
    payload: ReservationIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    _require_admin_or_collaborator(current_user)

    if not db.query(Room).filter(Room.id == payload.room_id).first():
        raise HTTPException(status_code=404, detail=f"Room id={payload.room_id} not found.")

    # Normalize to naive LOCAL wall-clock (America/Lima) so it matches the
    # naive DateTime column in PostgreSQL — tz-aware input is shifted, never
    # just stripped (see app.core.timeutils.ensure_local).
    start = ensure_local(payload.start_time)
    end = ensure_local(payload.end_time)

    if end <= start:
        raise HTTPException(status_code=400, detail="end_time must be after start_time.")

    # No overlapping reservations per room: [start, end) must not intersect an
    # existing window, or two users would hold "the active reservation" at once.
    overlap = db.query(Reservation).filter(
        Reservation.room_id == payload.room_id,
        Reservation.start_time < end,
        Reservation.end_time > start,
    ).first()
    if overlap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Room '{payload.room_id}' already has a reservation overlapping "
                f"{start.isoformat()} – {end.isoformat()} (reservation id={overlap.id})."
            ),
        )

    reservation = Reservation(
        room_id=payload.room_id,
        user_id=current_user.id,
        start_time=start,
        end_time=end,
        expected_occupancy=payload.expected_occupancy,
    )
    db.add(reservation)
    try:
        db.commit()
        db.refresh(reservation)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating reservation: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    logger.info(
        "Reservation id=%d created by '%s' for room_id=%s.",
        reservation.id, current_user.username, reservation.room_id,
    )
    return {
        "id": reservation.id,
        "room_id": reservation.room_id,
        "user_id": reservation.user_id,
        "start_time": reservation.start_time.isoformat(),
        "end_time": reservation.end_time.isoformat(),
        "expected_occupancy": reservation.expected_occupancy,
    }


# ANTES: @router.delete("/reservations/{reservation_id}", status_code=200)
# AHORA: se agrega responses={401,403,404} (sin response_model porque el
# mensaje de éxito de la Action 5 usa el slot {reserva_id}, no el body).
@router.delete(
    "/reservations/{reservation_id}",
    status_code=200,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
def delete_reservation(
    reservation_id: int,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Delete a reservation. Accessible to admin and collaborator roles."""
    _require_admin_or_collaborator(current_user)
    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation:
        raise HTTPException(status_code=404, detail=f"Reservation id={reservation_id} not found.")
    try:
        db.delete(reservation)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error deleting reservation id=%d: %s", reservation_id, exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    logger.info("Reservation id=%d deleted by '%s'.", reservation_id, current_user.username)
    return {"deleted": reservation_id}


# ANTES: @router.put("/reservations/{reservation_id}")
# AHORA: response_model=ReservationOut + responses={401,403,404,409} — Action 6
# (Editar reserva).
@router.put(
    "/reservations/{reservation_id}",
    response_model=ReservationOut,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
)
def update_reservation(
    reservation_id: int,
    payload: ReservationIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    _require_admin_or_collaborator(current_user)

    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation:
        raise HTTPException(status_code=404, detail=f"Reservation id={reservation_id} not found.")

    if not db.query(Room).filter(Room.id == payload.room_id).first():
        raise HTTPException(status_code=404, detail=f"Room id={payload.room_id} not found.")

    start = ensure_local(payload.start_time)
    end = ensure_local(payload.end_time)

    if end <= start:
        raise HTTPException(status_code=400, detail="end_time must be after start_time.")

    # Same no-overlap rule as creation, excluding the reservation being edited.
    overlap = db.query(Reservation).filter(
        Reservation.id != reservation_id,
        Reservation.room_id == payload.room_id,
        Reservation.start_time < end,
        Reservation.end_time > start,
    ).first()
    if overlap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Room '{payload.room_id}' already has a reservation overlapping "
                f"{start.isoformat()} – {end.isoformat()} (reservation id={overlap.id})."
            ),
        )

    reservation.room_id = payload.room_id
    reservation.start_time = start
    reservation.end_time = end
    reservation.expected_occupancy = payload.expected_occupancy

    try:
        db.commit()
        db.refresh(reservation)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error updating reservation: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    logger.info(
        "Reservation id=%d updated by '%s' for room_id=%s.",
        reservation.id, current_user.username, reservation.room_id,
    )
    return {
        "id": reservation.id,
        "room_id": reservation.room_id,
        "user_id": reservation.user_id,
        "start_time": reservation.start_time.isoformat(),
        "end_time": reservation.end_time.isoformat(),
        "expected_occupancy": reservation.expected_occupancy,
    }


# ENDPOINT NUEVO — cambia solo la hora de una reserva, manteniendo el mismo día.
# Pensado para Watson: la action de "editar reserva" solo pide hora_inicio y
# hora_fin (slots tipo Time), sin fecha — evita el datetime completo en
# formato ISO que causaba fallas cuando el slot combinado de fecha+hora no
# serializaba como se esperaba. Para cambiar de día, la guía indica cancelar
# la reserva y crear una nueva (no hace falta este endpoint para eso).
@router.patch(
    "/reservations/{reservation_id}/time",
    response_model=ReservationOut,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
)
def update_reservation_time(
    reservation_id: int,
    payload: ReservationTimeUpdateIn,
    db: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    _require_admin_or_collaborator(current_user)

    reservation = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not reservation:
        raise HTTPException(status_code=404, detail=f"Reservation id={reservation_id} not found.")

    start_t = _parse_time(payload.start_time)
    end_t = _parse_time(payload.end_time)

    # Mismo día de la reserva original — solo cambia la hora.
    day = reservation.start_time.date()
    new_start = datetime.combine(day, start_t)
    new_end = datetime.combine(day, end_t)

    if new_end <= new_start:
        raise HTTPException(status_code=400, detail="end_time must be after start_time.")

    overlap = db.query(Reservation).filter(
        Reservation.id != reservation_id,
        Reservation.room_id == reservation.room_id,
        Reservation.start_time < new_end,
        Reservation.end_time > new_start,
    ).first()
    if overlap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Room '{reservation.room_id}' already has a reservation overlapping "
                f"{new_start.isoformat()} – {new_end.isoformat()} (reservation id={overlap.id})."
            ),
        )

    reservation.start_time = new_start
    reservation.end_time = new_end

    try:
        db.commit()
        db.refresh(reservation)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error updating reservation time: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    logger.info(
        "Reservation id=%d time updated by '%s' → %s - %s.",
        reservation.id, current_user.username, new_start.isoformat(), new_end.isoformat(),
    )
    return {
        "id": reservation.id,
        "room_id": reservation.room_id,
        "user_id": reservation.user_id,
        "start_time": reservation.start_time.isoformat(),
        "end_time": reservation.end_time.isoformat(),
        "expected_occupancy": reservation.expected_occupancy,
    }


# ── Room 24h Timeline ─────────────────────────────────────────────────────────

# Diurnal envelope amplitude (°C) and occupancy thermal bump per person (°C)
_DIURNAL_AMPLITUDE_C = 1.5
_OCCUPANCY_TREND_BUMP_C = 0.08
_BASELINE_LOOKBACK_HOURS = 3
# Hardware contract: AC always boots at 20.0°C when transitioning to ON. Any OFF→ON
# edge re-applies this factory default until the AI emits a new cognitive_action.target.
AC_FACTORY_DEFAULT_C = 20.0


def _compute_past_ac_state(events: list) -> tuple[str, Optional[float]]:
    """Derive (status, setpoint) for one past hourly bucket from ordered AC events.

    Rules:
      * status='ON' when any reading in the hour reports ac_status='ON', else 'OFF'.
      * setpoint=None when status='OFF'.
      * When ON: setpoint is the mean of per-reading targets where every null/0 target
        is replaced by AC_FACTORY_DEFAULT_C, and every OFF→ON transition (including the
        case where the bucket's first reading is already ON) injects an additional
        AC_FACTORY_DEFAULT_C sample to weight the boot state.
      * Defensive floor: if the computed mean is still None/0, return AC_FACTORY_DEFAULT_C.
    """
    if not events:
        return "OFF", None
    samples: list[float] = []
    on_seen = False
    prev_on = False
    for ev in events:
        is_on = (ev.get("status") == "ON")
        if is_on:
            on_seen = True
            target = ev.get("target")
            if target is None or target == 0:
                samples.append(AC_FACTORY_DEFAULT_C)
            else:
                samples.append(float(target))
            if not prev_on:
                # OFF→ON edge (or bucket-opening ON): hardware boots to factory setpoint
                samples.append(AC_FACTORY_DEFAULT_C)
        prev_on = is_on
    if not on_seen:
        return "OFF", None
    if not samples:
        return "ON", AC_FACTORY_DEFAULT_C
    avg = sum(samples) / len(samples)
    if not avg:
        avg = AC_FACTORY_DEFAULT_C
    return "ON", round(float(avg), 2)


def _timeline_expected_people(schedules: list, anchor: datetime) -> int:
    # Schedules are stored in LOCAL wall-clock time; the anchor is UTC
    # (see app.core.timeutils — the weekday changes with the shift too).
    local = to_local(anchor)
    dow = local.weekday()
    hhmm = local.time()
    for s in schedules:
        if s.day_of_week == dow and s.start_time <= hhmm <= s.end_time:
            return s.expected_people
    return 0


def _timeline_reservation_for(reservations: list, anchor_naive: datetime) -> Optional[dict]:
    # Reservations are stored in LOCAL wall-clock time; anchor_naive is naive UTC.
    local = to_local(anchor_naive)
    for r in reservations:
        if r.start_time <= local < r.end_time:
            return {
                "id": r.id,
                "username": r.user.username if r.user else None,
                "expected_occupancy": r.expected_occupancy,
                "start_time": r.start_time.isoformat(),
                "end_time": r.end_time.isoformat(),
            }
    return None


@router.get("/rooms/{room_id}/timeline")
async def get_room_timeline(
    room_id: str,
    db_sql: Session = Depends(get_db_sql),
    db_mongo: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """24h room timeline: 12 past hourly buckets + current hour + 12 future predictions.

    Past hours (now-12h … now) aggregate real Mongo sensor readings per hour and the
    AC cognitive_action snapshot (status + applied setpoint). Configured setpoint is
    pulled from PostgreSQL (Room.target_temp).
    Future hours (now … now+12h) compute predicted_temperature from a diurnal drift
    over the recent baseline plus schedule occupancy, then derive the suggested AC
    setpoint via calculate_cooling_demand.
    Reservations that overlap any anchor are injected with username + expected_occupancy.
    Returns 25 chronologically sorted points.
    """
    _require_admin_or_collaborator(current_user)

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=12)
    end = now + timedelta(hours=12)

    # 25 hour-aligned anchors centred on the current hour floor
    now_hour = now.replace(minute=0, second=0, microsecond=0)
    anchors = [now_hour + timedelta(hours=i) for i in range(-12, 13)]

    # PG lookups (sync) — bridged to async via threadpool, room lookup included
    # so no blocking query runs on the event loop.
    def _load_pg_context():
        room = db_sql.query(Room).filter(Room.id == room_id).first()
        if room is None:
            return None, [], [], []
        sensor_ids = [d.id for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
        # Reservations are stored in LOCAL wall-clock time — shift the UTC window
        # bounds to local before prefiltering (see app.core.timeutils), otherwise
        # reservations in the earliest |offset| hours of the window are dropped
        # before _timeline_reservation_for ever sees them.
        start_local = to_local(start)
        end_local = to_local(end)
        reservations = (
            db_sql.query(Reservation)
            .filter(
                Reservation.room_id == room_id,
                Reservation.start_time < end_local,
                Reservation.end_time > start_local,
            )
            .all()
        )
        # Force-load user relationship while session is open
        for r in reservations:
            _ = r.user.username if r.user else None
        schedules = db_sql.query(Schedule).filter(Schedule.room_id == room_id).all()
        return room, sensor_ids, reservations, schedules

    room, sensor_ids, reservations, schedules = await run_in_threadpool(_load_pg_context)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Room id='{room_id}' not found.")

    # Aggregate past 12h readings per hour
    start_iso = start.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.%f")
    now_iso = now.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.%f")
    past_buckets: dict[datetime, dict] = {}
    if sensor_ids:
        pipeline = [
            {"$match": {
                "sensor_id": {"$in": sensor_ids},
                "timestamp": {"$gte": start_iso, "$lte": now_iso},
            }},
            {"$addFields": {"_ts": {"$dateFromString": {"dateString": "$timestamp"}}}},
            # Sort BEFORE grouping so $push preserves chronological order — required for
            # OFF→ON transition detection in _compute_past_ac_state.
            {"$sort": {"_ts": 1}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%dT%H:00:00", "date": "$_ts"}},
                "avg_temp": {"$avg": "$temperature"},
                "avg_humidity": {"$avg": "$humidity"},
                "avg_co_ppm": {"$avg": "$co_ppm"},
                "count": {"$sum": 1},
                "ac_events": {"$push": {
                    "status": "$cognitive_action.ac_status",
                    "target": "$cognitive_action.target",
                }},
            }},
        ]
        try:
            async for doc in db_mongo["sensor_readings"].aggregate(pipeline):
                hour_dt = datetime.strptime(doc["_id"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                past_buckets[hour_dt] = doc
        except Exception as exc:
            logger.error("Mongo timeline aggregation failed for room '%s': %s", room_id, exc)
            raise HTTPException(status_code=503, detail="Sensor history unavailable") from exc

    # Predictive baseline for the *AC-free* ambient. Naively averaging recent measured
    # temps biases the baseline downward whenever the AC was running, so the resulting
    # "sin-AC" prediction would secretly carry the cooling effect. Prefer hours where the
    # AC was OFF; degrade to the global mean, then to target_temp, when none are available.
    def _bucket_temp(h: datetime) -> Optional[float]:
        return past_buckets[h].get("avg_temp")

    off_hours = [
        h for h in past_buckets
        if _bucket_temp(h) is not None
        and _compute_past_ac_state(past_buckets[h].get("ac_events", []))[0] == "OFF"
    ]
    if off_hours:
        recent_off = sorted(off_hours, reverse=True)[:_BASELINE_LOOKBACK_HOURS]
        baseline_temp = sum(_bucket_temp(h) for h in recent_off) / len(recent_off)
        baseline_source = "ac_off_hours"
    else:
        all_temps = [_bucket_temp(h) for h in past_buckets if _bucket_temp(h) is not None]
        if all_temps:
            baseline_temp = sum(all_temps) / len(all_temps)
            baseline_source = "all_hours_mean"
        else:
            baseline_temp = room.target_temp
            baseline_source = "target_fallback"

    timeline: list[dict] = []
    for anchor in anchors:
        anchor_naive = anchor.replace(tzinfo=None)
        if anchor < now_hour:
            phase = "past"
        elif anchor == now_hour:
            phase = "current"
        else:
            phase = "future"

        point: dict = {
            "hour": anchor.isoformat(),
            "phase": phase,
            "reservation": _timeline_reservation_for(reservations, anchor_naive),
        }

        if anchor <= now_hour:
            bucket = past_buckets.get(anchor)
            if bucket:
                ac_status, ac_setpoint = _compute_past_ac_state(bucket.get("ac_events", []))
                point["actual_temperature"] = round(bucket["avg_temp"], 2) if bucket.get("avg_temp") is not None else None
                point["actual_humidity"] = round(bucket["avg_humidity"], 2) if bucket.get("avg_humidity") is not None else None
                point["actual_co_ppm"] = round(bucket["avg_co_ppm"], 2) if bucket.get("avg_co_ppm") is not None else None
                point["readings_count"] = bucket.get("count", 0)
                point["ac"] = {
                    "status": ac_status,
                    "setpoint": ac_setpoint,
                    "configured_setpoint": room.target_temp,
                }
            else:
                point["actual_temperature"] = None
                point["actual_humidity"] = None
                point["actual_co_ppm"] = None
                point["readings_count"] = 0
                point["ac"] = {
                    "status": "OFF",
                    "setpoint": None,
                    "configured_setpoint": room.target_temp,
                }
        else:
            # Diurnal sine phased on *local* time so the peak lands in the local afternoon
            # (~15:00) and the trough at night (~03:00). anchor is UTC; shift to local.
            local_hour = to_local(anchor).hour
            drift = _DIURNAL_AMPLITUDE_C * math.sin((local_hour - 9) / 24.0 * 2 * math.pi)
            expected_people = _timeline_expected_people(schedules, anchor)
            occupancy_offset = expected_people * _OCCUPANCY_TREND_BUMP_C
            # predicted_temp is the AC-free ambient the room would drift toward.
            predicted_temp = round(baseline_temp + drift + occupancy_offset, 2)

            room_context = {
                "room_id": room.id,
                "room_name": room.name,
                "max_capacity": room.max_capacity,
                "target_temp": room.target_temp,
                "expected_people": expected_people,
                "device_id": sensor_ids[0] if sensor_ids else None,
            }
            cog = await calculate_cooling_demand(predicted_temp, room_context, anchor)

            # projected_temp is the realized temperature *with* the AC acting: held at the
            # suggested setpoint when the system pre-cools, else equal to the ambient when
            # STANDBY (no mitigation needed). The sin-AC vs con-AC gap is the cooling effect.
            ac_status = cog.get("ac_status")
            suggested = cog.get("target")
            if ac_status == "ON" and suggested is not None:
                projected_temp = round(float(suggested), 2)
            else:
                projected_temp = predicted_temp

            point["predicted_temperature"] = predicted_temp
            point["projected_temperature"] = projected_temp
            point["expected_people"] = expected_people
            point["ac"] = {
                "status": ac_status,
                "suggested_setpoint": suggested,
                "configured_setpoint": room.target_temp,
                "cooling_mode": cog.get("cooling_mode"),
                "model": cog.get("model"),
            }

        timeline.append(point)

    return {
        "room_id": room.id,
        "room_name": room.name,
        "target_temp": room.target_temp,
        "generated_at": now.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "baseline_temp": round(baseline_temp, 2),
        "baseline_source": baseline_source,
        "timeline": timeline,
    }
