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
from app.services.learning_controller import BAND_HI, BAND_LO, NODE_FAN_MAX_W, NODE_HEATER_W

# Bucket minutes per range: ~300–370 points per series.
BUCKET_MIN = {"12h": 2, "1d": 5, "7d": 30, "30d": 120}
NODE_OCCUPIED_ADC = 2200
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


async def live_series(db, sensors: list[str], range_key: str, anchor: date | None = None) -> dict | None:
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
    occupied = {"$gte": [{"$ifNull": ["$node_state.co2_proxy", 0]}, NODE_OCCUPIED_ADC]}
    rows = await db["sensor_readings"].aggregate([
        {"$match": match},
        {"$group": {
            "_id": {"b": {"$dateTrunc": {"date": ts, "unit": "minute", "binSize": bucket_min}}},
            "temp": {"$avg": "$temperature"}, "fan": {"$avg": fan}, "heat": {"$avg": heat},
            "lfan": {"$avg": {"$divide": ["$learning_action.fan_pct", 100]}},
            "lheat": {"$avg": {"$divide": ["$learning_action.heater_pct", 100]}},
        }},
        {"$sort": {"_id.b": 1}},
    ]).to_list(length=None)
    totals = await db["sensor_readings"].aggregate([
        {"$match": match},
        {"$group": {
            "_id": None, "n": {"$sum": 1},
            "occ": {"$sum": {"$cond": [occupied, 1, 0]}},
            "out": {"$sum": {"$cond": [{"$and": [occupied, _out_of_band("$temperature")]}, 1, 0]}},
            "fan3": {"$sum": {"$pow": [fan, 3]}}, "heat": {"$sum": heat},
            "lfan3": {"$sum": {"$pow": [{"$divide": [{"$ifNull": ["$learning_action.fan_pct", 0]}, 100]}, 3]}},
            "lheat": {"$sum": {"$divide": [{"$ifNull": ["$learning_action.heater_pct", 0]}, 100]}},
            "first": {"$min": "$timestamp"},
        }},
    ]).to_list(length=None)
    minutes = await db["sensor_readings"].aggregate([
        {"$match": match},
        {"$group": {"_id": {"$dateTrunc": {"date": ts, "unit": "minute"}}, "occ": {"$max": occupied}}},
    ]).to_list(length=None)
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
        "learning": {"points": _points(learn_rows),
                     "summary": _summary(t and {"fan3": t["lfan3"], "heat": t["lheat"], "occ": t["occ"]},
                                         step_s, NODE_FAN_MAX_W, NODE_HEATER_W, comfort=False)},
    }
