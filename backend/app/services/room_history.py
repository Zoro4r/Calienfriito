"""One room's day, for the "Historial" tab: what the node measured and did.

Built from the node readings of one LOCAL calendar day:
  · series      — one point per 5 min: temperature, humidity, CO2, CO (max), fan %, heater %.
  · classes     — intervals with people in the room (MQ-135 above the firmware threshold).
  · summary     — class time outside the ideal range, fan / heater time and energy,
                  manual orders and CO alarms.
  · heater      — exact periods with the heater on (it is on/off, no power level).
Manual orders and CO alarms are counted from their periods in the readings.
Each reading's state lasts until the next one, at most MAX_GAP_S.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.config import LOCAL_UTC_OFFSET_HOURS
from app.services.learning_controller import BAND_HI, BAND_LO, NODE_FAN_MAX_W, NODE_HEATER_W

OFFSET = timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
BUCKET_MIN = 5
MAX_GAP_S = 60.0
GAP_EVENT_S = 300.0                 # a silence this long is reported as "sin datos"
NODE_OCCUPIED_ADC = 2200
FMT = "%Y-%m-%dT%H:%M:%S"

PROJECTION = {
    "_id": 0, "timestamp": 1, "temperature": 1, "humidity": 1, "co2_ppm": 1, "co_ppm": 1,
    "node_state.accion": 1, "node_state.ventilador_pwm": 1, "node_state.calefactor": 1,
    "node_state.co2_proxy": 1, "node_state.alarma_co": 1, "node_state.manual": 1,
    "node_state.motivo": 1,
}


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a local day, in naive UTC."""
    start = datetime.combine(day, time()) - OFFSET
    return start, start + timedelta(days=1)


def _ts(doc: dict) -> datetime:
    return datetime.fromisoformat(doc["timestamp"][:19])


def summarize_day(docs: list[dict], fan_max_w: float, heater_w: float) -> dict:
    """Series, classes, heater periods and summary from one day's readings (sorted)."""
    buckets: dict[datetime, dict] = {}
    occ_s = out_s = fan_s = heat_s = fan_wh = heat_wh = 0.0
    classes: list[dict] = []
    heater: list[dict] = []           # exact periods with the heater on (it is on/off, no power level)
    fan_runs: list[dict] = []         # exact periods with the fan on, each at one power level (%)
    events: list[dict] = []
    manual_since = alarm_since = None
    alarm_peak = 0.0

    for i, d in enumerate(docs):
        ts = _ts(d)
        ns = d.get("node_state") or {}
        nxt = _ts(docs[i + 1]) if i + 1 < len(docs) else None
        gap = (nxt - ts).total_seconds() if nxt else 0.0
        dt = min(gap, MAX_GAP_S)
        fan = (ns.get("ventilador_pwm") or 0) / 255
        heat = 1.0 if ns.get("calefactor") else 0.0
        occupied = (ns.get("co2_proxy") or 0) >= NODE_OCCUPIED_ADC
        temp = d.get("temperature")

        # 5-minute series
        b = ts.replace(minute=ts.minute - ts.minute % BUCKET_MIN, second=0, microsecond=0)
        acc = buckets.setdefault(b, {"n": 0, "temp": 0.0, "temp_n": 0, "hum": 0.0, "hum_n": 0, "co2": 0.0, "co2_n": 0, "co": 0.0, "fan": 0.0, "heat": 0.0})
        acc["n"] += 1
        # Readings without DHT data (None) don't pull the averages towards 0.
        if temp is not None:
            acc["temp"] += temp
            acc["temp_n"] += 1
        if d.get("humidity") is not None:
            acc["hum"] += d["humidity"]
            acc["hum_n"] += 1
        if d.get("co2_ppm") is not None:
            acc["co2"] += d["co2_ppm"]
            acc["co2_n"] += 1
        acc["co"] = max(acc["co"], d.get("co_ppm") or 0.0)
        acc["fan"] += fan
        acc["heat"] += heat

        # time and energy
        if occupied:
            occ_s += dt
            if temp is not None and (temp < BAND_LO or temp > BAND_HI):
                out_s += dt
        if fan > 0:
            fan_s += dt
        if heat > 0:
            heat_s += dt
        fan_wh += fan_max_w * fan ** 3 * dt / 3600
        heat_wh += heater_w * heat * dt / 3600

        # classes (contiguous occupied readings)
        if occupied:
            if classes and (ts - datetime.fromisoformat(classes[-1]["end"])).total_seconds() <= MAX_GAP_S:
                classes[-1]["end"] = (ts + timedelta(seconds=dt)).strftime(FMT)
            else:
                classes.append({"start": ts.strftime(FMT), "end": (ts + timedelta(seconds=dt)).strftime(FMT)})

        # fan periods: a new one whenever the power level changes
        level = round(fan * 100)
        if level > 0:
            last = fan_runs[-1] if fan_runs else None
            if last and last["level"] == level and (ts - datetime.fromisoformat(last["end"])).total_seconds() <= MAX_GAP_S:
                last["end"] = (ts + timedelta(seconds=dt)).strftime(FMT)
            else:
                fan_runs.append({"start": ts.strftime(FMT), "end": (ts + timedelta(seconds=dt)).strftime(FMT), "level": level})

        # heater periods
        if heat > 0:
            if heater and (ts - datetime.fromisoformat(heater[-1]["end"])).total_seconds() <= MAX_GAP_S:
                heater[-1]["end"] = (ts + timedelta(seconds=dt)).strftime(FMT)
            else:
                heater.append({"start": ts.strftime(FMT), "end": (ts + timedelta(seconds=dt)).strftime(FMT)})

        # manual orders and CO alarms, as periods
        if ns.get("manual") and manual_since is None:
            manual_since = (ts, ns.get("accion"), round(fan * 100))
        if not ns.get("manual") and manual_since is not None:
            events.append(_manual_event(manual_since, ts))
            manual_since = None
        if ns.get("alarma_co"):
            if alarm_since is None:
                alarm_since, alarm_peak = ts, 0.0
            alarm_peak = max(alarm_peak, d.get("co_ppm") or 0.0)
        elif alarm_since is not None:
            events.append({"kind": "co", "start": alarm_since.strftime(FMT), "end": ts.strftime(FMT),
                           "text": f"Alarma de CO: hasta {alarm_peak:.0f} ppm"})
            alarm_since = None

        # silences
        if nxt and gap > GAP_EVENT_S:
            events.append({"kind": "gap", "start": ts.strftime(FMT), "end": nxt.strftime(FMT), "text": "Sin datos del nodo"})


    if docs:
        last = _ts(docs[-1])
        if manual_since is not None:
            events.append(_manual_event(manual_since, last, ongoing=True))
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

    return {
        "series": series,
        "classes": classes,
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
            "readings": len(docs),
        },
    }


def _manual_event(since: tuple, until: datetime, ongoing: bool = False) -> dict:
    ts, accion, fan_pct = since
    what = {"COOLING": f"Ventilar al {fan_pct} %", "HEATING": "Calentar", "STANDBY": "Apagar ambos"}.get(accion, "Orden manual")
    return {"kind": "manual", "start": ts.strftime(FMT), "end": None if ongoing else until.strftime(FMT),
            "text": f"Orden manual: {what}"}


def equipment_for(meta: dict | None, start: datetime, end: datetime) -> dict:
    """The simulated scenario's equipment when the day falls inside it; else the node's."""
    if meta and meta.get("start") and meta["start"] < end and meta["end"] >= start:
        return meta["equipment"]
    return {"label": "Actuadores del nodo", "fan_max_w": NODE_FAN_MAX_W, "heater_w": NODE_HEATER_W}
