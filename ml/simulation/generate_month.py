"""
Generate a month of simulated data for one room with both control strategies.

Same people, same outdoor temperature, same sensor noise for both:
  · Rules (firmware heuristic) → written to `sensor_readings` as the room's node
    telemetry, so every view (En vivo, Historial, Resumen) shows the month.
  · Rules and learning         → written to `strategy_simulation` (one document per
    strategy and step), the counterfactual pair the "Estrategias" tab compares.
A `strategy_simulation_meta` document records the scenario (equipment, step, period,
timetable) so the dashboard can say what it is showing.

Everything is tagged as simulated (`is_simulated`, `demo_seed`) and can be removed with:
    db.sensor_readings.deleteMany({sensor_id: "s-A101", demo_seed: true})
    db.strategy_simulation.deleteMany({room_id: "A101"})
    db.strategy_simulation_meta.deleteOne({_id: "A101"})

Occupancy: the room's weekly classes from the database (Monday–Saturday, the same slot
every week), with attendance varying 60–100 % of the expected group. Outdoor temperature: the backend's Lima model
(app.services.climate_service) plus a day-to-day offset.

Usage (inside the backend container, with this folder copied next to it):
    PYTHONPATH=/app python generate_month.py --room A101 --days 30 --every 4   # one row per 2 min
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, time

import numpy as np
from pymongo import MongoClient

import compare_strategies as cs
from app.config import LOCAL_UTC_OFFSET_HOURS, MONGO_URI
from app.services.climate_service import outdoor_temp

STEP = cs.STEP_S
STEPS_PER_DAY = 24 * 3600 // STEP

def timetable(db_rows: list[tuple[int, time, time, int]]) -> list[tuple[int, float, float, int]]:
    """The room's weekly classes as (weekday, start hour, end hour, group size)."""
    return [(d, s.hour + s.minute / 60, e.hour + e.minute / 60, n) for d, s, e, n in db_rows]


def room_schedules(room_id: str) -> list[tuple[int, time, time, int]]:
    try:
        from app.database_sql import SessionLocal
        from app.models.admin import Schedule
    except ImportError:
        return []
    db = SessionLocal()
    try:
        return [(s.day_of_week, s.start_time, s.end_time, s.expected_people)
                for s in db.query(Schedule).filter(Schedule.room_id == room_id).all()]
    finally:
        db.close()


def build_scenario(start_local: datetime, n: int, room: cs.Room, rows, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    people = np.zeros(n)
    expected = np.zeros(n)            # timetable: group size for the whole class (what the controller knows)
    tout = np.zeros(n)
    days = n // STEPS_PER_DAY + 1
    offsets = rng.normal(0, 0.8, days)
    for i in range(n):
        ts = start_local + timedelta(seconds=i * STEP)
        tout[i] = outdoor_temp(ts) + offsets[i // STEPS_PER_DAY]
    for d in range(days):
        day = start_local + timedelta(days=d)
        base = d * STEPS_PER_DAY
        for weekday, start_h, end_h, group in rows:
            if day.weekday() != weekday:
                continue
            if rng.random() < 0.05:           # 5 % of sessions cancelled: scheduled, nobody comes
                s, e = base + int(start_h * 3600 // STEP), base + int(end_h * 3600 // STEP)
                expected[max(0, s):min(n, e)] = np.maximum(expected[max(0, s):min(n, e)], group)
                continue
            count = min(room.capacity, int(round(group * rng.uniform(0.6, 1.0))))
            s, e = base + int(start_h * 3600 // STEP), base + int(end_h * 3600 // STEP)
            ramp_in, ramp_out = 10 * cs.STEPS_PER_MIN, 5 * cs.STEPS_PER_MIN
            expected[max(0, s):min(n, e)] = np.maximum(expected[max(0, s):min(n, e)], group)
            for i in range(max(0, s), min(n, e)):
                k = min(1.0, (i - s + 1) / ramp_in, (e - i) / ramp_out)
                people[i] = max(people[i], round(count * k))
    return {"people": people, "expected": expected, "tout": tout,
            "t_noise": rng.normal(0, 0.15, n), "co2_noise": rng.normal(0, 25, n)}


def node_state(fan: float, heat: float, occupied: bool, t: float, rng: np.random.Generator) -> dict:
    if fan > 0:
        accion, motivo, led = "COOLING", f"Aula ocupada y T >= 24.5 C: ventilador ON (ventilador al {round(fan * 100)} %)", "AZUL (enfriando)"
    elif heat > 0:
        accion, motivo, led = "HEATING", "Aula ocupada y T <= 23.5 C: calefactor ON", "AMBAR (calentando)"
    elif occupied:
        accion, motivo, led = "STANDBY", "23.5 C < T < 24.5 C: se mantiene el estado", "VERDE (en espera)"
    else:
        accion, motivo, led = "DISABLED", "Aula vacia (MQ-135 < 2200)", "APAGADO"
    return {
        "accion": accion, "alarma_co": False, "ventilador_pwm": int(round(fan * 255)),
        "calefactor": bool(heat > 0), "buzzer": False, "led": led, "motivo": motivo,
        "bloqueo_s": 0, "pendiente": "", "manual": False, "manual_restante_s": 0,
        "co2_proxy": int((2900 if occupied else 1500) + rng.normal(0, 60)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--every", type=int, default=1,
                        help="write one row every N simulation steps (the simulation itself keeps its step)")
    args = parser.parse_args()
    room_id, sensor_id = args.room, f"s-{args.room}"

    room, act = cs.Room(), cs.SIZED
    offset = timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
    now_utc = datetime.utcnow().replace(microsecond=0)
    now_local = now_utc + offset
    start_local = datetime.combine((now_local - timedelta(days=args.days)).date(), time(0, 0))
    n = int((now_local - start_local).total_seconds() // STEP)
    rows = timetable(room_schedules(room_id))
    if not rows:
        raise SystemExit(f"{room_id} has no classes in the database: run ml/seed-data/generate_dataset.py first")
    sc = build_scenario(start_local, n, room, rows, args.seed)

    warm_s = cs.WARMUP_DAYS * 24 * 3600
    rules_log = cs.simulate(cs.Rules(), sc, room, act, args.days)
    learn_log = cs.simulate(cs.Predictive(act, expected=sc["expected"]), sc, room, act, args.days)

    db = MongoClient(MONGO_URI)["climate_db"]
    db["sensor_readings"].delete_many({"sensor_id": sensor_id, "demo_seed": True})
    db["strategy_simulation"].delete_many({"room_id": room_id})
    db["learning_state"].delete_many({"_id": room_id})
    db["strategy_simulation"].create_index([("room_id", 1), ("strategy", 1), ("ts", 1)])

    rng = np.random.default_rng(args.seed + 1)
    readings, sims = [], []
    for i in range(0, n, args.every):
        ts_utc = start_local - offset + timedelta(seconds=i * STEP)
        occupied = bool(sc["people"][i] > 0)
        hum = float(np.clip(52 + 0.25 * sc["people"][i] + rng.normal(0, 1.5), 35, 75))
        readings.append({
            "sensor_id": sensor_id,
            "temperature": float(rules_log["t_meas"][i]),
            "humidity": round(hum, 1),
            "co2_ppm": round(float(rules_log["co2"][i]), 1),
            "co_ppm": round(max(0.0, 2.5 + rng.normal(0, 0.6)), 1),
            "camera_occupancy": None,
            "is_simulated": True,
            "timestamp": ts_utc.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "node_state": node_state(rules_log["fan"][i], rules_log["heat"][i], occupied, rules_log["t_meas"][i], rng),
            "demo_seed": True,
        })
        for strategy, log in (("rules", rules_log), ("learning", learn_log)):
            sims.append({
                "room_id": room_id, "strategy": strategy, "ts": ts_utc,
                "temperature": float(log["t_meas"][i]),
                "fan": round(float(log["fan"][i]), 3), "heat": round(float(log["heat"][i]), 3),
                "occupied": occupied, "people": int(sc["people"][i]),
            })
        if len(readings) >= 5000:
            db["sensor_readings"].insert_many(readings)
            db["strategy_simulation"].insert_many(sims)
            readings, sims = [], []
    if readings:
        db["sensor_readings"].insert_many(readings)
        db["strategy_simulation"].insert_many(sims)

    db["strategy_simulation_meta"].replace_one({"_id": room_id}, {
        "_id": room_id,
        "generated_at": now_utc,
        "start": start_local - offset,
        "end": start_local - offset + timedelta(seconds=(n - 1) * STEP),
        "step_s": STEP * args.every,
        "warmup_days": cs.WARMUP_DAYS,
        "equipment": {"label": act.label, "fan_max_m3s": act.fan_max_m3s, "fan_max_w": act.fan_max_w, "heater_w": act.heater_w},
        "timetable": "Horario de clases del aula (lunes a sábado, base de datos)",
        "seed": args.seed,
    }, upsert=True)

    w = cs.WARMUP_DAYS * STEPS_PER_DAY
    for name, log in (("Reglas", rules_log), ("Aprendizaje", learn_log)):
        m = cs.metrics(log, sc, act, w, n)
        print(f"{name:12} fuera de banda {m['pct_time_out_of_band']:5} %   energía {m['total_kwh']:7} kWh")
    print(f"{n} pasos de {STEP} s desde {start_local} (hora local) para {room_id}")


if __name__ == "__main__":
    main()
