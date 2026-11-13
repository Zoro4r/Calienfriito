import logging
from datetime import datetime, timedelta
from starlette.concurrency import run_in_threadpool
from app.core.timeutils import to_local
from app.models.sensor import SensorReading
from app.services.forecast_controller import HORIZON_MIN, ForecastController
from app.services.predictive_service import calculate_cooling_demand
from app.services.room_profile import get_metadata
from app.services.strategy_comparison import compare_reading

logger = logging.getLogger(__name__)

# Feed-forward window: a schedule is considered active this many minutes BEFORE
# its start_time, so the cognitive engine can pre-cool ahead of arrival (the
# seed's warm-up rows carry the plan for the same window).
PRECOOL_LOOKAHEAD_MIN = 30


def detect_anomaly(reading: SensorReading) -> bool:
    return (
        (reading.temperature is not None and reading.temperature > 40)
        or (reading.humidity is not None and reading.humidity > 95)
        or reading.co_ppm > 50  # EPA alert threshold for CO
    )


def get_room_context(db_sql, sensor_id: str, reading_timestamp: datetime) -> dict | None:
    from app.models.admin import SensorDevice, Schedule, Reservation

    # Look up device — skip telemetry if inactive or control disabled
    device = (
        db_sql.query(SensorDevice)
        .filter(SensorDevice.id == sensor_id)
        .first()
    )

    if not device:
        logger.debug("sensor_id=%s not registered in sensor_devices.", sensor_id)
        return None

    if not device.is_active:
        logger.info("sensor_id=%s is_active=False — ignoring telemetry.", sensor_id)
        return {"_device_ignored": True}

    if not device.control_enabled:
        logger.info("sensor_id=%s control_enabled=False — no cognitive action.", sensor_id)
        return {"_control_disabled": True}

    room = device.room

    # Schedules are stored in LOCAL wall-clock time while the API stamps readings
    # with naive UTC — shift first (see app.core.timeutils), otherwise a 17-18h
    # Lima class (22-23h UTC) never matches its schedule.
    now = to_local(reading_timestamp)
    # Feed-forward: match a schedule that starts within the next PRECOOL window,
    # so pre-cooling can begin before the class does. (A lookahead that crosses
    # midnight misses — acceptable, no classes straddle midnight.)
    lookahead = now + timedelta(minutes=PRECOOL_LOOKAHEAD_MIN)
    # A one-off reservation takes precedence over the weekly schedule: it is the
    # most specific plan for this room and hour (same lookahead window).
    reservation = (
        db_sql.query(Reservation)
        .filter(
            Reservation.room_id == room.id,
            Reservation.start_time <= lookahead,
            Reservation.end_time >= now,
        )
        .order_by(Reservation.start_time)
        .first()
    )
    schedule = None if reservation else (
        db_sql.query(Schedule)
        .filter(
            Schedule.day_of_week == now.weekday(),
            Schedule.room_id == room.id,
            Schedule.start_time <= lookahead.time(),
            Schedule.end_time >= now.time(),
        )
        .first()
    )
    if reservation:
        expected_people, expected_source = reservation.expected_occupancy, "reservation"
    elif schedule:
        expected_people, expected_source = schedule.expected_people, "schedule"
    else:
        expected_people, expected_source = None, None
    return {
        "room_id": room.id,
        "room_name": room.name,
        "max_capacity": room.max_capacity,
        "target_temp": room.target_temp,
        "expected_people": expected_people,
        "expected_source": expected_source,
        "control_policy": getattr(room, "control_policy", "auto") or "auto",
        "device_id": device.id,
    }


def occupancy_forecast(db_sql, room_id: str, reading_timestamp: datetime, minutes: int) -> list[tuple[bool, float]]:
    """Timetable for the next `minutes`, one entry per minute: (class?, expected people).

    Built from the room's weekly schedules and reservations (both in LOCAL time); a
    reservation takes precedence over the schedule, as in get_room_context.
    """
    from app.models.admin import Reservation, Schedule

    start = to_local(reading_timestamp).replace(second=0, microsecond=0)
    end = start + timedelta(minutes=minutes)
    out: list[tuple[bool, float]] = [(False, 0.0)] * minutes
    days = {(start + timedelta(days=k)).weekday(): start.date() + timedelta(days=k) for k in range((end.date() - start.date()).days + 1)}
    for sch in db_sql.query(Schedule).filter(Schedule.room_id == room_id, Schedule.day_of_week.in_(list(days))).all():
        day = days[sch.day_of_week]
        s_dt, e_dt = datetime.combine(day, sch.start_time), datetime.combine(day, sch.end_time)
        for m in range(max(0, int((s_dt - start).total_seconds() // 60)), min(minutes, int((e_dt - start).total_seconds() // 60))):
            out[m] = (True, float(sch.expected_people))
    for res in db_sql.query(Reservation).filter(
        Reservation.room_id == room_id, Reservation.start_time < end, Reservation.end_time > start
    ).all():
        for m in range(max(0, int((res.start_time - start).total_seconds() // 60)), min(minutes, int((res.end_time - start).total_seconds() // 60))):
            out[m] = (True, float(res.expected_occupancy))
    return out


# One predictive controller per room, kept in memory and persisted in Mongo each minute.
_SHADOW: dict[str, ForecastController] = {}


async def shadow_learning_step(db, room_id: str, reading: SensorReading, db_sql=None) -> dict | None:
    """Run the predictive controller in shadow on a node reading.

    The node's rules act; the controller forecasts the temperature and returns what it
    would do (start the fan early or not). None when the reading is not a node decision.
    """
    ns = reading.node_state or {}
    if not ns.get("accion") or reading.temperature is None:
        return None
    ctrl = _SHADOW.get(room_id)
    if ctrl is None:
        state = await db["forecast_state"].find_one({"_id": room_id})
        ctrl = ForecastController.from_state(state) if state else ForecastController()
        _SHADOW[room_id] = ctrl
    ts = reading.timestamp.replace(tzinfo=None)
    ts_s = (ts - datetime(1970, 1, 1)).total_seconds()
    if ctrl.closes_minute(ts_s):
        # A minute closes: the controller may forecast and decide, so tell it whether the
        # room is in use (the node sees people, or a class is on or starts within the horizon).
        in_use = ns.get("accion") != "DISABLED"
        if not in_use and db_sql is not None:
            timetable = await run_in_threadpool(occupancy_forecast, db_sql, room_id, ts, HORIZON_MIN)
            in_use = any(c for c, _ in timetable)
        await run_in_threadpool(ctrl.step, ts_s, reading.temperature, in_use)   # Chronos runs here
        await db["forecast_state"].replace_one({"_id": room_id}, {"_id": room_id, **ctrl.to_state()}, upsert=True)
    else:
        ctrl.step(ts_s, reading.temperature)
    summary = ctrl.summary()
    from app.services.room_controller import remember_action
    remember_action(room_id, ts, summary)
    return summary


async def save_reading(db, reading: SensorReading, cognitive_action: dict | None = None,
                       learning_action: dict | None = None) -> str:
    doc = reading.model_dump()
    # Always store naive UTC ISO string so MongoDB string comparison in $gte/$lte queries
    # works consistently regardless of whether the client sent a 'Z' or '+00:00' timestamp.
    doc["timestamp"] = reading.timestamp.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.%f")
    if cognitive_action is not None:
        doc["cognitive_action"] = cognitive_action
    if learning_action is not None:
        doc["learning_action"] = learning_action
    # RF-30: record per reading whether the node and the AI agreed.
    doc["comparison"] = compare_reading(doc)
    logger.info(
        "[INGEST] sensor_id=%s is_simulated=%s co_ppm=%s ts=%s",
        reading.sensor_id, reading.is_simulated, reading.co_ppm, doc["timestamp"],
    )
    result = await db["sensor_readings"].insert_one(doc)
    return str(result.inserted_id)


async def process_reading(db, reading: SensorReading, db_sql=None) -> dict:
    logger.info("Ingest reading sensor_id=%s", reading.sensor_id)
    anomaly = detect_anomaly(reading)

    room_context = None
    cognitive_action = None

    if db_sql is not None:
        room_context = await run_in_threadpool(
            get_room_context, db_sql, reading.sensor_id, reading.timestamp
        )

    # Device inactive → skip telemetry entirely (don't even save to Mongo)
    if isinstance(room_context, dict) and room_context.get("_device_ignored"):
        logger.info("Dropping telemetry for inactive device sensor_id=%s.", reading.sensor_id)
        return {"sensor_id": reading.sensor_id, "skipped": True, "reason": "device_inactive"}

    # Control disabled → save reading but emit no cognitive action
    if isinstance(room_context, dict) and room_context.get("_control_disabled"):
        room_context = None   # don't include internal flag in response
        cognitive_action = {"ac_status": "DISABLED", "cooling_mode": None, "target": None, "model": "none"}

    # No temperature (the node's DHT22 failed): store the reading, but nothing can decide on it.
    if cognitive_action is None and reading.temperature is None:
        cognitive_action = {"ac_status": "DISABLED", "cooling_mode": None, "target": None, "model": "none",
                            "reason": "Sin temperatura: el DHT22 del nodo no envía lectura"}

    if cognitive_action is None:
        cognitive_action = await calculate_cooling_demand(
            reading.temperature, room_context, reading.timestamp, co2_ppm=reading.co2_ppm
        )

    learning_action = None
    room_id = room_context.get("room_id") if isinstance(room_context, dict) else None
    if room_id and reading.node_state and reading.temperature is not None:
        try:
            learning_action = await shadow_learning_step(db, room_id, reading, db_sql=db_sql)
        except Exception:   # the shadow controller must never block ingestion
            logger.exception("Learning shadow step failed for room %s", room_id)

    inserted_id = await save_reading(db, reading, cognitive_action=cognitive_action, learning_action=learning_action)
    logger.info(
        "Saved sensor_id=%s inserted_id=%s anomaly=%s room=%s ac=%s",
        reading.sensor_id, inserted_id, anomaly,
        room_context["room_name"] if room_context else "unknown",
        cognitive_action["ac_status"],
    )

    result = {
        "sensor_id": reading.sensor_id,
        "anomaly_detected": anomaly,
        "inserted_id": inserted_id,
        "timestamp": reading.timestamp,
        "cognitive_action": cognitive_action,
    }
    if room_context:
        result["room_context"] = room_context
    return result
