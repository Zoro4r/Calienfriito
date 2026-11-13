"""
Simulated comparison of two control strategies for one classroom:

  A · Rules     — the firmware heuristic (hardware/firmware/src/sketch.ino,
                  decidirHeuristica): fan 100 % at T >= 24.5 °C, heater at
                  T <= 23.5 °C, off at 24.0 °C, 60 s minimum between switches,
                  everything off when the room is empty.
  B · Predictive — the backend's ForecastController: every 5 min a pretrained model
                  (Amazon Chronos-Bolt, zero-shot) forecasts the next 30 min of indoor
                  temperature; when a class is on or about to start and the high forecast
                  (p90) crosses 24.5 °C, the fan starts early. Otherwise the rules decide.

Both strategies face exactly the same simulated days: same people, same outdoor
temperature, same sensor noise. The simulated room reacts to what each strategy
does, so the comparison is fair.

The predictive strategy does not know the room equations below: it only sees the
measured indoor temperature (with sensor noise) and the room's timetable. Both
strategies are scored on the same days (after WARMUP_DAYS, kept for comparability with
earlier runs). Success metric: minutes of class above 24.5 °C.

Usage:
    python ml/simulation/compare_strategies.py            # default 30 days
    python ml/simulation/compare_strategies.py --days 60 --seed 3
Outputs go to ml/simulation/results/.
"""
from __future__ import annotations

import argparse
import sys
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

# The predictive strategy is the backend's controller, imported as is.
try:
    from app.services.forecast_controller import HORIZON_MIN, ForecastController, default_forecaster, persistence
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    from app.services.forecast_controller import HORIZON_MIN, ForecastController, default_forecaster, persistence  # noqa: E402

STEP_S = 30                      # simulation step
STEPS_PER_MIN = 60 // STEP_S
SETPOINT, BAND = 24.0, 0.5       # firmware SETPOINT_C, HYSTERESIS_C
BAND_LO, BAND_HI = SETPOINT - BAND, SETPOINT + BAND
COMFORT_LO, COMFORT_HI = 22.0, 25.0   # wider comfort range (ASHRAE 55 / ISO 7730, light clothing)
CO2_OUT = 420.0
WARMUP_DAYS = 3


# ── The simulated room (unknown to the learning strategy) ─────────────────────
@dataclass
class Room:
    volume_m3: float = 190.0            # ~ 9 × 7 × 3 m classroom (A403, capacity 46)
    capacity: int = 46
    heat_capacity_j_k: float = 4.0e6    # air + furniture + inner surfaces of concrete walls
    ua_w_k: float = 450.0               # envelope + doors/windows + infiltration heat exchange
    infiltration_m3s: float = 0.08      # ~1.5 air changes per hour (doors open between classes)
    person_sensible_w: float = 75.0     # seated adult, sensible heat (ASHRAE Fundamentals)
    person_co2_m3s: float = 5.2e-6      # seated adult, ~0.0052 L/s CO2
    equipment_w: float = 400.0          # lights + projector while occupied
    solar_peak_w: float = 300.0


@dataclass
class Actuators:
    label: str
    fan_max_m3s: float      # airflow at 100 %
    fan_max_w: float        # electric power at 100 % (fan laws: power ∝ speed³)
    heater_w: float         # resistive heater: electric power = heat


SIZED = Actuators("Equipo dimensionado para el aula (extractor 1800 m3/h, 180 W; calefactor 2 kW)", 0.5, 180.0, 2000.0)
NODE = Actuators("Actuadores del nodo actual (ventilador 12 V 2.4 W; PTC 35 W)", 0.04, 2.4, 35.0)


def outdoor_temp(day_offset: float, hour: float) -> float:
    """Lima in spring (Oct–Nov): about 16.5 °C before dawn, 21.5 °C mid-afternoon."""
    return 19.0 + day_offset + 2.5 * math.sin(2 * math.pi * (hour - 8) / 24)


def solar_gain(room: Room, hour: float) -> float:
    return room.solar_peak_w * max(0.0, math.sin(math.pi * (hour - 6) / 12)) if 6 <= hour <= 18 else 0.0


def make_scenario(days: int, room: Room, seed: int) -> dict:
    """People per step, outdoor temperature per step and sensor noise, shared by both strategies."""
    rng = np.random.default_rng(seed)
    n = days * 24 * 3600 // STEP_S
    people = np.zeros(n)
    expected = np.zeros(n)       # timetable: group size during the whole class (what the controller knows)
    tout = np.zeros(n)
    blocks = [(7, 9), (9, 11), (11, 13), (14, 16), (16, 18), (18, 20), (20, 22)]
    for d in range(days):
        offset = rng.normal(0, 1.0)
        base = d * 24 * 3600 // STEP_S
        for i in range(24 * 3600 // STEP_S):
            tout[base + i] = outdoor_temp(offset, i * STEP_S / 3600)
        if d % 7 >= 5:          # weekend: empty
            continue
        for start, end in blocks:
            if rng.random() > 0.7:
                continue
            group = int(rng.integers(8, room.capacity + 1))
            count = int(round(group * rng.uniform(0.6, 1.0)))      # actual attendance
            s, e = base + start * 3600 // STEP_S, base + end * 3600 // STEP_S
            ramp_in, ramp_out = 10 * STEPS_PER_MIN, 5 * STEPS_PER_MIN
            expected[s:e] = group
            for i in range(s, e):
                k = min(1.0, (i - s + 1) / ramp_in, (e - i) / ramp_out)
                people[i] = round(count * k)
    return {
        "people": people,
        "expected": expected,
        "tout": tout,
        "t_noise": rng.normal(0, 0.15, n),     # DHT22: ±0.5 °C accuracy, 0.1 °C resolution
        "co2_noise": rng.normal(0, 25, n),
    }


# ── Strategy A: the firmware rule ─────────────────────────────────────────────
# Proportional fan when cooling (firmware T_NIVEL_75_C, T_NIVEL_100_C, CO2_NIVEL_100_PPM, BANDA_NIVEL_C).
LEVEL_75_C, LEVEL_100_C, LEVEL_CO2_PPM, LEVEL_BAND_C = 25.0, 25.5, 1500.0, 0.2


def level_for_load(t: float, co2: float) -> int:
    if t >= LEVEL_100_C or co2 >= LEVEL_CO2_PPM:
        return 100
    return 75 if t >= LEVEL_75_C else 50


class Rules:
    name = "Reglas (firmware)"

    def __init__(self) -> None:
        self.action = "DISABLED"
        self.last_change_s = -10**9
        self.changed_once = False
        self.level = 50

    def decide(self, t_meas: float, occupied: bool, now_s: int, co2_meas: float = 0.0, **_) -> tuple[float, float]:
        a = self.action
        # Fan level first, as the firmware does (actualizarNivelReglas before decidirHeuristica).
        up, down = level_for_load(t_meas, co2_meas), level_for_load(t_meas + LEVEL_BAND_C, co2_meas)
        if a != "COOLING":
            self.level = up
        elif up > self.level:
            self.level = up
        elif down < self.level:
            self.level = down
        if not occupied:
            prop = "DISABLED"
        elif t_meas >= BAND_HI:
            prop = "COOLING"
        elif t_meas <= BAND_LO:
            prop = "HEATING"
        elif a == "COOLING" and t_meas <= SETPOINT:
            prop = "STANDBY"
        elif a == "HEATING" and t_meas >= SETPOINT:
            prop = "STANDBY"
        elif a == "DISABLED":
            prop = "STANDBY"
        else:
            prop = a
        changes_actuator = (prop == "COOLING") != (a == "COOLING") or (prop == "HEATING") != (a == "HEATING")
        if prop != a and changes_actuator:
            if self.changed_once and now_s - self.last_change_s < 60:
                prop = a
            else:
                self.last_change_s, self.changed_once = now_s, True
        self.action = prop
        return (self.level / 100 if prop == "COOLING" else 0.0, 1.0 if prop == "HEATING" else 0.0)


# ── Strategy B: predictive (pretrained forecaster) ────────────────────────────
class Predictive:
    """The backend's ForecastController (the same class that runs live). When it asks for
    the fan, the fan runs at that level; otherwise the rules act, exactly as live, where
    an empty decision hands the node back to its rules ("auto")."""
    name = "Control predictivo"

    def __init__(self, act: "Actuators", expected: np.ndarray | None = None, forecaster=None) -> None:
        self.ctrl = ForecastController(forecaster or default_forecaster())
        self.rules = Rules()
        self.expected = expected        # timetable per step (expected people); None = unknown
        self.preds: list[tuple[int, float, float]] = []   # (target step, forecast, temp when forecast)

    def in_use(self, i: int, occupied: bool) -> bool:
        if occupied or self.expected is None:
            return occupied
        return bool(np.any(self.expected[i:i + HORIZON_MIN * STEPS_PER_MIN] > 0))

    def decide(self, t_meas: float, occupied: bool, now_s: int, tout: float, co2_meas: float) -> tuple[float, float]:
        i = now_s // STEP_S
        decided_before = self.ctrl.last_decision
        self.ctrl.step(now_s, t_meas, self.in_use(i, occupied))
        if self.ctrl.last_decision != decided_before and self.ctrl.forecast_c is not None:
            self.preds.append((i + HORIZON_MIN * STEPS_PER_MIN, self.ctrl.forecast_c, self.ctrl.history[-1]))
        rules_action = self.rules.decide(t_meas, occupied, now_s, co2_meas=co2_meas)
        if self.ctrl.fan_pct > 0:
            return self.ctrl.fan_pct / 100, 0.0
        return rules_action


Learning = Predictive   # name kept for generate_month.py


# ── Simulation loop ───────────────────────────────────────────────────────────
def simulate(strategy, sc: dict, room: Room, act: Actuators, days: int) -> dict:
    n = len(sc["people"])
    temp, co2 = 22.0, CO2_OUT
    log = {k: np.zeros(n) for k in ("t", "t_meas", "co2", "fan", "heat")}
    for i in range(n):
        hour = (i * STEP_S / 3600) % 24
        people, tout = sc["people"][i], sc["tout"][i]
        t_meas = round(temp + sc["t_noise"][i], 1)
        co2_meas = co2 + sc["co2_noise"][i]
        fan, heat = strategy.decide(t_meas=t_meas, occupied=people > 0, now_s=i * STEP_S, tout=tout, co2_meas=co2_meas)
        flow = room.infiltration_m3s + fan * act.fan_max_m3s
        gains = people * room.person_sensible_w + (room.equipment_w if people > 0 else 0.0) + solar_gain(room, hour) + heat * act.heater_w
        losses = (room.ua_w_k + 1.2 * 1005 * fan * act.fan_max_m3s) * (temp - tout)
        temp += (gains - losses) * STEP_S / room.heat_capacity_j_k
        co2 += (people * room.person_co2_m3s * 1e6 / room.volume_m3 - flow / room.volume_m3 * (co2 - CO2_OUT)) * STEP_S
        log["t"][i], log["fan"][i], log["heat"][i] = temp, fan, heat
        log["t_meas"][i], log["co2"][i] = t_meas, co2_meas
    return log


def metrics(log: dict, sc: dict, act: Actuators, from_step: int, to_step: int) -> dict:
    sl = slice(from_step, to_step)
    occ = sc["people"][sl] > 0
    t = log["t"][sl]
    minutes = STEP_S / 60
    out_band = (t < BAND_LO) | (t > BAND_HI)
    out_comfort = (t < COMFORT_LO) | (t > COMFORT_HI)
    deg = np.maximum(0, t - BAND_HI) + np.maximum(0, BAND_LO - t)
    fan_wh = float(np.sum(act.fan_max_w * log["fan"][sl] ** 3) * STEP_S / 3600)
    heat_wh = float(np.sum(act.heater_w * log["heat"][sl]) * STEP_S / 3600)
    switches = int(np.sum(np.diff((log["fan"][sl] > 0).astype(int)) != 0) + np.sum(np.diff((log["heat"][sl] > 0).astype(int)) != 0))
    occ_min = float(np.sum(occ) * minutes)
    hot = t > BAND_HI
    return {
        "occupied_hours": round(occ_min / 60, 1),
        "min_hot_class": round(float(np.sum(hot & occ)) * minutes, 0),
        "pct_time_hot": round(100 * float(np.sum(hot & occ)) * minutes / occ_min, 1),
        "pct_time_out_of_band": round(100 * float(np.sum(out_band & occ)) * minutes / occ_min, 1),
        "min_out_of_comfort_22_25": round(float(np.sum(out_comfort & occ)) * minutes, 0),
        "degree_minutes_out_of_band": round(float(np.sum(deg[occ])) * minutes, 1),
        "max_temp_occupied": round(float(np.max(t[occ])), 2),
        "min_temp_occupied": round(float(np.min(t[occ])), 2),
        "fan_kwh": round(fan_wh / 1000, 3),
        "heater_kwh": round(heat_wh / 1000, 3),
        "total_kwh": round((fan_wh + heat_wh) / 1000, 3),
        "on_off_switches": switches,
    }


def run(act: Actuators, days: int, seed: int, out: Path, tag: str) -> dict:
    room = Room()
    sc = make_scenario(days, room, seed)
    warm_steps = WARMUP_DAYS * 24 * 3600 // STEP_S
    rules_log = simulate(Rules(), sc, room, act, days)
    pred = Predictive(act, expected=sc["expected"])
    learn_log = simulate(pred, sc, room, act, days)
    n = len(sc["people"])
    result = {
        "actuators": asdict(act),
        "days_simulated": days,
        "warmup_days": WARMUP_DAYS,
        "evaluated_days": days - WARMUP_DAYS,
        "rules": metrics(rules_log, sc, act, warm_steps, n),
        "learning": metrics(learn_log, sc, act, warm_steps, n),
        "forecast": forecast_error(pred.preds, learn_log, warm_steps, n),
        "per_day": [],
    }
    day_steps = 24 * 3600 // STEP_S
    for d in range(WARMUP_DAYS, days):
        s, e = d * day_steps, (d + 1) * day_steps
        if not np.any(sc["people"][s:e] > 0):
            continue
        result["per_day"].append({
            "day": d + 1,
            "rules": metrics(rules_log, sc, act, s, e),
            "learning": metrics(learn_log, sc, act, s, e),
        })
    plot(sc, rules_log, learn_log, result, out / f"{tag}.png", act)
    return result


def forecast_error(preds, log, from_step: int, to_step: int) -> dict:
    """Mean absolute error of the 30-min forecasts against the measured temperature, next
    to the persistence baseline ("it stays as it is") on the same pairs."""
    pairs = [(p, now, log["t_meas"][k]) for k, p, now in preds if from_step <= k < to_step]
    if not pairs:
        return {"pairs": 0, "mae": None, "mae_baseline": None}
    return {
        "horizon_min": HORIZON_MIN,
        "model": getattr(default_forecaster(), "model_name", "persistencia"),
        "pairs": len(pairs),
        "mae": round(float(np.mean([abs(p - a) for p, _, a in pairs])), 3),
        "mae_baseline": round(float(np.mean([abs(n - a) for _, n, a in pairs])), 3),
    }


def plot(sc, rules_log, learn_log, result, path: Path, act: Actuators) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    day_steps = 24 * 3600 // STEP_S
    # Busiest evaluated weekday as the example day.
    busiest = max(result["per_day"], key=lambda d: d["rules"]["occupied_hours"])["day"] - 1
    s, e = busiest * day_steps, (busiest + 1) * day_steps
    hours = np.arange(e - s) * STEP_S / 3600
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1.2, 1.2]})
    ax1.axhspan(BAND_LO, BAND_HI, color="#34d399", alpha=0.12, label="Banda 23.5–24.5 °C")
    ax1.plot(hours, sc["tout"][s:e], color="#94a3b8", lw=1, ls="--", label="Exterior")
    ax1.plot(hours, rules_log["t"][s:e], color="#64748b", lw=1.6, label="Reglas")
    ax1.plot(hours, learn_log["t"][s:e], color="#0ea5e9", lw=1.6, label="Control predictivo")
    ax1.set_ylabel("°C")
    ax1.legend(loc="upper left", fontsize=8, ncol=4)
    ax1.set_title(f"Día {busiest + 1} · {act.label}", fontsize=10)
    ax2.fill_between(hours, sc["people"][s:e], color="#cbd5e1")
    ax2.set_ylabel("Personas")
    ax3.step(hours, rules_log["fan"][s:e] * 100, color="#64748b", lw=1, where="post", label="Ventilador · reglas")
    ax3.step(hours, learn_log["fan"][s:e] * 100, color="#0ea5e9", lw=1, where="post", label="Ventilador · predictivo")
    ax3.step(hours, -rules_log["heat"][s:e] * 100, color="#fdba74", lw=1, where="post", label="Calefactor · reglas (neg.)")
    ax3.set_ylabel("%")
    ax3.set_xlabel("Hora")
    ax3.legend(loc="lower left", fontsize=7, ncol=2)
    for ax in (ax1, ax2, ax3):
        ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    results = {
        "sized_equipment": run(SIZED, args.days, args.seed, out, "equipo_dimensionado"),
        "current_node": run(NODE, args.days, args.seed, out, "nodo_actual"),
    }
    (out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    for key, r in results.items():
        print(f"\n== {r['actuators']['label']} · {r['evaluated_days']} días evaluados ==")
        print(f"{'':32}{'Reglas':>12}{'Predictivo':>14}")
        for k in ("min_hot_class", "pct_time_hot", "pct_time_out_of_band", "min_out_of_comfort_22_25", "degree_minutes_out_of_band",
                  "max_temp_occupied", "min_temp_occupied", "fan_kwh", "heater_kwh", "total_kwh", "on_off_switches"):
            print(f"{k:32}{r['rules'][k]:>12}{r['learning'][k]:>14}")
        print("forecast:", r["forecast"])


if __name__ == "__main__":
    main()
