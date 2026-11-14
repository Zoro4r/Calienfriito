"""Time series for the "Estrategias" tab: rules vs learning, aggregated in the database.

Two sources:
  · simulation — `strategy_simulation` holds both strategies run on the same simulated
                 days (ml/simulation/generate_month.py), so each has its own temperature.
  · live       — the node's readings: the rules' temperature and actuators, and the
                 learning controller's shadow decisions (no temperature of its own,
                 because it does not act).
The frontend never receives raw readings here: one point per time bucket.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.config import LOCAL_UTC_OFFSET_HOURS
from app.services.forecast_controller import HORIZON_MIN
from app.services.learning_controller import BAND_HI, BAND_LO, NODE_FAN_MAX_W, NODE_HEATER_W

# Bucket minutes per range: ~300–370 points per series.
BUCKET_MIN = {"12h": 2, "1d": 5, "7d": 30, "30d": 120}
_OFFSET = timedelta(hours=LOCAL_UTC_OFFSET_HOURS)


def period(range_key: str, anchor: date | None, data_end: datetime) -> tuple[datetime, datetime, date]:
    """[start, end) in naive UTC for a range, aligned to the LOCAL calendar.

    12h: the last 12 hours up to the latest data. 1d: the local day of `anchor`
    (00:00–24:00). 7d: its Monday–Sunday week. 30d: its calendar month. Without an
    anchor, the period that holds the latest data."""
    if range_key == "12h":
        return data_end - timedelta(hours=12), data_end, (data_end + _OFFSET).date()
    d = anchor or (data_end + _OFFSET).date()
    if range_key == "1d":
        first, last = d, d + timedelta(days=1)
    elif range_key == "7d":
        first = d - timedelta(days=d.weekday())
        last = first + timedelta(days=7)
    else:
        first = d.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1)
    return datetime.combine(first, time()) - _OFFSET, datetime.combine(last, time()) - _OFFSET, d


def _frame(range_key: str, start: datetime, end: datetime, anchor: date, data_start: datetime | None, data_end: datetime) -> dict:
    return {
        "range": range_key,
        "date": anchor.isoformat(),
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "data_start": data_start.strftime("%Y-%m-%dT%H:%M:%S") if data_start else None,
        "data_end": data_end.strftime("%Y-%m-%dT%H:%M:%S"),
        "bucket_min": BUCKET_MIN[range_key],
    }


def _out_of_band(temp_expr) -> dict:
    # A missing temperature (null sorts below numbers in MongoDB) is not "out of band".
    return {"$and": [{"$ne": [temp_expr, None]}, {"$or": [{"$lt": [temp_expr, BAND_LO]}, {"$gt": [temp_expr, BAND_HI]}]}]}


def _hot(temp_expr) -> dict:
    # Heat discomfort: above the band's top (the predictive controller's success metric).
    return {"$and": [{"$ne": [temp_expr, None]}, {"$gt": [temp_expr, BAND_HI]}]}


def _points(rows: list[dict]) -> list[dict]:
    return [{
        "t": r["_id"]["b"].strftime("%Y-%m-%dT%H:%M:%S"),
        "temp": None if r.get("temp") is None else round(r["temp"], 2),
        "fan": None if r.get("fan") is None else round(r["fan"] * 100, 1),
        "heat": None if r.get("heat") is None else round(r["heat"] * 100, 1),
    } for r in rows]


def _intervals(minutes: list[dict]) -> list[dict]:
    """Merge per-minute occupancy rows ({_id: minute, occ, people}) into class intervals."""
    out: list[dict] = []
    for m in sorted(minutes, key=lambda r: r["_id"]):
        # A change of class leaves a few people in the room for a minute or two: below
        # this many, the minute splits two back-to-back classes.
        if not m.get("occ") or (m.get("people") is not None and m["people"] < 5):
            continue
        start, end = m["_id"], m["_id"] + timedelta(minutes=1)
        if out and start == out[-1]["_end"]:
            out[-1]["_end"] = end
            out[-1]["people"] = max(out[-1]["people"], m.get("people") or 0)
        else:
            out.append({"_start": start, "_end": end, "people": m.get("people") or 0})
    return [{"start": i["_start"].strftime("%Y-%m-%dT%H:%M:%S"), "end": i["_end"].strftime("%Y-%m-%dT%H:%M:%S"),
             "people": int(i["people"]) if i["people"] else None} for i in out]


def _summary(s: dict | None, step_s: float, fan_w: float, heater_w: float, comfort: bool = True) -> dict | None:
    if not s:
        return None
    return {
        "pct_out_of_band": round(100 * s["out"] / s["occ"], 1) if comfort and s.get("occ") else None,
        "pct_hot": round(100 * s.get("hot", 0) / s["occ"], 1) if comfort and s.get("occ") else None,
        "hot_hours": round(s.get("hot", 0) * step_s / 3600, 2) if comfort else None,
        "occupied_hours": round(s.get("occ", 0) * step_s / 3600, 1),
        "kwh": round((fan_w * s["fan3"] + heater_w * s["heat"]) * step_s / 3600 / 1000, 2),
    }


async def simulation_series(db, room_id: str, meta: dict, range_key: str, anchor: date | None = None) -> dict:
    bucket_min = BUCKET_MIN[range_key]
    start, end, anchor = period(range_key, anchor, meta["end"])
    match = {"room_id": room_id, "ts": {"$gte": start, "$lt": end} if range_key != "12h" else {"$gt": start, "$lte": end}}
    rows = await db["strategy_simulation"].aggregate([
        {"$match": match},
        {"$group": {
            "_id": {"s": "$strategy", "b": {"$dateTrunc": {"date": "$ts", "unit": "minute", "binSize": bucket_min}}},
            "temp": {"$avg": "$temperature"}, "fan": {"$avg": "$fan"}, "heat": {"$avg": "$heat"},
        }},
        {"$sort": {"_id.b": 1}},
    ]).to_list(length=None)
    totals = await db["strategy_simulation"].aggregate([
        {"$match": match},
        {"$group": {
            "_id": "$strategy",
            "occ": {"$sum": {"$cond": ["$occupied", 1, 0]}},
            "out": {"$sum": {"$cond": [{"$and": ["$occupied", _out_of_band("$temperature")]}, 1, 0]}},
            "hot": {"$sum": {"$cond": [{"$and": ["$occupied", _hot("$temperature")]}, 1, 0]}},
            "fan3": {"$sum": {"$pow": ["$fan", 3]}},
            "heat": {"$sum": "$heat"},
        }},
    ]).to_list(length=None)
    minutes = await db["strategy_simulation"].aggregate([
        {"$match": {**match, "strategy": "rules"}},
        {"$group": {"_id": {"$dateTrunc": {"date": "$ts", "unit": "minute"}},
                    "occ": {"$max": "$occupied"}, "people": {"$max": "$people"}}},
    ]).to_list(length=None)
    eq = meta["equipment"]
    by = {t["_id"]: t for t in totals}
    return {
        "source": "simulation",
        "equipment": eq,
        "step_s": meta["step_s"],
        "warmup_days": meta.get("warmup_days"),
        "timetable": meta.get("timetable"),
        **_frame(range_key, start, end, anchor, meta.get("start"), meta["end"]),
        "occupancy": _intervals(minutes),
        "rules": {"points": _points([r for r in rows if r["_id"]["s"] == "rules"]),
                  "summary": _summary(by.get("rules"), meta["step_s"], eq["fan_max_w"], eq["heater_w"])},
        "learning": {"points": _points([r for r in rows if r["_id"]["s"] == "learning"]),
                     "summary": _summary(by.get("learning"), meta["step_s"], eq["fan_max_w"], eq["heater_w"])},
    }


# (sensors, range, anchor) -> (computed at, result). Today's data changes every second, but
# the tab polls every 30 s: a minute-old result is fine. Past periods never change.
_LIVE_CACHE: dict[tuple, tuple[datetime, dict | None]] = {}
LIVE_CACHE_S = 60


async def live_series(db, sensors: list[str], range_key: str, anchor: date | None = None) -> dict | None:
    key = (tuple(sorted(sensors)), range_key, anchor)
    now = datetime.utcnow()
    hit = _LIVE_CACHE.get(key)
    if hit:
        ended = bool(hit[1]) and datetime.fromisoformat(hit[1]["end"]) <= hit[0]   # a past period: final
        if ended or (now - hit[0]).total_seconds() < LIVE_CACHE_S:
            return hit[1]
    result = await _live_series(db, sensors, range_key, anchor)
    if len(_LIVE_CACHE) > 200:
        _LIVE_CACHE.clear()
    _LIVE_CACHE[key] = (now, result)
    return result


async def _live_series(db, sensors: list[str], range_key: str, anchor: date | None = None) -> dict | None:
    bucket_min = BUCKET_MIN[range_key]
    base = {"sensor_id": {"$in": sensors}, "node_state.accion": {"$exists": True}}
    last = await db["sensor_readings"].find(base, {"timestamp": 1}).sort("timestamp", -1).limit(1).to_list(length=1)
    if not last:
        return None
    first = await db["sensor_readings"].find(base, {"timestamp": 1}).sort("timestamp", 1).limit(1).to_list(length=1)
    data_end = datetime.fromisoformat(last[0]["timestamp"][:19])
    data_start = datetime.fromisoformat(first[0]["timestamp"][:19])
    start, end, anchor = period(range_key, anchor, data_end)
    fmt_ = "%Y-%m-%dT%H:%M:%S"
    match = {**base, "timestamp": {"$gte": start.strftime(fmt_), "$lt": end.strftime(fmt_)}}
    ts = {"$toDate": {"$substrBytes": ["$timestamp", 0, 19]}}
    fan = {"$divide": [{"$ifNull": ["$node_state.ventilador_pwm", 0]}, 255]}
    heat = {"$cond": ["$node_state.calefactor", 1, 0]}
    occupied = {"$ne": ["$node_state.accion", "DISABLED"]}   # the firmware's own occupancy decision
    # One pass over the period's readings (~86 000 a day at 1 Hz): buckets, totals and
    # occupied minutes come out of the same scan.
    facets = await db["sensor_readings"].aggregate([
        {"$match": match},
        {"$addFields": {"_t": ts, "_fan": fan, "_heat": heat, "_occ": occupied}},
        {"$facet": {
            "rows": [
                {"$group": {
                    "_id": {"b": {"$dateTrunc": {"date": "$_t", "unit": "minute", "binSize": bucket_min}}},
                    "temp": {"$avg": "$temperature"}, "fan": {"$avg": "$_fan"}, "heat": {"$avg": "$_heat"},
                    "lfan": {"$avg": {"$divide": ["$learning_action.fan_pct", 100]}},
                    "lheat": {"$avg": {"$divide": ["$learning_action.heater_pct", 100]}},
                    "pred": {"$avg": "$learning_action.forecast_30"},
                }},
                {"$sort": {"_id.b": 1}},
            ],
            "totals": [
                {"$group": {
                    "_id": None, "n": {"$sum": 1},
                    "occ": {"$sum": {"$cond": ["$_occ", 1, 0]}},
                    "out": {"$sum": {"$cond": [{"$and": ["$_occ", _out_of_band("$temperature")]}, 1, 0]}},
                    "hot": {"$sum": {"$cond": [{"$and": ["$_occ", _hot("$temperature")]}, 1, 0]}},
                    "fan3": {"$sum": {"$pow": ["$_fan", 3]}}, "heat": {"$sum": "$_heat"},
                    "lfan3": {"$sum": {"$pow": [{"$divide": [{"$ifNull": ["$learning_action.fan_pct", 0]}, 100]}, 3]}},
                    "lheat": {"$sum": {"$divide": [{"$ifNull": ["$learning_action.heater_pct", 0]}, 100]}},
                    "first": {"$min": "$timestamp"},
                }},
            ],
            "minutes": [
                {"$group": {"_id": {"$dateTrunc": {"date": "$_t", "unit": "minute"}}, "occ": {"$max": "$_occ"}}},
            ],
        }},
    ]).to_list(length=None)
    rows, totals, minutes = facets[0]["rows"], facets[0]["totals"], facets[0]["minutes"]
    t = totals[0] if totals else None
    step_s = 1.0
    if t and t["n"] > 1:
        span_end = min(end, data_end)
        step_s = max(1.0, (span_end - datetime.fromisoformat(t["first"][:19])).total_seconds() / (t["n"] - 1))
    learn_rows = [{"_id": r["_id"], "temp": None, "fan": r.get("lfan"), "heat": r.get("lheat")} for r in rows]
    return {
        "source": "live",
        "equipment": {"label": "Actuadores del nodo", "fan_max_w": NODE_FAN_MAX_W, "heater_w": NODE_HEATER_W},
        "step_s": round(step_s, 1),
        **_frame(range_key, start, end, anchor, data_start, data_end),
        "occupancy": _intervals(minutes),
        "rules": {"points": _points(rows), "summary": _summary(t, step_s, NODE_FAN_MAX_W, NODE_HEATER_W)},
        "forecast": forecast_accuracy(rows, bucket_min),
        "learning": {"points": _points(learn_rows),
                     "summary": _summary(t and {"fan3": t["lfan3"], "heat": t["lheat"], "occ": t["occ"]},
                                         step_s, NODE_FAN_MAX_W, NODE_HEATER_W, comfort=False)},
    }


def forecast_accuracy(rows: list[dict], bucket_min: int) -> dict | None:
    """How well the 30-min forecasts matched what the room then measured.

    Each bucket's mean forecast is compared with the measured temperature HORIZON_MIN
    later. The persistence baseline ("it stays as it is now") is scored on the same
    pairs, so the pretrained model's value is measured, not assumed.
    """
    if HORIZON_MIN % bucket_min:
        return None                                   # buckets too coarse to shift by the horizon
    temp = {r["_id"]["b"]: r.get("temp") for r in rows}
    shift = timedelta(minutes=HORIZON_MIN)
    points, err, err_base = [], [], []
    for r in rows:
        b, pred = r["_id"]["b"], r.get("pred")
        if pred is None:
            continue
        points.append({"t": (b + shift).strftime("%Y-%m-%dT%H:%M:%S"), "pred": round(pred, 2)})
        actual, now = temp.get(b + shift), temp.get(b)
        if actual is not None and now is not None:
            err.append(abs(pred - actual))
            err_base.append(abs(now - actual))
    return {
        "horizon_min": HORIZON_MIN,
        "points": points,
        "pairs": len(err),
        "mae": round(sum(err) / len(err), 2) if err else None,
        "mae_baseline": round(sum(err_base) / len(err_base), 2) if err_base else None,
    }


WARM_EVERY_S = 50
_warm_task = None


async def warm_live_cache(db, sensor_groups: list[list[str]]) -> int:
    """Compute today's series for the rooms whose node reported in the last 2 minutes, so
    opening the Aprendizaje tab finds it ready instead of scanning a day of readings."""
    recent = (datetime.utcnow() - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
    done = 0
    for sensors in sensor_groups:
        if await db["sensor_readings"].find_one({"sensor_id": {"$in": sensors}, "timestamp": {"$gte": recent}}, {"_id": 1}):
            _LIVE_CACHE.pop((tuple(sorted(sensors)), "1d", None), None)
            await live_series(db, sensors, "1d")
            done += 1
    return done


def start_warm_loop() -> None:
    """Background loop: keep today's live series fresh for rooms with an active node."""
    import asyncio
    import logging
    from collections import defaultdict
    from starlette.concurrency import run_in_threadpool
    from app.database import get_db
    from app.database_sql import SessionLocal
    from app.models.admin import SensorDevice

    global _warm_task
    if _warm_task is not None:
        return

    def groups() -> list[list[str]]:
        s = SessionLocal()
        try:
            by_room: dict[str, list[str]] = defaultdict(list)
            for d in s.query(SensorDevice).all():
                if d.room_id:
                    by_room[d.room_id].append(d.id)
            return list(by_room.values())
        finally:
            s.close()

    async def _run():
        while True:
            try:
                await warm_live_cache(get_db(), await run_in_threadpool(groups))
            except Exception:
                logging.getLogger(__name__).exception("Warming the Aprendizaje cache failed")
            await asyncio.sleep(WARM_EVERY_S)

    _warm_task = asyncio.get_event_loop().create_task(_run())
