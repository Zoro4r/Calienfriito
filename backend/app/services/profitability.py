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
    day) and the class hours, for one strategy's rows ({ts, fan, occupied})."""
    h = step_s / 3600
    on = eq = ref = cls = 0.0
    for r in rows:
        fan = r.get("fan") or 0.0
        if fan > ON_LEVEL:
            on += h
        eq += fan ** 3 * h
        if r.get("ts") is not None and in_building_day(r["ts"]):
            ref += h
        if r.get("occupied"):
            cls += h
    return {"on_hours": on, "eq_hours": eq, "reference_hours": ref, "class_hours": cls}


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
    return {
        "room_id": meta["_id"],
        "copied_from": meta.get("copied_from"),
        "days": round(days, 1),
        "class_hours": r1(rules["class_hours"]),
        "reference_hours": r1(rules["reference_hours"]),
        "rules_hours": r1(rules["on_hours"]),
        "rules_eq_hours": r1(rules["eq_hours"]),
        "learning_hours": r1(learning["on_hours"]),
        "learning_eq_hours": r1(learning["eq_hours"]),
    }
