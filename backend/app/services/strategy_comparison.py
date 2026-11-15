"""Node heuristic vs backend AI: coincidence (RF-30) and energy per strategy (RF-31).

Both decisions are persisted on every node reading (RF-17): `node_state` is what the
ESP32 decided and executed; `cognitive_action` is what the AI decided in shadow mode.

Coincidence
    Both decisions are reduced to the question both can answer, "should the room be
    cooled?". The AI does not drive the heater, so the node's HEATING counts as
    "no cooling", like STANDBY. Readings under a CO alarm are excluded: the node
    forces the fan to maximum for safety (RF-24), which is not a thermal decision.
    Readings under a manual order from the dashboard (RF-26) are excluded too: the
    heuristic was not deciding. Their energy still counts, because it was consumed.

Energy (estimated, same time window for both strategies)
    energy = fan power × fan duty × time the decision was in force.
    · Node: the duty it published (`ventilador_pwm`, 0–255) and its heater state.
    · AI:   the duty the node would apply for the AI's decision — ON/PRE-COOLING maps to
            the firmware's PRE_COOLING duty, ON without mode to full speed.
    Each reading's decision lasts until the next reading, capped at MAX_GAP_S so a node
    outage is not counted as running time. When the INA219 is wired (RF-04) the node's
    figure becomes a measurement; until then both are estimates from the same model.
"""
from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Iterable, Optional

# 12 V axial fan, 0.15–0.25 A (PROYECTO_REDEFINIDO §4.1, item 5) → 12 V × 0.2 A.
FAN_POWER_W = 2.4
# 12 V PTC heater, 30–40 W (§4.4). Node only: the AI has no heating decision.
HEATER_POWER_W = 35.0
# Fan duty per action, as in the firmware (dutyDeAccion in hardware/firmware/src/sketch.ino).
DUTY_FULL = 255
DUTY_PRECOOL = 140
# A reading's decision is assumed in force until the next one, at most this long.
MAX_GAP_S = 60.0

COOL, NO_COOL, OFF = "enfriar", "no-enfriar", "apagado"


def node_intent(node_state: dict | None) -> Optional[str]:
    accion = (node_state or {}).get("accion")
    if not accion:
        return None
    if accion in ("COOLING", "PRE_COOLING"):
        return COOL
    if accion == "DISABLED":
        return OFF
    return NO_COOL   # STANDBY, HEATING


def ai_intent(cognitive_action: dict | None) -> Optional[str]:
    status = (cognitive_action or {}).get("ac_status")
    if not status:
        return None
    if status == "ON":
        return COOL
    if status == "DISABLED":
        return OFF
    return NO_COOL


def compare_reading(doc: dict) -> Optional[dict]:
    """Per-reading comparison stored at ingest. None when it does not apply."""
    node = doc.get("node_state")
    if not node or node.get("alarma_co") or node.get("manual"):
        return None
    n, a = node_intent(node), ai_intent(doc.get("cognitive_action"))
    if n is None or a is None:
        return None
    return {"node": n, "ai": a, "match": n == a}


def ai_duty(cognitive_action: dict | None, fan_type: str = "pwm_12v") -> int:
    ca = cognitive_action or {}
    if ca.get("ac_status") != "ON":
        return 0
    if fan_type == "smart_plug":
        return DUTY_FULL
    return DUTY_PRECOOL if ca.get("cooling_mode") == "PRE-COOLING" else DUTY_FULL


def _ts(doc: dict) -> datetime:
    return datetime.fromisoformat(doc["timestamp"])


def summarize(docs: Iterable[dict], max_divergences: int = 20,
              fan_power_w: float | None = FAN_POWER_W, fan_type: str = "pwm_12v") -> dict:
    """Coincidence, divergence detail and energy over readings sorted by time."""
    rows = [d for d in docs if d.get("node_state")]
    rows.sort(key=lambda d: d["timestamp"])
    times = [_ts(d) for d in rows]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    typical = median(gaps) if gaps else 0.0

    compared = matches = alarm = manual = 0
    node_fan_wh = ai_fan_wh = heater_wh = 0.0
    covered_s = 0.0
    pairs: dict[str, int] = {}
    divergences: list[dict] = []
    simulated = 0

    for i, d in enumerate(rows):
        dt = min(gaps[i] if i < len(gaps) else typical, MAX_GAP_S)
        covered_s += dt
        node = d.get("node_state") or {}
        ca = d.get("cognitive_action") or {}
        simulated += bool(d.get("is_simulated"))

        node_fan_wh += (fan_power_w or 0) * (node.get("ventilador_pwm") or 0) / 255 * dt / 3600
        heater_wh += HEATER_POWER_W * (1 if node.get("calefactor") else 0) * dt / 3600
        ai_fan_wh += (fan_power_w or 0) * ai_duty(ca, fan_type) / 255 * dt / 3600

        if node.get("alarma_co"):
            alarm += 1
            continue
        if node.get("manual"):
            manual += 1
            continue
        cmp = d.get("comparison") or compare_reading(d)
        if not cmp:
            continue
        compared += 1
        if cmp["match"]:
            matches += 1
            continue
        key = f"{node.get('accion')}|{ca.get('ac_status')}"
        pairs[key] = pairs.get(key, 0) + 1
        divergences.append({
            "timestamp": d["timestamp"],
            "temperature": d.get("temperature"),
            "co2_ppm": d.get("co2_ppm"),
            "node_action": node.get("accion"),
            "node_reason": node.get("motivo"),
            "ai_status": ca.get("ac_status"),
            "ai_mode": ca.get("cooling_mode"),
            "ai_target": ca.get("target"),
            "expected_occupancy": ca.get("expected_occupancy"),
            "actual_occupancy": ca.get("actual_occupancy"),
        })

    return {
        "readings": len(rows),
        "simulated_readings": simulated,
        "compared": compared,
        "matches": matches,
        "coincidence_pct": round(100 * matches / compared, 1) if compared else None,
        "excluded_co_alarm": alarm,
        "excluded_manual": manual,
        "divergence_pairs": [
            {"node_action": k.split("|")[0], "ai_status": k.split("|")[1], "count": v}
            for k, v in sorted(pairs.items(), key=lambda kv: -kv[1])
        ],
        "divergences": divergences[-max_divergences:][::-1],   # most recent first
        "energy": {
            "hours": round(covered_s / 3600, 3),
            "node_fan_wh": round(node_fan_wh, 3) if fan_power_w is not None else None,
            "ai_fan_wh": round(ai_fan_wh, 3) if fan_power_w is not None else None,
            "node_heater_wh": round(heater_wh, 3),
            "fan_power_w": fan_power_w,
            "heater_power_w": HEATER_POWER_W,
            "method": "estimated",
        },
    }
