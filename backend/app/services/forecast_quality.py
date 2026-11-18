"""Is the 30-min forecast good enough to drive the fan? And what would it recommend?

The gate scores the predictive controller's own decisions (forecast_log) against what the
room measured HORIZON_MIN later, next to the persistence baseline ("it stays as it is")
on the same pairs. The forecaster may only drive the fan while

    MAE_model + GATE_MARGIN_C < MAE_persistence

over the last GATE_WINDOW_H hours, with at least GATE_MIN_PAIRS pairs of readings the
node declared physical. Otherwise the node's rules keep the room (fallback).

Each forecast_log document carries `temp_now` (the minute mean the forecast started from)
and `data_origin` since this module exists; older documents lack them and are not scored,
so the gate starts closed until the room gathers its own evidence.

Recommendations are explicit rules over the variables the controller really has
(temperature history, the forecast, class timetable, the node's CO alarm). They never
claim a cooling effect: the fan moves air, and whether that lowers the temperature is
exactly what the forecast-vs-measured comparison shows over time.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from statistics import fmean
from typing import Optional

from app.services.forecast_controller import HORIZON_MIN, HOT_C

GATE_WINDOW_H = 24
GATE_MIN_PAIRS = 36            # 3 h of decisions (one every 5 min)
GATE_MARGIN_C = 0.05           # the model must beat persistence by this much, not tie it
PAIR_TOLERANCE_MIN = 2         # a decision ±2 min from t+30 still counts as "30 min later"
SCORED_ORIGINS = ("physical",)


def gate_required() -> bool:
    """PREDICTIVE_GATE=off lets a lab bench drive the fan without validation (never in production)."""
    return os.getenv("PREDICTIVE_GATE", "on").lower() not in ("off", "0", "false", "no")


def score(docs: list[dict]) -> dict:
    """Pair each forecast with the temperature measured HORIZON_MIN later (from a later decision)."""
    by_minute = {}
    for d in docs:
        if d.get("temp_now") is not None and d.get("data_origin") in SCORED_ORIGINS:
            by_minute[int((d["ts"] - datetime(1970, 1, 1)).total_seconds() // 60)] = d
    err, err_base, covered, n_p90 = [], [], 0, 0
    for minute, d in by_minute.items():
        if d.get("forecast_30") is None:
            continue
        later = next((by_minute[m] for m in sorted(range(minute + HORIZON_MIN - PAIR_TOLERANCE_MIN,
                                                          minute + HORIZON_MIN + PAIR_TOLERANCE_MIN + 1),
                                                    key=lambda m: abs(m - minute - HORIZON_MIN))
                      if m in by_minute), None)
        if later is None:
            continue
        actual = later["temp_now"]
        err.append(abs(d["forecast_30"] - actual))
        err_base.append(abs(d["temp_now"] - actual))
        if d.get("forecast_30_p90") is not None:
            n_p90 += 1
            covered += actual <= d["forecast_30_p90"]
    return {
        "pairs": len(err),
        "mae_model": round(fmean(err), 3) if err else None,
        "mae_persistence": round(fmean(err_base), 3) if err_base else None,
        # The logged P90 is the highest P90 over the horizon, not the P90 at t+30: its
        # coverage is an upper bound, shown as such and never used by the gate.
        "p90_peak_coverage": round(covered / n_p90, 3) if n_p90 else None,
    }


def verdict(quality: dict, model: Optional[str]) -> dict:
    """Validated or not, and why, in the user's words."""
    pairs, mae, base = quality["pairs"], quality["mae_model"], quality["mae_persistence"]
    if model in (None, "persistencia"):
        ok, reason = False, "Sin modelo de pronóstico: el servidor usa persistencia, que no anticipa nada."
    elif pairs < GATE_MIN_PAIRS:
        ok, reason = False, f"Datos insuficientes: {pairs} de {GATE_MIN_PAIRS} comparaciones con lecturas físicas en {GATE_WINDOW_H} h."
    elif mae + GATE_MARGIN_C >= base:
        ok, reason = False, f"No supera a la persistencia: error {mae:.2f} °C frente a {base:.2f} °C."
    else:
        ok, reason = True, f"Supera a la persistencia: error {mae:.2f} °C frente a {base:.2f} °C en {pairs} comparaciones."
    if not gate_required() and not ok:
        return {"validated": False, "authorized": True, "reason": reason + " Validación desactivada (PREDICTIVE_GATE=off)."}
    return {"validated": ok, "authorized": ok, "reason": reason}


async def room_quality(db, room_id: str, now: Optional[datetime] = None) -> dict:
    """Gate state for one room over the last GATE_WINDOW_H hours."""
    now = now or datetime.utcnow()
    start = now - timedelta(hours=GATE_WINDOW_H, minutes=HORIZON_MIN + PAIR_TOLERANCE_MIN)
    docs = await db["forecast_log"].find({"room_id": room_id, "ts": {"$gte": start}}, {"_id": 0}).to_list(length=None)
    docs.sort(key=lambda d: d["ts"])
    quality = score(docs)
    model = docs[-1].get("model") if docs else None
    return {"window_h": GATE_WINDOW_H, "min_pairs": GATE_MIN_PAIRS, "margin_c": GATE_MARGIN_C,
            "model": model, **quality, **verdict(quality, model)}


def recommend(*, ready: bool, temp_c: Optional[float], forecast_c: Optional[float],
              p90_c: Optional[float], in_use: Optional[bool], co_alarm: bool,
              validated: bool, sensor_ok: bool = True) -> dict:
    """One interpretable recommendation; `basis` lists the facts it rests on."""
    def out(code: str, text: str, *basis: str) -> dict:
        return {"code": code, "text": text, "basis": [b for b in basis if b]}

    if co_alarm:
        return out("alarma_co", "La alarma de CO manda: el nodo ventila al 100 % sin esperar al modelo.",
                   "Alarma de CO activa en el nodo")
    if not sensor_ok or temp_c is None:
        return out("sin_datos", "No actuar: el nodo no envía temperatura.", "Sin lectura del sensor de temperatura")
    if not ready or forecast_c is None:
        return out("sin_datos", "No actuar: aún no hay historial suficiente para pronosticar.",
                   "Se necesitan 30 min seguidos de lecturas")
    now_txt = f"Ahora {temp_c:.1f} °C"
    fc_txt = f"Pronóstico a {HORIZON_MIN} min: {forecast_c:.1f} °C" + (f" (P90 máx. {p90_c:.1f} °C)" if p90_c is not None else "")
    if not validated:
        return out("baja_confianza", "Mantener las reglas del nodo: el pronóstico aún no demostró superar a la persistencia.",
                   now_txt, fc_txt)
    if not in_use:
        return out("mantener", "Mantener: el aula no tiene clase ahora ni en los próximos 30 min.", now_txt, fc_txt)
    peak = p90_c if p90_c is not None else forecast_c
    if peak >= HOT_C and temp_c < HOT_C:
        return out("aumentar", f"Considerar encender la ventilación: se prevé superar {HOT_C:.1f} °C.", now_txt, fc_txt)
    if temp_c >= HOT_C and peak < HOT_C:
        return out("reducir", f"Considerar reducir la ventilación: se prevé bajar de {HOT_C:.1f} °C.", now_txt, fc_txt)
    return out("mantener", "Mantener la ventilación actual.", now_txt, fc_txt)
