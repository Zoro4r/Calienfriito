"""Savings report: monthly fan hours and cost per room against the fan always on.

Rooms with a real node and classes use their measured temperature and timetable of the
last days; other rooms, the month-long strategy simulation (see services/profitability).
Each room reports the strategy it runs today, so switching a room to the predictive
controller shows up here. Access: admin only.
"""
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.database_sql import get_db_sql
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from app.dependencies import get_current_user
from app.models.admin import Reservation, Schedule, SensorDevice, User
from app.services import profitability as pf
from app.services.room_controller import get_controller

router = APIRouter()


@router.get("/roi")
async def get_roi(
    db: AsyncIOMotorDatabase = Depends(get_db),
    db_sql: Session = Depends(get_db_sql),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")

    rooms = []
    # Rooms with a real node and classes: measured temperature + timetable (last 7 days).
    for room_id, sensors, weekly, one_off in await run_in_threadpool(_node_rooms, db_sql):
        usage = await pf.real_room_usage(db, room_id, sensors, weekly, one_off)
        if usage:
            usage["strategy"] = (await get_controller(db, room_id))["mode"]
            rooms.append(usage)
    # Other rooms: the month-long simulation, when there is one.
    done = {r["room_id"] for r in rooms}
    metas = await db["strategy_simulation_meta"].find({}, {"timetable": 0}).to_list(length=None)
    for meta in sorted(metas, key=lambda m: m["_id"]):
        if meta["_id"] in done:
            continue
        usage = await pf.room_usage(db, meta)
        usage["strategy"] = (await get_controller(db, meta["_id"]))["mode"]
        rooms.append(usage)

    return {
        "tariff": {"pen_per_kwh": pf.TARIFF_PEN_PER_KWH, "label": pf.TARIFF_LABEL},
        "fan_w": pf.FAN_W,
        "building": {"from": pf.BUILDING_FROM, "to": pf.BUILDING_TO, "days": "lunes a sábado"},
        "month_days": pf.MONTH_DAYS,
        "real_days": pf.REAL_DAYS,
        "rooms": sorted(rooms, key=lambda r: r["room_id"]),
        "simulated": not any(r.get("source") == "real" for r in rooms),
    }


def _node_rooms(db_sql: Session) -> list:
    """(room, its sensors, weekly classes, one-day classes) for rooms with a node and classes."""
    from collections import defaultdict
    sensors = defaultdict(list)
    for d in db_sql.query(SensorDevice).all():
        if d.room_id:
            sensors[d.room_id].append(d.id)
    out = []
    for room_id, ids in sensors.items():
        weekly = [(s.day_of_week, s.start_time, s.end_time)
                  for s in db_sql.query(Schedule).filter(Schedule.room_id == room_id).all()]
        one_off = [(r.start_time, r.end_time)
                   for r in db_sql.query(Reservation).filter(Reservation.room_id == room_id).all()]
        if weekly or one_off:
            out.append((room_id, ids, weekly, one_off))
    return out
