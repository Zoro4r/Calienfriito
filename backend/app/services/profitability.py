"""Fan hours and energy per room: our control against the fan always on.

Reference ("sin el sistema"): the fan at full power for the whole building day
(BUILDING_FROM–BUILDING_TO, Monday–Saturday), as a room fan is usually switched on in
the morning and left on. With the system: the fan runs only when the room is in use and
warm, at the power it needs, as the month-long simulation of each room recorded for the
rules and for the predictive controller.

Energy is returned as "full-power hours" (Σ speed³ · time: fan power grows with the cube
of its speed), so the dashboard multiplies by whatever fan power the client has. All
figures are scaled to a 30-day month so rooms simulated for different spans compare fairly.
"""
from datetime import datetime, timedelta

from app.config import LOCAL_UTC_OFFSET_HOURS

MONTH_DAYS = 30
BUILDING_FROM, BUILDING_TO = 7, 22          # local hours the building is open
BUILDING_WEEKDAYS = 6                       # Monday–Saturday
FAN_W = 180.0                               # wall extractor sized for a classroom (1800 m³/h, EC motor)
ON_LEVEL = 0.01                             # a fan level above this counts as "on"

# Luz del Sur, pliego tarifario 04/10/2026, Lima Sur, MT4, energía con IGV.
TARIFF_PEN_PER_KWH = 0.3644
TARIFF_LABEL = "Luz del Sur MT4, pliego oct. 2026, con IGV. Solo energía."

_OFFSET = timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
# (room, strategy) -> (generated_at, result): the simulation only changes when regenerated.
_CACHE: dict[tuple[str, str], tuple[datetime, dict]] = {}


def in_building_day(ts_utc: datetime) -> bool:
    local = ts_utc + _OFFSET
    return local.weekday() < BUILDING_WEEKDAYS and BUILDING_FROM <= local.hour < BUILDING_TO


def fan_usage(rows: list[dict], step_s: float) -> dict:
    """Hours the fan ran, its full-power-equivalent hours, the reference hours (building
    day) and the class hours, for one strategy's rows ({ts, fan, occupied}); plus the
    full-power hours of both per local hour of the day (for "un día típico")."""
    h = step_s / 3600
    on = eq = ref = cls = 0.0
    eq_by_hour, ref_by_hour = [0.0] * 24, [0.0] * 24
    for r in rows:
        fan = r.get("fan") or 0.0
        hour = (r["ts"] + _OFFSET).hour if r.get("ts") is not None else None
        if fan > ON_LEVEL:
            on += h
        eq += fan ** 3 * h
        if hour is not None:
            eq_by_hour[hour] += fan ** 3 * h
        if r.get("ts") is not None and in_building_day(r["ts"]):
            ref += h
            ref_by_hour[hour] += h
        if r.get("occupied"):
            cls += h
    return {"on_hours": on, "eq_hours": eq, "reference_hours": ref, "class_hours": cls,
            "eq_by_hour": eq_by_hour, "ref_by_hour": ref_by_hour}


async def _room_strategy(db, meta: dict, strategy: str) -> dict:
    key = (meta["_id"], strategy)
    hit = _CACHE.get(key)
    if hit and hit[0] == meta.get("generated_at"):
        return hit[1]
    rows = await (
        db["strategy_simulation"]
        .find({"room_id": meta["_id"], "strategy": strategy}, {"_id": 0, "ts": 1, "fan": 1, "occupied": 1})
        .to_list(length=None)
    )
    result = fan_usage(rows, float(meta["step_s"]))
    _CACHE[key] = (meta.get("generated_at"), result)
    return result


async def room_usage(db, meta: dict) -> dict:
    """Monthly fan hours for one simulated room: reference, rules and predictive control."""
    days = max((meta["end"] - meta["start"]).total_seconds() / 86400, 1)
    scale = MONTH_DAYS / days
    rules = await _room_strategy(db, meta, "rules")
    learning = await _room_strategy(db, meta, "learning")
    r1 = lambda x: round(x * scale, 1)  # noqa: E731
    per_day = lambda xs: [round(x / days, 4) for x in xs]  # noqa: E731   average day
    return {
        "room_id": meta["_id"],
        "copied_from": meta.get("copied_from"),
        "source": "simulated",
        "days": round(days, 1),
        "class_hours": r1(rules["class_hours"]),
        "reference_hours": r1(rules["reference_hours"]),
        "rules_hours": r1(rules["on_hours"]),
        "rules_eq_hours": r1(rules["eq_hours"]),
        "learning_hours": r1(learning["on_hours"]),
        "learning_eq_hours": r1(learning["eq_hours"]),
        # Full-power hours per local hour of an average day (× fan kW = kWh in that hour).
        "day_reference": per_day(rules["ref_by_hour"]),
        "day_rules": per_day(rules["eq_by_hour"]),
        "day_learning": per_day(learning["eq_by_hour"]),
    }


# ── Rooms with a real node: measured temperature + timetable ──────────────────
# The heuristic is applied to what the node really measured, minute by minute, during
# the room's classes (weekly and one-day). No thermal model: closer to the real room.
REAL_DAYS = 7
REAL_CACHE_S = 600
_REAL_CACHE: dict[str, tuple[datetime, dict]] = {}


def rules_level(temp_c: float) -> float:
    """Firmware rules for an occupied room: 50 / 75 / 100 % from 24.5 / 25.0 / 25.5 °C."""
    if temp_c >= 25.5:
        return 1.0
    if temp_c >= 25.0:
        return 0.75
    if temp_c >= 24.5:
        return 0.5
    return 0.0


def occupancy(weekly: list[tuple[int, object, object]], one_off: list[tuple[datetime, datetime]]):
    """occupied(local datetime) for weekly classes (weekday, start time, end time) and
    one-day classes (start, end), all in local time."""
    def occupied(local: datetime) -> bool:
        t = local.time()
        return any(local.weekday() == d and s <= t < e for d, s, e in weekly) or any(s <= local < e for s, e in one_off)
    return occupied


def minute_usage(minutes: list[dict], occupied) -> dict:
    """Fan use per strategy from one row per minute ({ts: naive UTC, temp, lfan: % or None}).

    Rules: the heuristic on the measured temperature while a class is on. Predictive: the
    shadow controller's recorded decision when there is one, else the rules' level."""
    h = 1 / 60
    out = {"reference_hours": 0.0, "class_hours": 0.0,
           "rules_hours": 0.0, "rules_eq_hours": 0.0, "learning_hours": 0.0, "learning_eq_hours": 0.0,
           "day_reference": [0.0] * 24, "day_rules": [0.0] * 24, "day_learning": [0.0] * 24}
    for m in minutes:
        local = m["ts"] + _OFFSET
        if in_building_day(m["ts"]):
            out["reference_hours"] += h
            out["day_reference"][local.hour] += h
        if not occupied(local) or m.get("temp") is None:
            continue
        out["class_hours"] += h
        rules = rules_level(m["temp"])
        learn = m["lfan"] / 100 if m.get("lfan") is not None else rules
        for key, lvl in (("rules", rules), ("learning", learn)):
            if lvl > ON_LEVEL:
                out[f"{key}_hours"] += h
            out[f"{key}_eq_hours"] += lvl ** 3 * h
            out[f"day_{key}"][local.hour] += lvl ** 3 * h
    return out


async def real_room_usage(db, room_id: str, sensors: list[str], weekly, one_off, now: datetime | None = None) -> dict | None:
    """Monthly fan hours for a room with a real node, from its last REAL_DAYS of readings."""
    now = now or datetime.utcnow()
    hit = _REAL_CACHE.get(room_id)
    if hit and (now - hit[0]).total_seconds() < REAL_CACHE_S:
        return hit[1]
    start_local = datetime.combine((now + _OFFSET).date() - timedelta(days=REAL_DAYS - 1), datetime.min.time())
    start_utc = start_local - _OFFSET
    rows = await db["sensor_readings"].aggregate([
        {"$match": {"sensor_id": {"$in": sensors}, "is_simulated": False,
                    "timestamp": {"$gte": start_utc.strftime("%Y-%m-%dT%H:%M:%S")}}},
        {"$group": {"_id": {"$substrBytes": ["$timestamp", 0, 16]},
                    "temp": {"$avg": "$temperature"}, "lfan": {"$avg": "$learning_action.fan_pct"}}},
    ]).to_list(length=None)
    if not rows:
        return None
    minutes = [{"ts": datetime.fromisoformat(r["_id"]), "temp": r.get("temp"), "lfan": r.get("lfan")} for r in rows]
    u = minute_usage(minutes, occupancy(weekly, one_off))
    days = max((now - start_utc).total_seconds() / 86400, 1)
    scale = MONTH_DAYS / days
    result = {
        "room_id": room_id, "copied_from": None, "source": "real", "days": round(days, 1),
        **{k: round(v * scale, 1) for k, v in u.items() if not k.startswith("day_")},
        **{k: [round(x / days, 4) for x in v] for k, v in u.items() if k.startswith("day_")},
    }
    _REAL_CACHE[room_id] = (now, result)
    return result
