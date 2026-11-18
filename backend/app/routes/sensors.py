import logging
import time
from datetime import datetime, timezone, timedelta
# CAMBIO: antes era "from typing import Optional" — se agregó "List" porque
# ahora usamos response_model=List[...] en list_readings.
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.errors import PyMongoError
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
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
SIM_PURGE_EVERY_S = 30
_last_sim_purge = 0.0
EVENTS_TTL_S = 30
_events_cache: dict = {}


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
    # None when the reading came without a CO sensor (HTTP readings); MQTT stores 0.0.
    co_ppm: Optional[float] = None
    ina_voltage_v: Optional[float] = None
    ina_current_ma: Optional[float] = None
    ina_power_mw: Optional[float] = None
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
# fields=chart: only what the room's live charts read over the history window
# (values, timestamps, and the node_state keys used by stateKey, the MQ-135 chart,
# node detection and the simulation-speed chip). The newest reading is fetched full.
CHART_PROJECTION = {
    "_id": 0,
    "timestamp": 1,
    "sensor_id": 1,
    "temperature": 1,
    "humidity": 1,
    "co2_ppm": 1,
    "co_ppm": 1,
    "is_simulated": 1,
    "node_state.accion": 1,
    "node_state.alarma_co": 1,
    "node_state.manual": 1,
    "node_state.co2_proxy": 1,
    "node_state.uptime_s": 1,
}


@router.get("/", response_model=List[SensorReadingOut])
async def list_readings(
    room_id: Optional[str] = None,
    sensor_id: Optional[str] = None,
    limit: int = Query(40, ge=1, le=2000),
    since: Optional[str] = Query(None, description="Only readings newer than this stored timestamp (incremental polling)"),
    fields: str = Query("full", pattern="^(full|chart)$", description="chart: light documents for the live charts"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
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
    projection = CHART_PROJECTION if fields == "chart" else None
    cursor = db["sensor_readings"].find(mongo_filter, projection).sort("timestamp", -1).limit(limit)
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

    # Raw telemetry is one reading per second (~260k for 24 h and three nodes). The window is
    # read from the per-minute rollups instead (CO peak and mean CO₂ of each minute), plus the
    # few raw readings after a sensor's last rolled-up minute. A sensor without rollups falls
    # back to its raw readings. The result is also shared for EVENTS_TTL_S.
    cached = _events_cache.get((hours, limit))
    if cached and time.monotonic() - cached[0] < EVENTS_TTL_S:
        return cached[1]
    devices = await run_in_threadpool(lambda: db_sql.query(SensorDevice.id, SensorDevice.room_id).all())
    room_of = {sid: rid for sid, rid in devices if rid}
    if not room_of:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    since_dt = now - timedelta(hours=hours)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
    raw_projection = {"_id": 0, "sensor_id": 1, "timestamp": 1, "co2_ppm": 1, "co_ppm": 1}
    by_sensor: dict[str, list[dict]] = {sid: [] for sid in room_of}
    try:
        minutes = await db["reading_minutes"].find(
            {"sensor_id": {"$in": list(room_of)}, "m": {"$gte": since_dt}, "n": {"$gt": 0}},
            {"_id": 0, "sensor_id": 1, "m": 1, "co2_s": 1, "co2_n": 1, "co_max": 1},
        ).sort([("sensor_id", 1), ("m", 1)]).to_list(length=None)
        last_min: dict[str, datetime] = {}
        for mdoc in minutes:
            sid, m = mdoc["sensor_id"], mdoc["m"]
            by_sensor[sid].append({
                "timestamp": m.strftime("%Y-%m-%dT%H:%M:%S"),
                "co_ppm": mdoc.get("co_max"),
                "co2_ppm": mdoc["co2_s"] / mdoc["co2_n"] if mdoc.get("co2_n") else None,
            })
            last_min[sid] = m
        for sid in room_of:
            tail_from = (last_min[sid] + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S") if sid in last_min else since
            by_sensor[sid].extend(await db["sensor_readings"].find(
                {"sensor_id": sid, "timestamp": {"$gte": tail_from}}, raw_projection,
            ).sort("timestamp", 1).to_list(length=None))
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    events = [e for sid, rows in by_sensor.items() for e in detect_events(room_of[sid], rows, now)]
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    _events_cache[(hours, limit)] = (time.monotonic(), events[:limit])
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
    whole: bool = Query(False, alias="all"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """What the room's node measured and did on one local day (latest day with data if
    `date` is missing): 5-min series, classes, summary, events and state changes.
    `format=csv` downloads every reading of that day; with `all=true`, every reading
    the room has (from its first to its last local day)."""
    from datetime import date as date_cls
    from fastapi.responses import StreamingResponse
    from app.services.room_history import (
        CSV_HEADER, FMT, OFFSET, PROJECTION, ClassWalker, InaEnergy, csv_line, day_bounds, equipment_for,
    )

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
        whole = whole and format == "csv"
        if whole:
            data_start = datetime.fromisoformat(first[0]["timestamp"][:19])
            start, end = day_bounds((data_start + OFFSET).date())[0], day_bounds((data_end + OFFSET).date())[1]
        match = {**base, "timestamp": {"$gte": start.strftime(FMT), "$lt": end.strftime(FMT)}}
        meta = await db["strategy_simulation_meta"].find_one({"_id": room_id})
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    from app.services.strategy_series import class_intervals, room_timetable
    timetable = class_intervals(*(await run_in_threadpool(room_timetable, db_sql, room_id)), start, end)

    if format == "csv":
        label = f"{(start + OFFSET).date().isoformat()}_{(end + OFFSET - timedelta(days=1)).date().isoformat()}" if whole else anchor.isoformat()

        async def rows():
            # Streamed in batches straight from the cursor: the download starts at once and
            # months of 1 Hz readings never sit in memory together.
            yield CSV_HEADER + "\n"
            energy, classes, batch = InaEnergy(), ClassWalker(timetable), []
            try:
                async for d in db["sensor_readings"].find(match, PROJECTION).sort("timestamp", 1).batch_size(5000):
                    batch.append(csv_line(d, energy.add(d), classes.at(d["timestamp"])))
                    if len(batch) >= 5000:
                        yield "\n".join(batch) + "\n"
                        batch = []
            except PyMongoError as exc:
                logger.error("History CSV of %s interrupted: %s", room_id, exc)
            if batch:
                yield "\n".join(batch) + "\n"

        return StreamingResponse(rows(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="historial_{room_id}_{label}.csv"'})

    equipment = equipment_for(meta, start, end)
    if not (meta and meta.get("start") and meta["start"] < end and meta["end"] >= start):
        configured_room = await run_in_threadpool(lambda: db_sql.query(Room).filter(Room.id == room_id).first())
        if configured_room:
            equipment["fan_max_w"] = configured_room.fan_power_w
    simulated = bool(meta and meta.get("start") and meta["start"] < end and meta["end"] >= start)
    try:
        result = await _history_day(db, room_id, sensors, anchor, start, end, closed=anchor < (data_end + OFFSET).date(),
                                    equipment=equipment, timetable=timetable, simulated=simulated)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
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


async def _history_day(db, room_id: str, sensors: list[str], anchor, start: datetime, end: datetime, *,
                       closed: bool, equipment: dict, timetable: list, simulated: bool) -> dict:
    """Series, periods and summary of one local day. Finished days are served from
    `room_history_days` while their inputs and raw reading count stay the same."""
    from app.services import history_cache
    from app.services.reading_rollups import load_minutes, summarize_minutes
    from app.services.room_history import FMT, PROJECTION, summarize_day, ina_energy_series

    key = raw_n = None
    if closed:
        key = history_cache.inputs_key(sensors, equipment, timetable, simulated)
        raw_n = await db["sensor_readings"].count_documents(
            {"sensor_id": {"$in": sensors}, "timestamp": {"$gte": start.strftime(FMT), "$lt": end.strftime(FMT)}})
        cached = await history_cache.get(db, room_id, anchor, key, raw_n)
        if cached is not None:
            return cached

    # Per-minute rollups (~1 440 small documents a day) instead of ~86 400 readings.
    by_sensor = await load_minutes(db, sensors, start, end, verify=True)
    if len(by_sensor) > 1:
        # Several nodes in one room interleave their readings, which per-sensor rollups
        # cannot replay: keep the raw path.
        docs = await db["sensor_readings"].find(
            {"sensor_id": {"$in": sensors}, "node_state.accion": {"$exists": True},
             "timestamp": {"$gte": start.strftime(FMT), "$lt": end.strftime(FMT)}}, PROJECTION,
        ).sort("timestamp", 1).to_list(length=None)
        result = summarize_day(docs, equipment["fan_max_w"], equipment["heater_w"], timetable)
        result["summary"]["ina_energy_wh"] = next((v for v in reversed(ina_energy_series(docs)) if v is not None), None)
    else:
        minutes = next(iter(by_sensor.values()), [])
        result, energy = await run_in_threadpool(summarize_minutes, minutes, equipment["fan_max_w"], equipment["heater_w"], timetable)
        result["summary"]["ina_energy_wh"] = energy
    if simulated:
        # Simulated days know how many people came: same class intervals as "Aprendizaje".
        from app.services.strategy_series import _intervals
        sim = await db["strategy_simulation"].aggregate([
            {"$match": {"room_id": room_id, "strategy": "rules", "ts": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": {"$dateTrunc": {"date": "$ts", "unit": "minute"}},
                        "occ": {"$max": "$occupied"}, "people": {"$max": "$people"}}},
        ]).to_list(length=None)
        result["classes"] = _intervals(sim)
    if closed:
        await history_cache.put(db, room_id, anchor, key, raw_n, result)
    return result


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

    `auto` uses the node's live readings when the room has a real node (rules + the
    predictive controller's shadow decisions), and otherwise the room's strategy
    simulation if one exists (both strategies with their own temperature).
    """
    from datetime import date as date_cls
    from app.services.strategy_series import live_series, room_timetable, simulation_series

    anchor = date_cls.fromisoformat(day) if day else None

    try:
        meta = await db["strategy_simulation_meta"].find_one({"_id": room_id})
        sensors = await run_in_threadpool(
            lambda: [d.id for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
        )
        real_node = bool(sensors) and await db["sensor_readings"].find_one(
            {"sensor_id": {"$in": sensors}, "is_simulated": False, "node_state.accion": {"$exists": True}}, {"_id": 1})
        if meta and (source == "simulation" or (source == "auto" and not real_node)):
            return await simulation_series(db, room_id, meta, range_key, anchor)
        timetable = await run_in_threadpool(room_timetable, db_sql, room_id)
        configured_room = await run_in_threadpool(lambda: db_sql.query(Room).filter(Room.id == room_id).first())
        data = await live_series(db, sensors, range_key, anchor, room_id, timetable,
                                 configured_room.fan_power_w if configured_room else 2.4) if sensors else None
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

    room_equipment = await run_in_threadpool(lambda: db_sql.query(Room).filter(Room.id == room_id).first())
    return {"room_id": room_id, "hours": hours, **summarize(
        docs,
        fan_power_w=room_equipment.fan_power_w if room_equipment else 2.4,
        fan_type=room_equipment.fan_type if room_equipment else "pwm_12v",
    )}


class DemoReadingIn(BaseModel):
    room_id: str
    temperature: Optional[float]            # None: the DHT does not answer
    humidity: Optional[float] = 55.0
    co2_ppm: float
    co_ppm: float = 0.0
    manual: bool = False                    # a dashboard order forces the fan on
    hold_s: int = Field(0, ge=0, le=60)     # change asked before 60 s: keep the previous state
    hold_temperature: Optional[float] = None


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
    node_state = emulate(body.temperature, body.co2_ppm, body.co_ppm, manual=body.manual,
                         hold_s=body.hold_s, hold_temperature=body.hold_temperature)
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

    # Purge simulated readings older than 15 seconds, so a stopped simulator leaves the DB
    # clean. Every open dashboard polls this endpoint every 5 s: the purge runs at most once
    # per SIM_PURGE_EVERY_S and only looks at the last hour, because an unbounded delete scans
    # the whole timestamp index and piled-up purges starved Mongo for every other view.
    global _last_sim_purge
    fifteen_sec_ago = (now - timedelta(seconds=15)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    if time.monotonic() - _last_sim_purge >= SIM_PURGE_EVERY_S:
        _last_sim_purge = time.monotonic()
        hour_ago = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")
        purge = await db["sensor_readings"].delete_many(
            {**SIM_TOOL_READINGS, "timestamp": {"$gte": hour_ago, "$lt": fifteen_sec_ago}}
        )
        if purge.deleted_count:
            logger.info("Purged %d old simulated readings", purge.deleted_count)

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


def override_command(mode: str, minutes: int, by: str, level: int = 100,
                     authority: str = "manual", now: datetime | None = None) -> dict:
    """MQTT command for a remote order; raises ValueError for an unknown mode.

    `modo` stays "manual" so nodes flashed before these fields keep working. Newer
    firmware reads `autoridad` (manual: a person; predictivo: the learning controller,
    cancelled by the node as soon as it loses the server) and `expira_utc` (epoch s):
    a retained order replayed after a reboot is rejected once expired, instead of
    restarting its full duration. `id` is echoed back in telemetry as `comando_id`.
    `now` is naive UTC (the backend's convention) or aware.
    """
    if mode == "auto":
        return {"modo": "auto"}
    if mode not in OVERRIDE_ACTIONS:
        raise ValueError(mode)
    import uuid
    now = now or datetime.now(timezone.utc)
    issued = int((now if now.tzinfo else now.replace(tzinfo=timezone.utc)).timestamp())
    command = {"modo": "manual", "accion": OVERRIDE_ACTIONS[mode], "duracion_s": minutes * 60, "por": by,
               "id": uuid.uuid4().hex[:12], "autoridad": authority,
               "emitida_utc": issued, "expira_utc": issued + minutes * 60}
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


@router.get("/rooms/{room_id}/smart-plug")
async def smart_plug_status(
    room_id: str,
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    from app.services.smart_plug import room_status

    return await room_status(db_sql, room_id)


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
    room_fan_type = await run_in_threadpool(
        lambda: getattr(db_sql.query(Room).filter(Room.id == room_id).first(), "fan_type", "pwm_12v")
    )

    from app.config import MQTT_ENABLED
    from app.services.mqtt_ingest import publish_command
    if not MQTT_ENABLED:
        raise HTTPException(status_code=503, detail="MQTT is disabled on this server.")
    fan_level = 100 if room_fan_type == "smart_plug" and payload.mode in ("fan", "on") else payload.level
    command = override_command(payload.mode, payload.minutes, current_user.username, fan_level)
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
        "level": fan_level if command.get("accion") == "COOLING" else None,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "by": current_user.username,
        "command_id": command.get("id"),
        "authority": command.get("autoridad"),
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
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Who drives the room (rules or learning), who chose it and when (naive UTC),
    plus the last order the learning controller sent, if any.

    Also whether learning may drive now (`authorized`, with the forecast `gate`), the
    `fallback` reason when learning was chosen but the rules are acting, and what the
    node reported about the last order (`ack`: applied / rejected / expired / pending).
    """
    from app.services.room_controller import ORDER_BY, authorization, fallback_reason, get_controller
    try:
        ctl = await get_controller(db, room_id)
        order = await db["manual_orders"].find_one({"_id": room_id})
        auth = await authorization(db, room_id)
        last = None
        if order and order.get("by") == ORDER_BY:
            last = {k: order.get(k) for k in ("mode", "level", "minutes", "started_at", "command_id")}
            last["ack"] = await _order_ack(db, db_sql, room_id, order)
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    fallback = None
    if ctl["mode"] == "learning":
        fallback = auth["reason"] if not auth["authorized"] else fallback_reason(room_id)
    return {"room_id": room_id, **ctl, "last_order": last,
            "authorized": auth["authorized"], "authorization_reason": auth["reason"],
            "gate": auth.get("gate"), "fallback": fallback}


async def _order_ack(db, db_sql: Session, room_id: str, order: dict) -> str | None:
    """What the node said about an order, from the `comando_id` / `comando_estado` it echoes.

    None when the order has no id (sent before ids existed) or it is a hand-back to the rules.
    """
    if not order.get("command_id"):
        return None
    sensors = await run_in_threadpool(
        lambda: [d.id for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
    )
    latest = await db["sensor_readings"].find(
        {"sensor_id": {"$in": sensors}, "node_state.comando_id": order["command_id"]},
        {"node_state.comando_estado": 1},
    ).sort("timestamp", -1).limit(1).to_list(length=1)
    if not latest:
        return "pending"      # not echoed yet, or firmware without acknowledgements
    state = (latest[0].get("node_state") or {}).get("comando_estado")
    return {"aplicado": "applied", "rechazado": "rejected", "vencido": "expired"}.get(state, "pending")


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
    """Switch between the node's rules and the learning controller (admins only).

    Learning is refused (409) until the forecast passes its gate and the room's device
    has control enabled: an unvalidated forecaster never takes the actuator.
    """
    from app.config import MQTT_ENABLED
    from app.services.room_controller import CONTROLLERS, authorization, get_controller, set_controller
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can choose who controls the room.")
    if payload.mode not in CONTROLLERS:
        raise HTTPException(status_code=422, detail="mode must be rules or learning.")
    if not MQTT_ENABLED:
        raise HTTPException(status_code=503, detail="MQTT is disabled on this server.")
    if payload.mode == "learning":
        try:
            auth = await authorization(db, room_id)
        except PyMongoError as exc:
            raise HTTPException(status_code=503, detail="Database unavailable") from exc
        if not auth["authorized"]:
            raise HTTPException(status_code=409, detail=auth["reason"])
    try:
        ctl = await set_controller(db, room_id, payload.mode, current_user.username)
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    except Exception as exc:   # broker down: the choice is saved, the loop retries the order
        logger.error("Controller switch for %s could not reach the node: %s", room_id, exc)
        ctl = await get_controller(db, room_id)
    logger.info("Controller of room %s set to %s by '%s'", room_id, payload.mode, current_user.username)
    return {"room_id": room_id, **ctl}


def _hand_back_to_rules(room_id: str, by: str) -> None:
    """Control was disabled: cancel a running learning order now, not when it expires.

    control_sensor is sync (FastAPI runs it in a worker thread), so the async cancel
    goes back to the event loop through anyio. If the broker is down the order still
    expires on the node by itself.
    """
    import anyio.from_thread
    from app.config import MQTT_ENABLED
    from app.services.room_controller import _LAST_ACTION, cancel_learning_order

    last = _LAST_ACTION.get(room_id)
    if last:
        last[1]["control_enabled"] = False     # the loop must not renew it before the next reading
    if not MQTT_ENABLED:
        return
    try:
        anyio.from_thread.run(cancel_learning_order, get_db(), room_id, by)
    except Exception as exc:   # broker or Mongo down: the order expires on the node
        logger.error("Could not cancel the learning order of %s: %s", room_id, exc)


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
    if device.room_id and not (device.is_active and device.control_enabled):
        _hand_back_to_rules(device.room_id, current_user.username)
    return {
        "sensor_id": device.id,
        "room_id": device.room_id,
        "is_active": device.is_active,
        "control_enabled": device.control_enabled,
    }


# ============================================================================
# Predictive layer made visible: what the forecaster says now, how good it has been,
# what it recommends, and who is allowed to act on it. Read-only: nothing here sends
# an order. Learning orders go through room_controller only.
# ============================================================================

FORECAST_STALE_MIN = 15      # decisions every 5 min: older than this, the forecast is not current


@router.get("/rooms/{room_id}/forecast")
async def room_forecast(
    room_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """The room's latest measured temperature next to the latest forecast, never mixed.

    `observed` is a measurement (sensor, time, origin); `forecast` is a prediction (model,
    when it was made, horizon); `baseline` is persistence (the temperature the forecast
    started from). `quality` is the gate over the last 24 h; `recommendation` is a rule
    over those facts; `mode` says who acts: local rules, observation (the model only
    predicts and recommends), supervised predictive control, or fallback to the rules.
    """
    from app.services.data_service import node_in_use, occupancy_forecast
    from app.services.forecast_controller import HORIZON_MIN, READY_MINUTES
    from app.services.forecast_quality import recommend
    from app.services.predictive_service import model_status
    from app.services.room_controller import authorization, fallback_reason, get_controller

    devices = await run_in_threadpool(
        lambda: [(d.id, d.is_active, d.control_enabled)
                 for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
    )
    sensors = [d[0] for d in devices]
    control_enabled = any(active and enabled for _, active, enabled in devices)
    now = datetime.utcnow()
    try:
        latest = await db["sensor_readings"].find(
            {"sensor_id": {"$in": sensors}, "node_state.accion": {"$exists": True}},
            {"_id": 0, "sensor_id": 1, "timestamp": 1, "temperature": 1, "is_simulated": 1, "data_origin": 1,
             "camera_occupancy": 1, "node_state": 1},
        ).sort("timestamp", -1).limit(1).to_list(length=1)
        last_fc = await db["forecast_log"].find({"room_id": room_id}, {"_id": 0}).sort("ts", -1).limit(1).to_list(length=1)
        state = await db["forecast_state"].find_one({"_id": room_id}, {"history": 1})
        ctl = await get_controller(db, room_id)
        auth = await authorization(db, room_id, now)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    observed = None
    ns: dict = {}
    if latest:
        d = latest[0]
        ns = d.get("node_state") or {}
        observed = {"temp_c": d.get("temperature"), "at": d["timestamp"][:19], "sensor_id": d["sensor_id"],
                    "data_origin": d.get("data_origin") or ("simulated" if d.get("is_simulated") else "unknown")}

    forecast = None
    if last_fc:
        f = last_fc[0]
        age_min = (now - f["ts"]).total_seconds() / 60
        forecast = {"model": f.get("model"), "horizon_min": HORIZON_MIN, "generated_at": f["ts"].strftime("%Y-%m-%dT%H:%M:%S"),
                    "target_at": (f["ts"] + timedelta(minutes=HORIZON_MIN)).strftime("%Y-%m-%dT%H:%M:%S"),
                    "median_c": f.get("forecast_30"), "p90_peak_c": f.get("forecast_30_p90"),
                    "would_fan_pct": f.get("fan_pct"), "backfilled": bool(f.get("backfilled")),
                    "data_origin": f.get("data_origin", "unknown"), "current": age_min <= FORECAST_STALE_MIN}
    minutes_seen = len((state or {}).get("history") or [])
    ready = minutes_seen >= READY_MINUTES
    baseline = None
    if forecast and last_fc[0].get("temp_now") is not None:
        baseline = {"model": "persistencia", "value_c": last_fc[0]["temp_now"],
                    "note": "La temperatura se mantiene como estaba al pronosticar."}

    timetable = await run_in_threadpool(occupancy_forecast, db_sql, room_id, now, HORIZON_MIN)
    in_use = bool(node_in_use(ns, (latest[0] if latest else {}).get("camera_occupancy"))) or any(c for c, _ in timetable)
    current = bool(forecast and forecast["current"])
    gate = auth.get("gate") or {}
    recommendation = recommend(
        ready=ready and current, temp_c=observed["temp_c"] if observed else None,
        forecast_c=forecast["median_c"] if current else None, p90_c=forecast["p90_peak_c"] if current else None,
        in_use=in_use, co_alarm=bool(ns.get("alarma_co")), validated=bool(gate.get("validated")),
        sensor_ok=bool(observed and observed["temp_c"] is not None),
    )

    if ctl["mode"] == "learning":
        mode = "predictive" if auth["authorized"] and not fallback_reason(room_id) else "fallback"
    else:
        mode = "observation" if current else "local"
    return {
        "room_id": room_id,
        "observed": observed,
        "forecast": forecast,
        "baseline": baseline,
        "data": {"minutes_seen": minutes_seen, "ready_minutes": READY_MINUTES, "ready": ready},
        "quality": auth.get("gate"),
        "recommendation": recommendation,
        "mode": {"value": mode, "controller": ctl["mode"], "control_enabled": control_enabled,
                 "authorized": auth["authorized"],
                 "reason": None if mode == "predictive" else (fallback_reason(room_id) if mode == "fallback" and auth["authorized"] else auth["reason"])},
        "regressor": model_status(),
    }


# Columns of the training export, in order. Labels (future temperatures) are NOT here:
# they are built offline from these rows (ml/pipeline/thermal_dataset.py), so no row
# ever carries information from its own future.
TRAINING_COLUMNS = [
    "timestamp_server_utc", "room_id", "sensor_id", "data_origin", "is_simulated",
    "temperature", "humidity", "co2_ppm", "co_ppm", "co2_proxy_adc", "camera_occupancy",
    "ocupado_nodo", "accion", "autoridad", "manual", "fan_applied_pct", "fan_target_pct", "fan_rpm",
    "heater", "alarma_co", "bloqueo_s", "comando_id",
    "ina_voltage_v", "ina_current_ma", "ina_power_mw",
    "ai_model", "ai_target", "expected_occupancy", "actual_occupancy",
    "forecast_model", "forecast_30", "forecast_30_p90", "forecast_fan_pct",
]


def _pwm_pct(value) -> float | None:
    """Firmware PWM duty (0-255) as %."""
    return None if value is None else round(value * 100 / 255, 1)


def _training_row(d: dict, room_id: str) -> list:
    ns, ca, la = d.get("node_state") or {}, d.get("cognitive_action") or {}, d.get("learning_action") or {}
    authority = ns.get("autoridad") or (("manual" if ns.get("manual") else "local") if ns else None)
    applied = _pwm_pct(ns["ventilador_pwm"]) if ns.get("ventilador_pwm") is not None else ns.get("ventilador_pct")
    return [
        d["timestamp"][:19], room_id, d.get("sensor_id"),
        d.get("data_origin") or ("simulated" if d.get("is_simulated") else "unknown"), int(bool(d.get("is_simulated"))),
        d.get("temperature"), d.get("humidity"), d.get("co2_ppm"), d.get("co_ppm"), ns.get("co2_proxy"), d.get("camera_occupancy"),
        ns.get("ocupado"), ns.get("accion"), authority, ns.get("manual"),
        applied, _pwm_pct(ns.get("ventilador_pwm_objetivo")), ns.get("ventilador_rpm"),
        ns.get("calefactor"), ns.get("alarma_co"), ns.get("bloqueo_s"), ns.get("comando_id"),
        d.get("ina_voltage_v"), d.get("ina_current_ma"), d.get("ina_power_mw"),
        ca.get("model"), ca.get("target"), ca.get("expected_occupancy"), ca.get("actual_occupancy"),
        la.get("model"), la.get("forecast_30"), la.get("forecast_30_p90"), la.get("fan_pct"),
    ]


def _median_step_s(rows: list[list]) -> float | None:
    ts = [datetime.fromisoformat(r[0]) for r in rows]
    steps = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
    return steps[len(steps) // 2] if steps else None


def training_export_body(docs: list[dict], room_id: str) -> tuple[str, list[list]]:
    """CSV text and rows for exported documents (pure: used by the route and the tests)."""
    import csv
    import io

    rows = [_training_row(d, room_id) for d in docs]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(TRAINING_COLUMNS)
    writer.writerows(["" if v is None else v for v in r] for r in rows)
    return buf.getvalue(), rows


@router.get("/training-export")
async def training_export(
    room_id: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$"),
    origin: str = Query("all", pattern="^(all|physical|simulated|unknown)$"),
    format: str = Query("csv", pattern="^(csv|json)$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    """Readings of one room over a range (naive UTC, `to` exclusive) for offline training.

    Read-only: the stored documents are exported as they are, with their provenance
    (`data_origin`: physical, simulated or unknown, the last for documents older than
    the field). `format=json` returns `{manifest, columns, rows}`; the CSV carries the
    same manifest in the `X-Export-Manifest` header. At most 31 days per request.
    """
    import hashlib
    import json
    from fastapi.responses import Response

    if current_user.role == "guest":
        raise HTTPException(status_code=403, detail="Guests cannot export training data.")
    start, end = datetime.fromisoformat(from_), datetime.fromisoformat(to)
    if end <= start or end - start > timedelta(days=31):
        raise HTTPException(status_code=422, detail="Range must be positive and at most 31 days.")
    sensors = await run_in_threadpool(
        lambda: [d.id for d in db_sql.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
    )
    match: dict = {"sensor_id": {"$in": sensors},
                   "timestamp": {"$gte": start.strftime("%Y-%m-%dT%H:%M:%S"), "$lt": end.strftime("%Y-%m-%dT%H:%M:%S")}}
    if origin == "simulated":
        match["is_simulated"] = True
    elif origin == "physical":
        match["data_origin"] = "physical"
    elif origin == "unknown":
        match["is_simulated"] = {"$ne": True}
        match["data_origin"] = {"$in": [None, "unknown"]}
    try:
        docs = await db["sensor_readings"].find(match, {"_id": 0}).sort("timestamp", 1).to_list(length=None)
    except PyMongoError as exc:
        logger.error("MongoDB unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable") from exc

    body, rows = training_export_body(docs, room_id)
    origins: dict[str, int] = {}
    for r in rows:
        origins[r[3]] = origins.get(r[3], 0) + 1
    temps = [r[5] for r in rows]
    manifest = {
        "room_id": room_id, "from": start.strftime("%Y-%m-%dT%H:%M:%S"), "to": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "origin_filter": origin, "rows": len(rows), "origins": origins, "sensors": sensors,
        "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None,
        "missing_temperature_pct": round(100 * sum(t is None for t in temps) / len(temps), 1) if temps else None,
        "median_step_s": _median_step_s(rows),
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "labels": "none: build T+h labels offline (ml/pipeline/thermal_dataset.py)",
    }
    if format == "json":
        return {"manifest": manifest, "columns": TRAINING_COLUMNS, "rows": rows}
    return Response(body, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="entrenamiento_{room_id}_{start:%Y%m%d}_{end:%Y%m%d}.csv"',
        "X-Export-Manifest": json.dumps(manifest, ensure_ascii=True),
    })
