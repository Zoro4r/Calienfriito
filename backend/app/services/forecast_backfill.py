"""Backfill the forecast log for past days, as the live shadow controller would have done.

The predictive controller only forecasts from the moment it runs; for days before that,
this replays the readings already stored: one mean temperature per minute through the
same ForecastController (Chronos every 5 min), with the room's timetable deciding when
the room is in use. Forecasts already in the log are kept.

Usage (in the backend container, with its environment):
    python -m app.services.forecast_backfill B101 7
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta

from app.services.forecast_controller import HORIZON_MIN, ForecastController
from app.services.strategy_series import class_intervals


async def backfill(db, room_id: str, sensors: list[str], days: int, timetable, forecaster=None) -> int:
    end = datetime.utcnow().replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    rows = await db["sensor_readings"].aggregate([
        {"$match": {"sensor_id": {"$in": sensors}, "is_simulated": False,
                    "timestamp": {"$gte": start.strftime("%Y-%m-%dT%H:%M:%S")}}},
        {"$group": {"_id": {"$substrBytes": ["$timestamp", 0, 16]}, "temp": {"$avg": "$temperature"}}},
        {"$sort": {"_id": 1}},
    ]).to_list(length=None)
    have = {d["ts"] for d in await db["forecast_log"].find({"room_id": room_id, "ts": {"$gte": start}}, {"ts": 1}).to_list(length=None)}
    classes = class_intervals(*timetable, start, end + timedelta(minutes=HORIZON_MIN))
    soon = timedelta(minutes=HORIZON_MIN)
    in_use = lambda m: any(s < m + soon and e > m for s, e, _ in classes)  # noqa: E731   class now or within the horizon

    ctrl = ForecastController(forecaster)
    docs = []
    for r in rows:
        if r.get("temp") is None:
            continue
        minute = datetime.fromisoformat(r["_id"])
        ts_s = (minute - datetime(1970, 1, 1)).total_seconds()
        before = ctrl.last_decision
        ctrl.step(ts_s, r["temp"], in_use(minute))          # closes the previous minute
        ctrl.step(ts_s + 30, r["temp"], in_use(minute))
        if ctrl.last_decision != before and ctrl.forecast_c is not None:
            at = datetime(1970, 1, 1) + timedelta(minutes=ctrl.last_decision)
            if at not in have:
                docs.append({"_id": f"{room_id}|{at:%Y-%m-%dT%H:%M}", "room_id": room_id, "ts": at,
                             "forecast_30": ctrl.forecast_c, "forecast_30_p90": ctrl.peak_p90_c,
                             "fan_pct": ctrl.fan_pct, "model": getattr(ctrl.forecaster, "model_name", "persistencia"),
                             "backfilled": True})
    if docs:
        await db["forecast_log"].insert_many(docs, ordered=False)
    return len(docs)


async def _main(room_id: str, days: int) -> None:
    from app.database import get_db
    from app.database_sql import SessionLocal
    from app.models.admin import SensorDevice
    from app.services.strategy_series import room_timetable

    s = SessionLocal()
    try:
        sensors = [d.id for d in s.query(SensorDevice).filter(SensorDevice.room_id == room_id).all()]
        timetable = room_timetable(s, room_id)
    finally:
        s.close()
    n = await backfill(get_db(), room_id, sensors, days, timetable)
    print(f"{room_id}: {n} forecasts added for the last {days} days")


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 7))
