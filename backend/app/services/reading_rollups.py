"""Per-minute rollups of node readings, so history views never scan raw telemetry.

A node publishes one reading per second (~86 400 a day per room). The "Historial" and
"Estrategias" tabs only draw 2–120 min buckets and day totals, so each minute of one
sensor is stored once in `reading_minutes`:

  · sums for averages (temperature, humidity, CO₂), CO peak, fan / fan³ / heater sums,
  · `segs` — runs of consecutive readings with the same actuator state (fan PWM, heater,
    manual order, CO alarm, node action) and no gap over MAX_GAP_S. Inside a run each
    reading lasts until the next one, so its duration and the time out of the comfort
    band are exact; replaying the runs gives the same periods, events and totals as
    `room_history.summarize_day` over the raw readings,
  · `ina` — measured energy integrated inside the minute plus its first / last power
    sample, so energy across minute edges is joined exactly,
  · `raw_n` — every reading of the sensor in that minute (node or not), to detect
    readings that arrived after the minute was rolled up.

The only approximation: with a timetable, a run is "in class" by its first reading.
Runs never cross a minute, so this is exact while classes start and end on whole
minutes (timetables and reservations are edited in minutes).

A background loop rolls every sensor up to the minute of its latest reading; readers
use the rollups up to that point and roll the few raw minutes after it on the fly.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Iterable

from app.services.learning_controller import BAND_HI, BAND_LO
from app.services.room_history import BUCKET_MIN, FMT, GAP_EVENT_S, MAX_GAP_S, PROJECTION, _manual_event

logger = logging.getLogger(__name__)

MINUTES = "reading_minutes"
STATE = "reading_rollup_state"
REPROCESS_MIN = 5          # minutes re-rolled on every pass, for readings that arrive late
CHUNK_MIN = 24 * 60        # minutes rolled per call while catching up
ROLL_EVERY_S = 60
EPOCH_2000 = datetime(2000, 1, 1)   # $dateTrunc's reference for binSize


def _ts19(s: str) -> datetime:
    return datetime.fromisoformat(s[:19])


def _floor_min(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def minute_id(sensor_id: str, m: datetime) -> str:
    return f"{sensor_id}|{m:%Y-%m-%dT%H:%M}"


# ── Building ────────────────────────────────────────────────────────────────────

def _new_minute(sensor_id: str, m: datetime) -> dict:
    return {"_id": minute_id(sensor_id, m), "sensor_id": sensor_id, "m": m, "raw_n": 0, "n": 0,
            "temp_s": 0.0, "temp_n": 0, "hum_s": 0.0, "hum_n": 0, "co2_s": 0.0, "co2_n": 0, "co_max": 0.0,
            "fan_s": 0.0, "fan3_s": 0.0, "heat_s": 0.0, "segs": [],
            "ina": {"wh": 0.0, "any": False, "f": None, "l": None}}


def build_minutes(docs: Iterable[dict], sensor_id: str) -> list[dict]:
    """Rollups of one sensor's readings (sorted by timestamp). Readings without a node
    decision (`node_state.accion` missing) only count in `raw_n`."""
    out: list[dict] = []
    cur: dict | None = None
    ina_prev: tuple[datetime, float] | None = None
    for d in docs:
        ts = _ts19(d["timestamp"])
        m = _floor_min(ts)
        if cur is None or cur["m"] != m:
            cur = _new_minute(sensor_id, m)
            out.append(cur)
            ina_prev = None
        cur["raw_n"] += 1
        ns = d.get("node_state") or {}
        if "accion" not in ns:
            continue

        cur["n"] += 1
        temp, hum, co2 = d.get("temperature"), d.get("humidity"), d.get("co2_ppm")
        co = d.get("co_ppm") or 0.0
        if temp is not None:
            cur["temp_s"] += temp
            cur["temp_n"] += 1
        if hum is not None:
            cur["hum_s"] += hum
            cur["hum_n"] += 1
        if co2 is not None:
            cur["co2_s"] += co2
            cur["co2_n"] += 1
        cur["co_max"] = max(cur["co_max"], co)
        pwm = ns.get("ventilador_pwm") or 0
        fan = pwm / 255
        heat = 1 if ns.get("calefactor") else 0
        cur["fan_s"] += fan
        cur["fan3_s"] += fan ** 3
        cur["heat_s"] += heat

        out_band = temp is not None and (temp < BAND_LO or temp > BAND_HI)
        key = (pwm, heat, bool(ns.get("manual")), bool(ns.get("alarma_co")), ns.get("accion"))
        segs = cur["segs"]
        last = segs[-1] if segs else None
        if last and last["_key"] == key and (ts - last["_l"]).total_seconds() <= MAX_GAP_S:
            gap = (ts - last["_l"]).total_seconds()      # the previous reading lasted until this one
            last["d"] += gap
            if last["lo"]:
                last["o"] += gap
            last["_l"] = ts
            last["lo"] = out_band
            last["pk"] = max(last["pk"], co)
        else:
            segs.append({"_key": key, "_s": ts, "_l": ts, "d": 0.0, "o": 0.0, "lo": out_band, "pk": co,
                         "pwm": pwm, "h": heat, "man": key[2], "al": key[3], "a": key[4]})

        # Measured energy, as room_history.ina_energy_series: never across a missing sample.
        power = d.get("ina_power_mw")
        full_ts = datetime.fromisoformat(d["timestamp"])
        ina = cur["ina"]
        if ina["f"] is None:
            ina["f"] = [d["timestamp"], power]
        ina["l"] = [d["timestamp"], power]
        if power is None:
            ina_prev = None
        else:
            ina["any"] = True
            if ina_prev:
                gap = (full_ts - ina_prev[0]).total_seconds()
                if 0 < gap <= MAX_GAP_S:
                    ina["wh"] += (ina_prev[1] + power) * gap / 7_200_000
            ina_prev = (full_ts, power)

    for cur in out:
        for g in cur["segs"]:
            g["s"] = g.pop("_s").strftime(FMT)
            g["l"] = g.pop("_l").strftime(FMT)
            g.pop("_key")
    return out


async def rollup_range(db, sensor_id: str, start: datetime, end: datetime) -> int:
    """(Re)build one sensor's rollups for the minutes in [start, end). Returns minutes stored."""
    from pymongo import ReplaceOne

    match = {"sensor_id": sensor_id, "timestamp": {"$gte": start.strftime(FMT), "$lt": end.strftime(FMT)}}
    docs = await db["sensor_readings"].find(match, PROJECTION).sort("timestamp", 1).to_list(length=None)
    minutes = build_minutes(docs, sensor_id)
    if minutes:
        await db[MINUTES].bulk_write([ReplaceOne({"_id": m["_id"]}, m, upsert=True) for m in minutes], ordered=False)
    await db[MINUTES].delete_many({"sensor_id": sensor_id, "m": {"$gte": start, "$lt": end},
                                   "_id": {"$nin": [m["_id"] for m in minutes]}})
    return len(minutes)


async def roll_sensor(db, sensor_id: str, chunk_min: int = CHUNK_MIN) -> int:
    """Roll one sensor forward up to the minute of its latest reading (exclusive: that
    minute may still be filling). Returns the minutes covered by this call (0 = up to date)."""
    readings = db["sensor_readings"]
    last = await readings.find({"sensor_id": sensor_id}, {"timestamp": 1}).sort("timestamp", -1).limit(1).to_list(1)
    if not last:
        return 0
    last_min = _floor_min(_ts19(last[0]["timestamp"]))
    state = await db[STATE].find_one({"_id": sensor_id})
    if state:
        upto = state["upto"]
        start = upto - timedelta(minutes=REPROCESS_MIN)
    else:
        first = await readings.find({"sensor_id": sensor_id}, {"timestamp": 1}).sort("timestamp", 1).limit(1).to_list(1)
        upto = start = _floor_min(_ts19(first[0]["timestamp"]))
    end = min(last_min, start + timedelta(minutes=chunk_min))
    if end <= start:
        return 0
    await rollup_range(db, sensor_id, start, end)
    if end > upto:
        await db[STATE].update_one({"_id": sensor_id}, {"$set": {"upto": end}}, upsert=True)
        return int((end - upto).total_seconds() // 60)
    return 0


async def rolled_upto(db, sensors: list[str]) -> dict[str, datetime]:
    """Per sensor, the first minute NOT covered by rollups (missing = nothing rolled)."""
    return {s["_id"]: s["upto"] for s in await db[STATE].find({"_id": {"$in": sensors}}).to_list(length=None)}


async def load_minutes(db, sensors: list[str], start: datetime, end: datetime,
                       projection: dict | None = None, verify: bool = False) -> dict[str, list[dict]]:
    """Each sensor's minutes in [start, end) (minute-aligned, naive UTC): stored rollups up
    to the rolled point, and the raw readings after it rolled on the fly.

    `verify` counts the raw readings of the rolled part and rebuilds it when readings
    arrived after it was rolled (a cheap index count). Fewer raw readings than rolled is
    fine: raw telemetry may have been purged after rolling."""
    upto = await rolled_upto(db, sensors)
    result: dict[str, list[dict]] = {}
    for sid in sensors:
        cut = min(max(upto.get(sid, start), start), end)
        stored: list[dict] = []
        if cut > start:
            q = {"sensor_id": sid, "m": {"$gte": start, "$lt": cut}}
            stored = await db[MINUTES].find(q, projection).sort("m", 1).to_list(length=None)
            if verify:
                raw = await db["sensor_readings"].count_documents(
                    {"sensor_id": sid, "timestamp": {"$gte": start.strftime(FMT), "$lt": cut.strftime(FMT)}})
                if raw > sum(m.get("raw_n", 0) for m in stored):
                    logger.info("Rollups of %s behind raw readings in %s..%s: rebuilding", sid, start, cut)
                    await rollup_range(db, sid, start, cut)
                    stored = await db[MINUTES].find(q, projection).sort("m", 1).to_list(length=None)
        tail: list[dict] = []
        if cut < end:
            match = {"sensor_id": sid, "node_state.accion": {"$exists": True},
                     "timestamp": {"$gte": cut.strftime(FMT), "$lt": end.strftime(FMT)}}
            docs = await db["sensor_readings"].find(match, PROJECTION).sort("timestamp", 1).to_list(length=None)
            tail = build_minutes(docs, sid)
        minutes = [m for m in stored + tail if m.get("n")]
        if minutes:
            result[sid] = minutes
    return result


# ── Reading ─────────────────────────────────────────────────────────────────────

def summarize_minutes(minutes: list[dict], fan_max_w: float, heater_w: float,
                      timetable: list[tuple[datetime, datetime, int]] | None = None) -> tuple[dict, float | None]:
    """`room_history.summarize_day` from one sensor's rollups (sorted by minute), plus the
    day's measured INA219 energy in Wh (None without samples)."""
    buckets: dict[datetime, dict] = {}
    for m in minutes:
        b = m["m"].replace(minute=m["m"].minute - m["m"].minute % BUCKET_MIN)
        acc = buckets.setdefault(b, {"n": 0, "temp": 0.0, "temp_n": 0, "hum": 0.0, "hum_n": 0, "co2": 0.0, "co2_n": 0,
                                     "co": 0.0, "fan": 0.0, "heat": 0.0})
        acc["n"] += m["n"]
        acc["temp"] += m["temp_s"]
        acc["temp_n"] += m["temp_n"]
        acc["hum"] += m["hum_s"]
        acc["hum_n"] += m["hum_n"]
        acc["co2"] += m["co2_s"]
        acc["co2_n"] += m["co2_n"]
        acc["co"] = max(acc["co"], m["co_max"])
        acc["fan"] += m["fan_s"]
        acc["heat"] += m["heat_s"]

    segs = [g for m in minutes for g in m["segs"]]
    starts = [datetime.fromisoformat(g["s"]) for g in segs]
    occ_s = out_s = fan_s = heat_s = fan_wh = heat_wh = 0.0
    classes: list[dict] = []
    heater: list[dict] = []
    fan_runs: list[dict] = []
    events: list[dict] = []
    manual_since = alarm_since = None
    alarm_peak = 0.0

    for i, g in enumerate(segs):
        s, l = starts[i], datetime.fromisoformat(g["l"])
        nxt = starts[i + 1] if i + 1 < len(segs) else None
        gap = (nxt - l).total_seconds() if nxt else 0.0
        dt_last = min(gap, MAX_GAP_S)
        dur = g["d"] + dt_last
        end = (l + timedelta(seconds=dt_last)).strftime(FMT)
        fan = g["pwm"] / 255
        heat = 1.0 if g["h"] else 0.0
        occupied = (any(a <= s < b for a, b, _ in timetable) if timetable is not None
                    else g["a"] not in (None, "DISABLED"))

        if occupied:
            occ_s += dur
            out_s += g["o"] + (dt_last if g["lo"] else 0.0)
        if fan > 0:
            fan_s += dur
        if heat > 0:
            heat_s += dur
        fan_wh += fan_max_w * fan ** 3 * dur / 3600
        heat_wh += heater_w * heat * dur / 3600

        if occupied and timetable is None:
            if classes and (s - datetime.fromisoformat(classes[-1]["end"])).total_seconds() <= MAX_GAP_S:
                classes[-1]["end"] = end
            else:
                classes.append({"start": s.strftime(FMT), "end": end})

        level = round(fan * 100)
        if level > 0:
            last = fan_runs[-1] if fan_runs else None
            if last and last["level"] == level and (s - datetime.fromisoformat(last["end"])).total_seconds() <= MAX_GAP_S:
                last["end"] = end
            else:
                fan_runs.append({"start": s.strftime(FMT), "end": end, "level": level})

        if heat > 0:
            if heater and (s - datetime.fromisoformat(heater[-1]["end"])).total_seconds() <= MAX_GAP_S:
                heater[-1]["end"] = end
            else:
                heater.append({"start": s.strftime(FMT), "end": end})

        if g["man"] and manual_since is None:
            manual_since = (s, g["a"], round(fan * 100))
        if not g["man"] and manual_since is not None:
            events.append(_manual_event(manual_since, s))
            manual_since = None
        if g["al"]:
            if alarm_since is None:
                alarm_since, alarm_peak = s, 0.0
            alarm_peak = max(alarm_peak, g["pk"])
        elif alarm_since is not None:
            events.append({"kind": "co", "start": alarm_since.strftime(FMT), "end": s.strftime(FMT),
                           "text": f"Alarma de CO: hasta {alarm_peak:.0f} ppm"})
            alarm_since = None

        if nxt and gap > GAP_EVENT_S:
            events.append({"kind": "gap", "start": l.strftime(FMT), "end": nxt.strftime(FMT), "text": "Sin datos del nodo"})

    if segs:
        last_ts = datetime.fromisoformat(segs[-1]["l"])
        if manual_since is not None:
            events.append(_manual_event(manual_since, last_ts, ongoing=True))
        if alarm_since is not None:
            events.append({"kind": "co", "start": alarm_since.strftime(FMT), "end": None,
                           "text": f"Alarma de CO: hasta {alarm_peak:.0f} ppm"})
    events.sort(key=lambda e: e["start"])

    series = [{
        "t": b.strftime(FMT),
        "temp": round(a["temp"] / a["temp_n"], 2) if a["temp_n"] else None,
        "hum": round(a["hum"] / a["hum_n"], 1) if a["hum_n"] else None,
        "co2": round(a["co2"] / a["co2_n"]) if a["co2_n"] else None,
        "co": round(a["co"], 1),
        "fan": round(100 * a["fan"] / a["n"], 1),
        "heat": round(100 * a["heat"] / a["n"], 1),
    } for b, a in sorted(buckets.items())]

    result = {
        "series": series,
        "classes": classes if timetable is None else [
            {"start": s_.strftime(FMT), "end": e_.strftime(FMT), "people": p} for s_, e_, p in timetable],
        "heater": heater,
        "fan": fan_runs,
        "summary": {
            "class_hours": round(occ_s / 3600, 2),
            "pct_out_of_band": round(100 * out_s / occ_s, 1) if occ_s else None,
            "out_of_band_hours": round(out_s / 3600, 2),
            "fan_hours": round(fan_s / 3600, 2),
            "heater_hours": round(heat_s / 3600, 2),
            "fan_kwh": round(fan_wh / 1000, 3),
            "heater_kwh": round(heat_wh / 1000, 3),
            "manual_orders": sum(1 for e in events if e["kind"] == "manual"),
            "co_alarms": sum(1 for e in events if e["kind"] == "co"),
            "readings": sum(m["n"] for m in minutes),
        },
    }
    return result, ina_energy(minutes)


def ina_energy(minutes: list[dict]) -> float | None:
    """Measured Wh over one sensor's minutes, joined across minute edges (≤ MAX_GAP_S)."""
    total, seen = 0.0, False
    prev: tuple[datetime, float] | None = None
    for m in minutes:
        ina = m["ina"]
        first, last = ina.get("f"), ina.get("l")
        if prev and first and first[1] is not None:
            gap = (datetime.fromisoformat(first[0]) - prev[0]).total_seconds()
            if 0 < gap <= MAX_GAP_S:
                total += (prev[1] + first[1]) * gap / 7_200_000
        total += ina["wh"]
        seen = seen or ina["any"]
        prev = (datetime.fromisoformat(last[0]), last[1]) if last and last[1] is not None else None
    return round(total, 6) if seen else None


def bucket_of(m: datetime, bucket_min: int) -> datetime:
    """The bucket of a minute, as Mongo's $dateTrunc(unit=minute, binSize) computes it."""
    k = int((m - EPOCH_2000).total_seconds() // 60) // bucket_min
    return EPOCH_2000 + timedelta(minutes=k * bucket_min)


def live_aggregates(minutes: Iterable[dict], bucket_min: int) -> tuple[list[dict], dict | None, list[dict]]:
    """Strategy-tab aggregates from rollups of any number of sensors, in the shape of the
    former $facet over raw readings: bucket rows, period totals and per-minute temperature."""
    rows: dict[datetime, dict] = {}
    per_min: dict[datetime, list[float]] = {}
    totals = {"n": 0, "fan3": 0.0, "heat": 0.0, "first": None}
    for m in minutes:
        if not m.get("n"):
            continue
        b = bucket_of(m["m"], bucket_min)
        r = rows.setdefault(b, {"temp_s": 0.0, "temp_n": 0, "fan_s": 0.0, "heat_s": 0.0, "n": 0})
        r["temp_s"] += m["temp_s"]
        r["temp_n"] += m["temp_n"]
        r["fan_s"] += m["fan_s"]
        r["heat_s"] += m["heat_s"]
        r["n"] += m["n"]
        pm = per_min.setdefault(m["m"], [0.0, 0])
        pm[0] += m["temp_s"]
        pm[1] += m["temp_n"]
        totals["n"] += m["n"]
        totals["fan3"] += m["fan3_s"]
        totals["heat"] += m["heat_s"]
        first = m["segs"][0]["s"] if m.get("segs") else m["m"].strftime(FMT)
        if totals["first"] is None or first < totals["first"]:
            totals["first"] = first
    out_rows = [{"_id": {"b": b}, "temp": r["temp_s"] / r["temp_n"] if r["temp_n"] else None,
                 "fan": r["fan_s"] / r["n"], "heat": r["heat_s"] / r["n"]} for b, r in sorted(rows.items())]
    out_min = [{"_id": m, "temp": v[0] / v[1] if v[1] else None} for m, v in sorted(per_min.items())]
    return out_rows, (totals if totals["n"] else None), out_min


# ── Background loop ─────────────────────────────────────────────────────────────

_task = None


async def roll_all(db, sensors: list[str], max_chunks: int = 4) -> int:
    """One pass: roll every sensor forward, a few chunks each so a long backlog never
    blocks the loop. Returns minutes rolled."""
    done = 0
    for sid in sensors:
        for _ in range(max_chunks):
            n = await roll_sensor(db, sid)
            done += n
            if n == 0:
                break
    return done


def start_rollup_loop() -> None:
    """Background loop: keep `reading_minutes` up to date for every registered sensor."""
    from starlette.concurrency import run_in_threadpool
    from app.database import get_db
    from app.database_sql import SessionLocal
    from app.models.admin import SensorDevice

    global _task
    if _task is not None:
        return

    def sensors() -> list[str]:
        s = SessionLocal()
        try:
            return [d.id for d in s.query(SensorDevice.id).all()]
        finally:
            s.close()

    async def _run():
        db = get_db()
        try:
            await db[MINUTES].create_index([("sensor_id", 1), ("m", 1)])
        except Exception:
            logger.exception("Could not create the reading_minutes index")
        while True:
            try:
                await roll_all(db, await run_in_threadpool(sensors))
            except Exception:
                logger.exception("Rolling up readings failed")
            await asyncio.sleep(ROLL_EVERY_S)

    _task = asyncio.get_event_loop().create_task(_run())
