import logging
from datetime import datetime, timezone, timedelta
# CAMBIO: antes era "from typing import Optional" — se agregó "List" porque
# ahora usamos response_model=List[...] en list_readings.
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.errors import PyMongoError
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.timeutils import local_now
from app.models.sensor import SensorReading
from app.models.admin import SensorDevice, Reservation, User, Room
# LÍNEA NUEVA — antes este import no existía. Trae el modelo de error compartido.
from app.models.common import ErrorDetail
from app.services.data_service import process_reading
from app.database import get_db
from app.database_sql import get_db_sql
from app.dependencies import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

# Readings produced by the dashboard's simulation tool: flagged simulated and without
# a node decision. Node telemetry (node_state set) is kept even when the node itself
# runs in a simulator (Wokwi), because it is the history RF-30/RF-31 are computed on.
SIM_TOOL_READINGS = {"is_simulated": True, "node_state": None}


# ============================================================================
# BLOQUE NUEVO — todas estas clases (CognitiveActionOut, SensorReadingOut,
# EmergencyEntryOut, EmergenciesOut, SensorControlOut) no existían antes.
# Antes cada endpoint solo hacía "return {...}" o "return docs" sin declarar
# la forma de la respuesta, por eso en Watson el panel "Response" salía vacío.
# ============================================================================
# Declared so Watson's extension "Response" panel can expose these fields as
# insertable variables — FastAPI otherwise emits an empty schema for plain
# dict/list returns, leaving Watson with nothing to map.

class CognitiveActionOut(BaseModel):
    ac_status: str
    cooling_mode: Optional[str] = None
    target: Optional[float] = None
    thermal_load_offset: Optional[float] = None
    model: str
    expected_occupancy: Optional[float] = None
    actual_occupancy: Optional[float] = None
    effective_occupancy: Optional[float] = None
    occupancy_gap: Optional[float] = None


class LearningActionOut(BaseModel):
    fan_pct: int
    heater_pct: int = 0
    ready: bool
    # Predictive controller (services/forecast_controller.py): median temperature expected
    # in 30 min, highest 90th percentile in that window, minutes of history, model used.
    forecast_30: Optional[float] = None
    forecast_30_p90: Optional[float] = None
    minutes_seen: Optional[int] = None
    model: Optional[str] = None
    minutes_learned: Optional[int] = None   # readings stored by the earlier learning controller


class SensorReadingOut(BaseModel):
    sensor_id: str
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    co2_ppm: Optional[float] = None
    co_ppm: float = 0.0
    camera_occupancy: Optional[int] = None
    is_simulated: bool = False
    timestamp: str
    cognitive_action: Optional[CognitiveActionOut] = None
    # Decision taken on the ESP32 node (readings ingested over MQTT); None for HTTP readings.
    node_state: Optional[dict] = None
    # What the predictive controller would do, in shadow (services/forecast_controller.py).
    learning_action: Optional[LearningActionOut] = None


class EmergencyEntryOut(BaseModel):
    room_id: str
    room_name: str
    sensor_id: str
    co_ppm: float
    timestamp: Optional[str] = None
    is_simulated: bool


class EmergenciesOut(BaseModel):
    real: List[EmergencyEntryOut]
    simulated: List[EmergencyEntryOut]


class SensorControlOut(BaseModel):
    sensor_id: str
    room_id: Optional[str] = None
    is_active: bool
    control_enabled: bool


@router.post("/", status_code=201)
async def ingest_reading(
    reading: SensorReading,
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
):
    try:
        result = await process_reading(db, reading, db_sql=db_sql)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return result


# ANTES: @router.get("/")
# AHORA: se agrega response_model=List[SensorReadingOut] — es el endpoint de
# la Action 1 (Estado del aula) de la guía de Watson.
@router.get("/", response_model=List[SensorReadingOut])
async def list_readings(
    room_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    limit: int = Query(40, ge=1, le=2000),
    since: Optional[str] = Query(None, description="Only readings newer than this stored timestamp (incremental polling)"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
):
    mongo_filter: dict = {}

    if room_id is not None:
        ids = await run_in_threadpool(
            lambda: [d.id for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
        )
        if not ids:
            return []
        mongo_filter["sensor_id"] = {"$in": ids}
    elif sensor_id is not None:
        mongo_filter["sensor_id"] = sensor_id

    if since:
        mongo_filter["timestamp"] = {"$gt": since}
    cursor = db["sensor_readings"].find(mongo_filter).sort("timestamp", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    for d in docs:
        d.pop("_id", None)
    return docs


class RoomLatestOut(BaseModel):
    room_id: Optional[str] = None
    sensor_id: Optional[str] = None
    reading: Optional[SensorReadingOut] = None


@router.get("/latest", response_model=List[RoomLatestOut])
async def latest_by_room(
    by: str = Query("room", pattern="^(room|sensor)$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Latest reading of every room in one response (one aggregation, not one request per room).

    Rooms without sensors or without readings are included with reading=None.
    by=sensor returns one row per registered sensor instead (for the sensors table).
    """
    def _load():
        rooms = [r.id for r in db_sql.query(Room.id).all()]
        devices = db_sql.query(SensorDevice.id, SensorDevice.room_id).all()
        return rooms, devices

    rooms, devices = await run_in_threadpool(_load)
    room_of = {sensor_id: room_id for sensor_id, room_id in devices}
    pipeline = [
        {"$match": {"sensor_id": {"$in": list(room_of)}}},
        {"$sort": {"sensor_id": 1, "timestamp": -1}},   # served by the (sensor_id, timestamp) index
        {"$group": {"_id": "$sensor_id", "doc": {"$first": "$$ROOT"}}},
    ]
    try:
        groups = await db["sensor_readings"].aggregate(pipeline).to_list(length=None)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    if by == "sensor":
        last = {g["_id"]: {k: v for k, v in g["doc"].items() if k != "_id"} for g in groups}
        return [{"sensor_id": sid, "room_id": rid, "reading": last.get(sid)} for sid, rid in devices]

    latest: dict[str, dict] = {}
    for g in groups:
        doc = g["doc"]
        doc.pop("_id", None)
        room_id = room_of.get(g["_id"])
        # A room may have several sensors: keep its most recent reading.
        if room_id and (room_id not in latest or doc["timestamp"] > latest[room_id]["timestamp"]):
            latest[room_id] = doc
    return [{"room_id": r, "reading": latest.get(r)} for r in rooms]


class SparkPointOut(BaseModel):
    timestamp: str
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    co2_ppm: Optional[float] = None
    co_ppm: Optional[float] = None


class SensorRecentOut(BaseModel):
    sensor_id: str
    room_id: Optional[str] = None
    points: List[SparkPointOut]


SPARK_FIELDS = ("temperature", "humidity", "co2_ppm", "co_ppm")


def downsample(points: list[dict], max_points: int) -> list[dict]:
    """Keep at most max_points, evenly spaced, always including the last one."""
    n = len(points)
    if n <= max_points:
        return points
    step = (n - 1) / (max_points - 1)
    return [points[round(i * step)] for i in range(max_points)]


@router.get("/recent", response_model=List[SensorRecentOut])
async def recent_by_sensor(
    minutes: int = Query(30, ge=5, le=180),
    max_points: int = Query(30, ge=5, le=120),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """The last `minutes` of every sensor, for the Resumen sparklines, in one response.

    The window ends at each sensor's own latest reading, so a sensor that stopped
    reporting still shows how it was doing before it went quiet.
    """
    devices = await run_in_threadpool(lambda: db_sql.query(SensorDevice.id, SensorDevice.room_id).all())
    room_of = {sid: rid for sid, rid in devices}
    if not room_of:
        return []
    try:
        lasts = await db["sensor_readings"].aggregate([
            {"$match": {"sensor_id": {"$in": list(room_of)}}},
            {"$sort": {"sensor_id": 1, "timestamp": -1}},
            {"$group": {"_id": "$sensor_id", "ts": {"$first": "$timestamp"}}},
        ]).to_list(length=None)
        windows = []
        for g in lasts:
            try:
                end = datetime.fromisoformat(g["ts"].replace("Z", ""))
            except ValueError:
                continue
            start = (end - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
            windows.append({"sensor_id": g["_id"], "timestamp": {"$gte": start, "$lte": g["ts"]}})
        docs = await db["sensor_readings"].find(
            {"$or": windows} if windows else {"_id": None},
            {"_id": 0, "sensor_id": 1, "timestamp": 1, **{f: 1 for f in SPARK_FIELDS}},
        ).sort([("sensor_id", 1), ("timestamp", 1)]).to_list(length=None)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    series: dict[str, list[dict]] = {}
    for d in docs:
        series.setdefault(d.pop("sensor_id"), []).append(d)
    return [{"sensor_id": sid, "room_id": room_of[sid], "points": downsample(pts, max_points)}
            for sid, pts in series.items()]


class RoomEventOut(BaseModel):
    room_id: str
    timestamp: str
    kind: str
    text: str
    tone: str


@router.get("/events", response_model=List[RoomEventOut])
async def recent_events(
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(8, ge=1, le=50),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Latest events of every room (CO₂/CO alerts and sensors that stopped reporting),
    newest first, derived from the last `hours` of readings in one query."""
    from app.services.room_events import detect_events

    devices = await run_in_threadpool(lambda: db_sql.query(SensorDevice.id, SensorDevice.room_id).all())
    room_of = {sid: rid for sid, rid in devices if rid}
    if not room_of:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        docs = await db["sensor_readings"].find(
            {"sensor_id": {"$in": list(room_of)}, "timestamp": {"$gte": since}},
            {"_id": 0, "sensor_id": 1, "timestamp": 1, "co2_ppm": 1, "co_ppm": 1},
        ).sort([("sensor_id", 1), ("timestamp", 1)]).to_list(length=None)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    by_sensor: dict[str, list[dict]] = {}
    for d in docs:
        by_sensor.setdefault(d["sensor_id"], []).append(d)
    events = [e for sid, rows in by_sensor.items() for e in detect_events(room_of[sid], rows, now)]
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events[:limit]


class NodeComparisonOut(BaseModel):
    room_id: str
    sensor_id: str
    compared: int
    matches: int
    coincidence_pct: Optional[float] = None


@router.get("/comparison/summary", response_model=List[NodeComparisonOut])
async def strategy_comparison_summary(
    hours: float = Query(24, gt=0, le=168),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Coincidence node vs AI for every node in one aggregation (overview, RF-29/30).

    Uses the per-reading `comparison` recorded at ingest; readings without it (CO alarm,
    manual order) are not heuristic decisions and do not count.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    devices = await run_in_threadpool(lambda: db_sql.query(SensorDevice.id, SensorDevice.room_id).all())
    room_of = {sid: rid for sid, rid in devices if rid}
    try:
        groups = await db["sensor_readings"].aggregate([
            {"$match": {"timestamp": {"$gte": since}, "comparison": {"$ne": None}}},
            {"$group": {
                "_id": "$sensor_id",
                "n": {"$sum": 1},
                "m": {"$sum": {"$cond": ["$comparison.match", 1, 0]}},
            }},
        ]).to_list(length=None)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return [
        {"room_id": room_of[g["_id"]], "sensor_id": g["_id"], "compared": g["n"], "matches": g["m"],
         "coincidence_pct": round(100 * g["m"] / g["n"], 1) if g["n"] else None}
        for g in groups if g["_id"] in room_of
    ]


@router.get("/rooms/{room_id}/history")
async def room_history(
    room_id: str,
    day: Optional[str] = Query(None, alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """What the room's node measured and did on one local day (latest day with data if
    `date` is missing): 5-min series, classes, summary, events and state changes.
    `format=csv` downloads every reading of that day."""
    from datetime import date as date_cls
    from fastapi.responses import Response
    from app.services.room_history import FMT, OFFSET, PROJECTION, day_bounds, equipment_for, summarize_day

    sensors = await run_in_threadpool(
        lambda: [d.id for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
    )
    base = {"sensor_id": {"$in": sensors}, "node_state.accion": {"$exists": True}}
    try:
        first = await db["sensor_readings"].find(base, {"timestamp": 1}).sort("timestamp", 1).limit(1).to_list(length=1)
        last = await db["sensor_readings"].find(base, {"timestamp": 1}).sort("timestamp", -1).limit(1).to_list(length=1)
        if not last:
            return {"room_id": room_id, "date": None}
        data_end = datetime.fromisoformat(last[0]["timestamp"][:19])
        anchor = date_cls.fromisoformat(day) if day else (data_end + OFFSET).date()
        start, end = day_bounds(anchor)
        match = {**base, "timestamp": {"$gte": start.strftime(FMT), "$lt": end.strftime(FMT)}}
        docs = await db["sensor_readings"].find(match, PROJECTION).sort("timestamp", 1).to_list(length=None)
        meta = await db["strategy_simulation_meta"].find_one({"_id": room_id})
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    if format == "csv":
        lines = ["timestamp_utc,temperature,humidity,co2_ppm,co_ppm,accion,ventilador_pct,calefactor,ocupada,alarma_co,manual,motivo"]
        for d in docs:
            ns = d.get("node_state") or {}
            motivo = (ns.get("motivo") or "").replace('"', "'")
            lines.append(",".join(str(v) for v in [
                d["timestamp"][:19], "" if d.get("temperature") is None else d["temperature"], "" if d.get("humidity") is None else d["humidity"], d.get("co2_ppm") or "", d.get("co_ppm") or "",
                ns.get("accion", ""), round((ns.get("ventilador_pwm") or 0) * 100 / 255), int(bool(ns.get("calefactor"))),
                int((ns.get("co2_proxy") or 0) >= 2200), int(bool(ns.get("alarma_co"))), int(bool(ns.get("manual"))), f'"{motivo}"',
            ]))
        return Response("\n".join(lines) + "\n", media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="historial_{room_id}_{anchor.isoformat()}.csv"'})

    equipment = equipment_for(meta, start, end)
    result = summarize_day(docs, equipment["fan_max_w"], equipment["heater_w"])
    if meta and meta.get("start") and meta["start"] < end and meta["end"] >= start:
        # Simulated days know how many people came: same class intervals as "Aprendizaje".
        from app.services.strategy_series import _intervals
        minutes = await db["strategy_simulation"].aggregate([
            {"$match": {"room_id": room_id, "strategy": "rules", "ts": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": {"$dateTrunc": {"date": "$ts", "unit": "minute"}},
                        "occ": {"$max": "$occupied"}, "people": {"$max": "$people"}}},
        ]).to_list(length=None)
        result["classes"] = _intervals(minutes)
    return {
        "room_id": room_id,
        "date": anchor.isoformat(),
        "start": start.strftime(FMT),
        "end": end.strftime(FMT),
        "data_start": first[0]["timestamp"][:19],
        "data_end": data_end.strftime(FMT),
        "equipment": equipment,
        **result,
    }


@router.get("/rooms/{room_id}/strategies")
async def room_strategies(
    room_id: str,
    range_key: str = Query("1d", alias="range", pattern="^(12h|1d|7d|30d)$"),
    source: str = Query("auto", pattern="^(auto|simulation|live)$"),
    day: Optional[str] = Query(None, alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Rules vs learning for one room, one point per time bucket.

    `date` (local, YYYY-MM-DD) picks the day, its week or its month; without it, the
    period holding the latest data. Periods follow the local calendar.

    `auto` uses the room's strategy simulation when one exists (both strategies with
    their own temperature) and the node's live readings otherwise (rules + the
    learning controller's shadow decisions).
    """
    from datetime import date as date_cls
    from app.services.strategy_series import live_series, simulation_series

    anchor = date_cls.fromisoformat(day) if day else None

    try:
        meta = await db["strategy_simulation_meta"].find_one({"_id": room_id})
        if meta and source != "live":
            return await simulation_series(db, room_id, meta, range_key, anchor)
        sensors = await run_in_threadpool(
            lambda: [d.id for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
        )
        data = await live_series(db, sensors, range_key, anchor) if sensors else None
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return data or {"source": "none"}


@router.get("/comparison")
async def strategy_comparison(
    room_id: str,
    hours: float = Query(24, gt=0, le=168),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Node heuristic vs AI over the last `hours` (RF-30, RF-31).

    JSON: coincidence %, divergence breakdown and recent divergences with context,
    and the estimated energy of each strategy over the same window.
    CSV: every compared reading, for the report's results figure.
    """
    from fastapi.responses import Response
    from app.services.strategy_comparison import summarize, compare_reading

    sensors = await run_in_threadpool(
        lambda: [d.id for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
    )
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    projection = {
        "_id": 0, "timestamp": 1, "is_simulated": 1, "temperature": 1, "co2_ppm": 1,
        "node_state": 1, "cognitive_action": 1, "comparison": 1,
    }
    try:
        docs = await db["sensor_readings"].find(
            {"sensor_id": {"$in": sensors}, "timestamp": {"$gte": since}, "node_state": {"$ne": None}},
            projection,
        ).sort("timestamp", 1).to_list(length=None)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    if format == "csv":
        lines = ["timestamp_utc,temperature,co2_ppm,node_action,node_fan_pwm,node_heater,ai_status,ai_mode,ai_target,expected_occupancy,actual_occupancy,match"]
        for d in docs:
            n, a = d.get("node_state") or {}, d.get("cognitive_action") or {}
            cmp = d.get("comparison") or compare_reading(d)
            lines.append(",".join(str(v if v is not None else "") for v in [
                d["timestamp"], d.get("temperature"), d.get("co2_ppm"), n.get("accion"), n.get("ventilador_pwm"),
                n.get("calefactor"), a.get("ac_status"), a.get("cooling_mode"), a.get("target"),
                a.get("expected_occupancy"), a.get("actual_occupancy"), "" if cmp is None else cmp["match"],
            ]))
        return Response("\n".join(lines) + "\n", media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="comparacion_{room_id}.csv"'})

    return {"room_id": room_id, "hours": hours, **summarize(docs)}


class DemoReadingIn(BaseModel):
    room_id: str
    temperature: float
    humidity: float = 55.0
    co2_ppm: float
    co_ppm: float = 0.0


# Demo readings carry the emulated node decision, tagged so they can be told apart and removed.
DEMO_READINGS = {"is_simulated": True, "node_state.demo": True}


@router.post("/demo", responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}})
async def send_demo_reading(
    body: DemoReadingIn,
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Store one simulated reading for a room as if its node had sent it (Simulación page).

    The node's answer comes from the firmware rules (services/node_emulator), so every
    view (Aulas, En vivo, Resumen, alerts) shows what the real node would do. It skips
    the learning controller, which must not learn from made-up readings.
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")
    from app.services.data_service import save_reading
    from app.services.node_emulator import emulate

    device = await run_in_threadpool(
        lambda: db_sql.query(SensorDevice).filter(SensorDevice.room_id == body.room_id).first()
    )
    if device is None:
        raise HTTPException(status_code=404, detail=f"El aula {body.room_id} no tiene nodo registrado.")
    node_state = emulate(body.temperature, body.co2_ppm, body.co_ppm)
    reading = SensorReading(
        sensor_id=device.id, temperature=body.temperature, humidity=body.humidity,
        co2_ppm=body.co2_ppm, co_ppm=body.co_ppm, is_simulated=True, node_state=node_state,
    )
    try:
        await save_reading(db, reading)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"room_id": body.room_id, "sensor_id": device.id, "node_state": node_state}


@router.delete("/demo", responses={403: {"model": ErrorDetail}})
async def clear_demo_readings(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove every demo reading: the rooms go back to their own data."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")
    try:
        result = await db["sensor_readings"].delete_many(DEMO_READINGS)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"deleted": result.deleted_count}


@router.delete("/simulated", status_code=200, responses={403: {"model": ErrorDetail}})
async def purge_simulated_readings(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete ALL simulated readings immediately.

    Called by the dashboard when leaving simulation mode (switch to real data
    or navigating away) so synthetic CO spikes stop feeding /emergencies at
    once, instead of waiting for the 15-second purge window.
    """
    if current_user.role not in ("admin", "collaborator"):
        raise HTTPException(status_code=403, detail="Admin or collaborator role required.")
    try:
        result = await db["sensor_readings"].delete_many(SIM_TOOL_READINGS)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable purging simulated readings: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    if result.deleted_count:
        logger.info("Purged %d simulated readings ('%s').", result.deleted_count, current_user.username)
    return {"deleted": result.deleted_count}


# ── Emergency monitor ────────────────────────────────────────────────────────

_SIM_KEYWORDS = ("SIM", "TEST")


def _looks_synthetic(sensor_id: str, room_id: str | None) -> bool:
    """True when the sensor or room ID contains a known simulation keyword."""
    haystack = ((sensor_id or "") + (room_id or "")).upper()
    return any(kw in haystack for kw in _SIM_KEYWORDS)


# ANTES: @router.get("/emergencies")
# AHORA: se agregan response_model=EmergenciesOut y responses={403:...} — es
# el endpoint de la Action 7 (Emergencias de CO) de la guía de Watson.
@router.get("/emergencies", response_model=EmergenciesOut, responses={403: {"model": ErrorDetail}})
async def get_co_emergencies(
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Return CO > 50 ppm alerts from the last 15 seconds, split into real vs simulated.

    Hard-fallback rules (any one forces simulated):
      1. is_simulated flag is True in the stored reading
      2. sensor_id or resolved room_id contains 'SIM' or 'TEST'
      3. Sensor not registered in SQL (room_id cannot be resolved)
      4. room_id does not exist in the rooms table

    Only real if is_simulated is strictly False AND the room resolves to a known SQL room.
    """
    if current_user.role not in ("admin", "collaborator"):
        raise HTTPException(status_code=403, detail="Admin or collaborator role required.")

    now = datetime.now(timezone.utc)

    # Purge simulated readings older than 15 seconds — if the simulator stops sending,
    # the DB is clean within one polling cycle and the frontend receives nothing.
    fifteen_sec_ago = (now - timedelta(seconds=15)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    purge = await db["sensor_readings"].delete_many(
        {**SIM_TOOL_READINGS, "timestamp": {"$lt": fifteen_sec_ago}}
    )
    if purge.deleted_count:
        print(f"Purging old simulation data...")

    cursor = db["sensor_readings"].find(
        {"co_ppm": {"$gt": 50}, "timestamp": {"$gte": fifteen_sec_ago}}
    ).sort("co_ppm", -1)

    docs = await cursor.to_list(length=200)

    if not docs:
        return {"real": [], "simulated": []}

    # Deduplicate — one entry per sensor_id, highest co_ppm wins (already sorted desc)
    seen: dict[str, dict] = {}
    for doc in docs:
        sid = doc["sensor_id"]
        if sid not in seen:
            seen[sid] = doc

    # Enrich with room names from SQL — one threadpool hop for both queries:
    # this endpoint is polled every 5 s per client.
    sensor_ids = list(seen.keys())

    def _load_sql_context() -> tuple[dict[str, str], dict[str, str]]:
        devices = db_sql.query(SensorDevice).filter(SensorDevice.id.in_(sensor_ids)).all()
        room_ids = list({d.room_id for d in devices})
        rooms = db_sql.query(Room).filter(Room.id.in_(room_ids)).all()
        return {d.id: d.room_id for d in devices}, {r.id: r.name for r in rooms}

    device_to_room, room_name_map = await run_in_threadpool(_load_sql_context)

    real: list[dict] = []
    simulated: list[dict] = []

    for sid, doc in seen.items():
        rid = device_to_room.get(sid)                 # None → sensor not in SQL
        resolved_name = room_name_map.get(rid) if rid else None  # None → room not in SQL

        is_sim_flag   = bool(doc.get("is_simulated", False))
        id_synthetic  = _looks_synthetic(sid, rid)
        room_unknown  = resolved_name is None         # any gap in SQL chain → synthetic

        # DEBUG: show exactly why each entry was bucketed real vs simulated
        print(
            f"[EMERGENCY] sid={sid} rid={rid} resolved={resolved_name!r} "
            f"is_sim_flag={is_sim_flag} id_synthetic={id_synthetic} room_unknown={room_unknown} "
            f"co_ppm={doc.get('co_ppm')} ts={doc.get('timestamp')}"
        )

        entry = {
            "room_id":    rid or sid,
            "room_name":  resolved_name or "Simulación",
            "sensor_id":  sid,
            "co_ppm":     doc.get("co_ppm", 0),
            "timestamp":  doc.get("timestamp"),
        }

        if is_sim_flag or id_synthetic or room_unknown:
            simulated.append({**entry, "is_simulated": True})
        else:
            real.append({**entry, "is_simulated": False})

    if real:
        logger.warning(
            "CO REAL EMERGENCY — '%s' polling: %d sensor(s) — rooms: %s",
            current_user.username, len(real), [r["room_name"] for r in real],
        )
    if simulated:
        logger.info(
            "CO simulated alert — '%s' polling: %d sensor(s) (hard-fallback applied)",
            current_user.username, len(simulated),
        )

    return {"real": real, "simulated": simulated}


# ── Sensor control ────────────────────────────────────────────────────────────

class SensorControlIn(BaseModel):
    is_active: bool
    control_enabled: bool


class NodeStatusOut(BaseModel):
    room_id: str
    online: bool
    since: Optional[str] = None   # UTC ISO; None when only the retained message is known


@router.get("/nodes", response_model=List[NodeStatusOut])
async def node_status(current_user: User = Depends(get_current_user)):
    """Connectivity of each MQTT node (RF-10), from its status topic and Last Will."""
    from app.services.mqtt_ingest import NODE_STATUS
    return list(NODE_STATUS.values())


class OverrideIn(BaseModel):
    mode: str            # auto | fan | heater | off  ("on" = fan, kept for older clients)
    minutes: int = 15    # duration of a manual order (1..240)
    level: int = 100     # fan power in % for a "fan" order (20..100; PC fans stall below ~20 %)


# What each manual mode forces on the node (the firmware's accion). The node never runs
# fan and heater together, so the modes are mutually exclusive.
OVERRIDE_ACTIONS = {"fan": "COOLING", "on": "COOLING", "heater": "HEATING", "off": "STANDBY"}


def override_command(mode: str, minutes: int, by: str, level: int = 100) -> dict:
    """MQTT command for a manual order; raises ValueError for an unknown mode."""
    if mode == "auto":
        return {"modo": "auto"}
    if mode not in OVERRIDE_ACTIONS:
        raise ValueError(mode)
    command = {"modo": "manual", "accion": OVERRIDE_ACTIONS[mode], "duracion_s": minutes * 60, "por": by}
    if OVERRIDE_ACTIONS[mode] == "COOLING":
        command["nivel"] = level
    return command


def _require_room_control(db_sql: Session, current_user: User, room_id: str) -> None:
    """Admins always; collaborators only during their active reservation on the room (RF-37)."""
    if current_user.role == "admin":
        return
    if current_user.role != "collaborator":
        raise HTTPException(status_code=403, detail="Guests cannot control devices.")
    now = local_now()
    active = db_sql.query(Reservation).filter(
        Reservation.user_id == current_user.id,
        Reservation.room_id == room_id,
        Reservation.start_time <= now,
        Reservation.end_time >= now,
    ).first()
    if not active:
        raise HTTPException(status_code=403, detail="No active reservation for this room. Control denied.")


@router.post("/rooms/{room_id}/override", responses={403: {"model": ErrorDetail}, 503: {"model": ErrorDetail}})
async def override_room(
    room_id: str,
    payload: OverrideIn,
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Manual override of the room's fan or heater from the dashboard (RF-26).

    Publishes a retained command to ccs/<room>/command; the node applies it within one
    cycle (RF-08) and reports `manual` in its telemetry. A CO alarm on the node keeps
    priority over any manual order (RF-24).
    """
    if payload.mode != "auto" and payload.mode not in OVERRIDE_ACTIONS:
        raise HTTPException(status_code=422, detail="mode must be auto, fan, heater or off.")
    if not 1 <= payload.minutes <= 240:
        raise HTTPException(status_code=422, detail="minutes must be between 1 and 240.")
    if not 20 <= payload.level <= 100:
        raise HTTPException(status_code=422, detail="level must be between 20 and 100.")
    await run_in_threadpool(_require_room_control, db_sql, current_user, room_id)

    from app.config import MQTT_ENABLED
    from app.services.mqtt_ingest import publish_command
    if not MQTT_ENABLED:
        raise HTTPException(status_code=503, detail="MQTT is disabled on this server.")
    command = override_command(payload.mode, payload.minutes, current_user.username, payload.level)
    try:
        await publish_command(room_id, command)
    except Exception as exc:  # broker down or unreachable
        logger.error("Could not publish override for %s: %s", room_id, exc)
        raise HTTPException(status_code=503, detail="MQTT broker unavailable.") from exc
    logger.info("Override %s for room %s by '%s'", command, room_id, current_user.username)
    # The node only reports the time left; keep the order so the dashboard knows its length.
    order = {
        "_id": room_id,
        "mode": "fan" if payload.mode == "on" else payload.mode,
        "minutes": None if payload.mode == "auto" else payload.minutes,
        "level": payload.level if command.get("accion") == "COOLING" else None,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "by": current_user.username,
    }
    try:
        await db["manual_orders"].replace_one({"_id": room_id}, order, upsert=True)
    except PyMongoError as exc:   # the order was sent; only its record is missing
        logger.error("Could not record manual order for %s: %s", room_id, exc)
    return {"room_id": room_id, "command": command}


@router.get("/rooms/{room_id}/override")
async def last_override(
    room_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Last manual order sent to the room: mode, minutes, when (naive UTC) and by whom."""
    try:
        doc = await db["manual_orders"].find_one({"_id": room_id})
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    if not doc:
        return {"room_id": room_id, "mode": "auto", "minutes": None, "started_at": None, "by": None}
    doc["room_id"] = doc.pop("_id")
    return doc


class ControllerIn(BaseModel):
    mode: str            # rules | learning


@router.get("/rooms/{room_id}/controller")
async def room_controller(
    room_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Who drives the room (rules or learning), who chose it and when (naive UTC),
    plus the last order the learning controller sent, if any."""
    from app.services.room_controller import ORDER_BY, get_controller
    try:
        ctl = await get_controller(db, room_id)
        order = await db["manual_orders"].find_one({"_id": room_id})
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    last = None
    if order and order.get("by") == ORDER_BY:
        last = {k: order.get(k) for k in ("mode", "level", "minutes", "started_at")}
    return {"room_id": room_id, **ctl, "last_order": last}


class NodeSettingsIn(BaseModel):
    umbral_ocupacion: int   # MQ-135 ADC (0-4095) from which the node considers the room occupied


@router.put("/rooms/{room_id}/node-settings", responses={403: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}})
async def set_node_settings(room_id: str, payload: NodeSettingsIn, current_user: User = Depends(get_current_user)):
    """Change a node's occupancy threshold without reflashing it (admins only).

    Published retained to ccs/<room>/settings; the node applies it, stores it in flash and reports
    it back as `umbral_ocupacion` in its telemetry, which is how the dashboard confirms it.
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can change node settings.")
    if not 1 <= payload.umbral_ocupacion <= 4095:
        raise HTTPException(status_code=422, detail="umbral_ocupacion must be between 1 and 4095.")
    from app.config import MQTT_ENABLED
    from app.services.mqtt_ingest import publish_settings
    if not MQTT_ENABLED:
        raise HTTPException(status_code=503, detail="MQTT is disabled on this server.")
    try:
        await publish_settings(room_id, {"umbral_ocupacion": payload.umbral_ocupacion})
    except Exception as exc:  # broker down or unreachable
        logger.error("Could not publish settings for %s: %s", room_id, exc)
        raise HTTPException(status_code=503, detail="MQTT broker unavailable.") from exc
    logger.info("Node settings for %s set by '%s': umbral_ocupacion=%d", room_id, current_user.username, payload.umbral_ocupacion)
    return {"room_id": room_id, "umbral_ocupacion": payload.umbral_ocupacion}


@router.put("/rooms/{room_id}/controller", responses={403: {"model": ErrorDetail}, 503: {"model": ErrorDetail}})
async def set_room_controller(
    room_id: str,
    payload: ControllerIn,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Switch between the node's rules and the learning controller (admins only)."""
    from app.config import MQTT_ENABLED
    from app.services.room_controller import CONTROLLERS, get_controller, set_controller
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can choose who controls the room.")
    if payload.mode not in CONTROLLERS:
        raise HTTPException(status_code=422, detail="mode must be rules or learning.")
    if not MQTT_ENABLED:
        raise HTTPException(status_code=503, detail="MQTT is disabled on this server.")
    try:
        ctl = await set_controller(db, room_id, payload.mode, current_user.username)
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    except Exception as exc:   # broker down: the choice is saved, the loop retries the order
        logger.error("Controller switch for %s could not reach the node: %s", room_id, exc)
        ctl = await get_controller(db, room_id)
    logger.info("Controller of room %s set to %s by '%s'", room_id, payload.mode, current_user.username)
    return {"room_id": room_id, **ctl}


# ANTES: @router.put("/{sensor_id}/control")
# AHORA: se agregan response_model=SensorControlOut y responses={401,403,404}
# — es el endpoint de la Action 12 (Encender/apagar sensor) de la guía.
@router.put(
    "/{sensor_id}/control",
    response_model=SensorControlOut,
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
def control_sensor(
    sensor_id: str,
    payload: SensorControlIn,
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    # guests never allowed
    if current_user.role == "guest":
        raise HTTPException(status_code=403, detail="Guests cannot control sensors.")

    device = db_sql.query(SensorDevice).filter(SensorDevice.id == sensor_id).first()
    if not device:
        raise HTTPException(status_code=404, detail=f"Sensor '{sensor_id}' not registered.")

    # collaborators need an active reservation on this room
    if current_user.role == "collaborator":
        # Reservations are stored in LOCAL wall-clock time (same convention as
        # schedules) — see app.core.timeutils.
        now = local_now()
        active = db_sql.query(Reservation).filter(
            Reservation.user_id == current_user.id,
            Reservation.room_id == device.room_id,
            Reservation.start_time <= now,
            Reservation.end_time >= now,
        ).first()
        if not active:
            raise HTTPException(
                status_code=403,
                detail="No active reservation for this room. Control denied.",
            )

    device.is_active = payload.is_active
    device.control_enabled = payload.control_enabled
    try:
        db_sql.commit()
        db_sql.refresh(device)
    except SQLAlchemyError as exc:
        db_sql.rollback()
        logger.error("DB error on sensor control: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    logger.info(
        "Sensor '%s' updated by '%s' (role=%s): is_active=%s control_enabled=%s",
        sensor_id, current_user.username, current_user.role,
        device.is_active, device.control_enabled,
    )
    return {
        "sensor_id": device.id,
        "room_id": device.room_id,
        "is_active": device.is_active,
        "control_enabled": device.control_enabled,
    }
