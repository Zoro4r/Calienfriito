"""
Predictive service — dual-mode thermal load engine.

Occupancy model (feed-forward + feedback):
  - expected_occupancy → PLANNED headcount from Schedule/Reservation. Feed-forward
    signal: drives anticipatory pre-cooling, the only value known before arrival.
  - actual_occupancy   → REALIZED headcount from the ESP32-CAM when available,
    otherwise inferred from CO2 (occupancy_service). Feedback signal: trims the
    demand once the room actually fills (or doesn't).
  The engine blends both into an effective_occupancy via FEEDBACK_WEIGHT.

Operates in two modes:
  - ML mode: loads a scikit-learn BUNDLE (model + feature contract + room_id map +
    metadata). Feature values are assembled by name from bundle["features"], so the
    row always matches the trained order. Available: temperature, hour_of_day,
    expected_occupancy, actual_occupancy, outdoor_temp (climate_service, Tier 2),
    floor/volume_m3/ac_btu (room_profile, Tier 3), plus room_code for legacy bundles.
    outdoor temp and room metadata need no hardware.
  - Heuristic mode (fallback): estimates thermal load as
    effective_occupancy × THERMAL_LOAD_PER_PERSON when no model.joblib is present.

The output drives the AC cognitive action: if the current temperature exceeds
the occupancy-adjusted target, the system emits ac_status=ON (PRE-COOLING);
otherwise it emits STANDBY, avoiding unnecessary energy consumption.
"""
import logging
from datetime import datetime
from pathlib import Path

import numpy as np

from app.core.timeutils import to_local
from app.services.occupancy_service import estimate_actual_occupancy, occupancy_gap
from app.services.occupancy_service import fuse_occupancy
from app.config import OCCUPANCY_FUSION_MODE, OCCUPANCY_VISUAL_WEIGHT
from app.services.climate_service import outdoor_temp as _outdoor_temp
from app.services.room_profile import get_metadata as _room_metadata

logger = logging.getLogger(__name__)

_MODEL_PATH = Path(__file__).resolve().parent.parent / "ml" / "model.joblib"
_model = None
# Feature ORDER contract + room_id→code map, populated from the trained bundle.
_features: list[str] = ["temperature", "hour_of_day", "expected_occupancy", "actual_occupancy"]
_room_map: dict = {}
_metadata: dict = {}

# Degrees Celsius of thermal load added per person present (heuristic bootstrap)
THERMAL_LOAD_PER_PERSON = 0.05

# Weight of the FEEDBACK (CO2-measured actual) signal vs the FEED-FORWARD
# (reservation-planned expected) signal when both are available. 0 → trust the
# plan only; 1 → trust the live sensor only. 0.6 leans on measured reality while
# retaining anticipation for the ramp-up at the start of an occupancy window.
FEEDBACK_WEIGHT = 0.6


def load_model() -> None:
    """Load the trained scikit-learn model from disk at application startup.

    If the model file does not exist, the service degrades gracefully to the
    heuristic fallback. No exception is raised so the rest of the API stays live.
    Train with: ml/pipeline/extract_data.py → ml/pipeline/train_model.py
    """
    global _model, _features, _room_map, _metadata
    try:
        import joblib
        if not _MODEL_PATH.exists():
            logger.warning(
                "model.joblib not found at %s — heuristic fallback active. "
                "Run ml/pipeline/extract_data.py then ml/pipeline/train_model.py.",
                _MODEL_PATH,
            )
            return

        loaded = joblib.load(_MODEL_PATH)
        if isinstance(loaded, dict) and "model" in loaded:
            # Serve-side gate (D4): a bundle that failed to beat its own baselines
            # is never served — the transparent heuristic is better-justified.
            metrics = (loaded.get("metadata") or {}).get("metrics") or {}
            if metrics.get("beats_baselines") is False:
                logger.warning(
                    "ML bundle at %s lost to its baselines (beats_baselines=false) — "
                    "refusing to serve it; heuristic fallback active. Retrain with "
                    "better features before shipping.",
                    _MODEL_PATH,
                )
                return
            # New bundle format: model + feature contract + room map + metadata.
            _model = loaded["model"]
            _features = loaded.get("features", _features)
            _room_map = loaded.get("room_id_map", {})
            _metadata = loaded.get("metadata", {})
            logger.info(
                "ML bundle loaded from %s (features=%s, rooms=%d, trained_at=%s)",
                _MODEL_PATH, _features, len(_room_map), _metadata.get("trained_at"),
            )
        else:
            # Legacy bare estimator — keep default feature order, no room map.
            _model = loaded
            logger.info("ML model (legacy bare estimator) loaded from %s", _MODEL_PATH)
    except Exception as exc:
        logger.error("Failed to load ML model: %s — heuristic fallback active.", exc)


def active_engine() -> str:
    """Return the engine currently driving cognitive actions.

    'ml' when a trained model is loaded in memory, otherwise 'heuristic'. This is
    global, startup-bound server state — independent of any single reading.
    """
    return "ml" if _model is not None else "heuristic"


def model_status() -> dict:
    """What the loaded regressor is, and what its metrics do and do not show.

    Its target (target_temp_offset) is built by a formula in ml/pipeline/extract_data.py,
    not measured, so the metrics score how well it reproduces that formula: it is a
    setpoint adjustment, never a validated model of the room's thermal dynamics.
    """
    metrics = _metadata.get("metrics") or {}
    return {
        "engine": active_engine(),
        "loaded": _model is not None,
        "predicts": "Ajuste del objetivo de temperatura por carga térmica (°C)",
        "target": _metadata.get("target"),
        "label_kind": "synthetic",
        "physically_validated": False,
        "drives_actuators": False,
        "trained_at": _metadata.get("trained_at"),
        "n_rows": _metadata.get("n_rows"),
        "features": list(_features) if _model is not None else [],
        "metrics": {k: metrics.get(k) for k in ("rmse", "r2", "baseline_heuristic_rmse", "beats_baselines")} if metrics else None,
        "note": "Las métricas miden cuánto reproduce una fórmula sintética, no la eficacia del control físico.",
    }


async def calculate_cooling_demand(
    current_temp: float,
    room_context: dict,
    reading_timestamp: datetime,
    co2_ppm: float | None = None,
    camera_occupancy: int | None = None,
) -> dict:
    """Compute the cognitive AC action for an incoming sensor reading.

    Args:
        current_temp: Measured temperature in °C from the sensor.
        room_context: Dict with room metadata (target_temp, expected_people,
                      max_capacity, etc.) or None/empty when the sensor has no
                      registered room or control is disabled.
        reading_timestamp: UTC datetime of the reading; used to extract hour_of_day
                           as a feature for the ML model.
        co2_ppm: Measured CO2 in ppm, used as an occupancy proxy.
        camera_occupancy: Optional direct count from the ESP32-CAM/gateway.

    Returns:
        Dict with keys: ac_status ('ON' | 'STANDBY'), cooling_mode, target,
        thermal_load_offset, model ('ml' | 'heuristic' | 'none'), and the
        occupancy telemetry: expected_occupancy, actual_occupancy,
        effective_occupancy, occupancy_gap.
    """
    raw_expected = (room_context or {}).get("expected_people")
    expected_people = raw_expected or 0
    target_temp = (room_context or {}).get("target_temp")
    policy = (room_context or {}).get("control_policy") or "auto"
    max_capacity = (room_context or {}).get("max_capacity")

    if not room_context or target_temp is None:
        return {"ac_status": "STANDBY", "cooling_mode": None, "target": target_temp, "model": "none"}

    # Direct visual count replaces or blends with the delayed CO2 proxy according
    # to OCCUPANCY_FUSION_MODE. The chosen value is the model's actual occupancy.
    actual_people = fuse_occupancy(
        co2_ppm, camera_occupancy, max_capacity,
        mode=OCCUPANCY_FUSION_MODE if OCCUPANCY_FUSION_MODE in {"co2", "visual", "blend"} else "visual",
        visual_weight=OCCUPANCY_VISUAL_WEIGHT,
    )

    # Effective occupancy = feed-forward (plan) blended with feedback (measured).
    # No CO2 signal → pure feed-forward on the reservation/schedule expectation.
    if actual_people is None:
        effective_people = float(expected_people)
    else:
        effective_people = (
            (1 - FEEDBACK_WEIGHT) * expected_people + FEEDBACK_WEIGHT * actual_people
        )

    # Time features were trained on naive LOCAL timestamps (the seed data writes
    # Lima wall-clock times), while the API stamps live readings with naive UTC.
    # Shift before deriving hour_of_day / outdoor_temp (see app.core.timeutils) —
    # otherwise both features carry a 5 h phase error.
    ts = to_local(reading_timestamp)
    hour = ts.hour

    # Per-room policy decides the engine:
    #   manual    → no prediction, hold the configured target (zero offset)
    #   heuristic → force the heuristic formula regardless of loaded ML model
    #   auto      → ML model when loaded, else heuristic fallback
    if policy == "manual":
        thermal_load = 0.0
        model_used = "manual"
    elif policy == "heuristic" or _model is None:
        thermal_load = effective_people * THERMAL_LOAD_PER_PERSON
        model_used = "heuristic"
    else:
        # actual_people may be None (no CO2) — fall back to expected for that slot
        actual_feature = actual_people if actual_people is not None else expected_people
        # Physical room metadata (Tier 3) — floor, volume_m3, ac_btu.
        meta = _room_metadata(room_context.get("room_id"))
        volume_m3 = room_context.get("volume_m3") or meta["volume_m3"]
        # Build the row in the exact order the bundle was trained on (_features).
        feat_values = {
            "temperature": current_temp,
            "hour_of_day": hour,
            # Missing plan is NaN (HGBR-native missing), matching training's
            # convention — 0 would mean "a plan for zero people", a different thing.
            "expected_occupancy": float("nan") if raw_expected is None else expected_people,
            "actual_occupancy": actual_feature,
            "room_code": _room_map.get(str(room_context.get("room_id")), -1),  # legacy bundles
            "outdoor_temp": _outdoor_temp(ts),   # exogenous climate driver (Tier 2)
            "floor": meta["floor"],
            "volume_m3": volume_m3,
            "ac_btu": meta["ac_btu"],
        }
        try:
            row = [float(feat_values[f]) for f in _features]
        except KeyError as exc:
            logger.error("Unknown model feature %s — heuristic fallback for this reading.", exc)
            thermal_load = effective_people * THERMAL_LOAD_PER_PERSON
            model_used = "heuristic"
        else:
            features = np.array([row], dtype=float)
            thermal_load = float(max(0.0, _model.predict(features)[0]))
            model_used = "ml"

    thermal_load = round(thermal_load, 3)
    adjusted_target = round(target_temp - thermal_load, 2)

    occupancy_telemetry = {
        "expected_occupancy": expected_people,
        "actual_occupancy": actual_people,
        "effective_occupancy": round(effective_people, 2),
        "occupancy_gap": occupancy_gap(expected_people, actual_people),
    }

    if current_temp > adjusted_target:
        return {
            "ac_status": "ON",
            "cooling_mode": "PRE-COOLING",
            "target": adjusted_target,
            "thermal_load_offset": thermal_load,
            "model": model_used,
            **occupancy_telemetry,
        }
    return {
        "ac_status": "STANDBY",
        "cooling_mode": None,
        "target": adjusted_target,
        "thermal_load_offset": thermal_load,
        "model": model_used,
        **occupancy_telemetry,
    }
