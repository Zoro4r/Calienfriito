"""
Extract sensor_readings from MongoDB, engineer features, generate target label,
and save a clean CSV ready for training.

Occupancy features (kept as DISTINCT columns — never conflated):
    expected_occupancy → PLANNED headcount. Read from the stored cognitive_action
                         (the real reservation plan); only synthesized as a last
                         resort when a reading has no plan attached.
    actual_occupancy   → REALIZED headcount inferred from CO2 via a steady-state
                         mass-balance proxy. Feedback signal (measured reality).
    room metadata      → floor, volume_m3, ac_btu per room (from room_profile).
                         Physical drivers of thermal mass / A/C capacity, so the
                         model learns each room's behavior instead of an opaque code.

Rows are written in CHRONOLOGICAL order so train_model.py can hold out the most
recent slice as the test set (no future→past leakage).

Target — target_temp_offset:
    Bootstrap heuristic from the REALIZED thermal load:
        people_load  = actual_occupancy * 0.05 * hour_weight
        solar_gain   = max(0, (temperature - 22) * 0.02)
    NOTE: this label is still synthetic; replace with real A/C feedback to let the
    model exceed the heuristic (see docs/platform/TECHNICAL_DOCUMENTATION.md, To-Do / D2).

Usage (run from project root):
    python ml/pipeline/extract_data.py
"""

import json
import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from pymongo import MongoClient

# Import the shared climate + room-profile + occupancy models and the bootstrap
# label helper (single source of every assumption — nothing is mirrored by hand).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))  # ml/pipeline -> raiz
from app.services.climate_service import outdoor_temp as _outdoor_temp  # noqa: E402
from app.services.occupancy_service import (  # noqa: E402
    CO2_BASELINE_PPM,
    CO2_PPM_PER_PERSON,
)
from app.services.room_profile import (  # noqa: E402
    METADATA_FEATURES,
    get_metadata,
    thermal_factor,
)
from common import bootstrap_people_load  # noqa: E402

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DATA_DIR = Path(__file__).parent / "data"
OUTPUT_CSV = DATA_DIR / "features.csv"
ROOM_MAP_JSON = DATA_DIR / "room_map.json"

# Columns persisted to the CSV. room_code (opaque) is replaced by physical room
# metadata features (Tier 3); outdoor_temp is the exogenous climate driver (Tier 2).
FEATURES = [
    "temperature", "humidity", "co2_ppm",
    "hour_of_day", "day_of_week",
    "expected_occupancy", "actual_occupancy", "outdoor_temp",
] + METADATA_FEATURES
TARGET = "target_temp_offset"

# Cooling-demand contribution per °C that the outside air exceeds the baseline
# (envelope heat gain). Baseline 18 °C so even Lima winter contributes mild,
# varying load. Independent of occupancy → signal the occupancy heuristic misses.
CLIMATE_GAIN = 0.06
CLIMATE_BASELINE_C = 18.0


def _room_factor(room_ids: pd.Series) -> np.ndarray:
    """Per-room thermal-mass multiplier, from the shared room_profile."""
    return room_ids.astype(str).apply(thermal_factor).to_numpy()


def _attach_metadata(df: pd.DataFrame) -> pd.DataFrame:
    """Add physical room metadata columns (floor, volume_m3, ac_btu) by room_id."""
    meta = df["room_id"].astype(str).apply(get_metadata)
    for col in METADATA_FEATURES:
        df[col] = meta.apply(lambda m: m[col])
    return df

# Rooms used for the fully-synthetic bootstrap. Reuse the real UTEC ids so the
# per-room thermal factor (ROOM_THERMAL) applies to synthetic rows too.
_SYNTH_ROOMS = ["A403", "M601", "M604", "M1001", "M802", "M602"]


def _people_from_co2(co2: np.ndarray, max_capacity: int | None = None) -> np.ndarray:
    """Inverse mass balance: infer realized headcount from CO2 (mirrors backend)."""
    people = np.maximum(0.0, (co2 - CO2_BASELINE_PPM) / CO2_PPM_PER_PERSON)
    if max_capacity is not None:
        people = np.minimum(people, max_capacity)
    return np.round(people)


def _co2_from_people(people: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Forward mass balance: synthesize a plausible CO2 level from real occupancy."""
    noise = rng.normal(0, 15, len(people))  # sensor/ventilation jitter in ppm
    return np.maximum(350.0, CO2_BASELINE_PPM + people * CO2_PPM_PER_PERSON + noise).round(2)


def _room_id_from_sensor(sid) -> str:
    """Derive room_id from sensor_id (convention: 's-<ROOM>')."""
    if isinstance(sid, str) and sid.startswith("s-"):
        return sid[2:]
    return sid if isinstance(sid, str) and sid else "unknown"


def main():
    DATA_DIR.mkdir(exist_ok=True)

    print(f"Connecting to MongoDB at {MONGO_URI}...")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    docs = list(client["climate_db"]["sensor_readings"].find({}, {"_id": 0}))
    print(f"Found {len(docs)} documents in sensor_readings.")

    # Synthetic rows are tagged is_synthetic=1; train_model.py pins them to the
    # train slice so the chronological test tail stays 100% real (no leakage of
    # untimed synthetic rows into the holdout).
    if len(docs) >= 50:
        df = _clean(pd.DataFrame(docs))
        if len(df) < 100:
            print(f"Only {len(df)} clean rows — augmenting with {1000} synthetic rows.")
            df = pd.concat([df, _generate_synthetic(1000)], ignore_index=True)
    else:
        print("Insufficient real data — generating 2000 synthetic rows.")
        df = _generate_synthetic(2000)

    # Encode room_id → integer code and persist the map for inference (D5).
    df, room_map = _encode_rooms(df)
    ROOM_MAP_JSON.write_text(json.dumps(room_map, indent=2), encoding="utf-8")
    print(f"Saved room map ({len(room_map)} rooms) to {ROOM_MAP_JSON}")

    # room_id and is_synthetic ride along as diagnostic columns (not model
    # features): per-room RMSE reporting and the synthetic/real split guard.
    out = df[FEATURES + [TARGET, "room_id", "is_synthetic", "data_origin"]]
    out.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved {len(out)} rows to {OUTPUT_CSV} (chronological order preserved)")
    print(out.describe().round(3))


# Columns every source function returns (room_id still a string here).
_BASE_COLS = [
    "temperature", "humidity", "co2_ppm",
    "hour_of_day", "day_of_week",
    "expected_occupancy", "actual_occupancy", "room_id", "outdoor_temp",
] + METADATA_FEATURES


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["temperature", "humidity"]).copy()
    df["co2_ppm"] = df["co2_ppm"].fillna(df["co2_ppm"].median())
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df = df.dropna(subset=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)   # D3: chronological
    df["hour_of_day"] = df["timestamp"].dt.hour
    df["day_of_week"] = df["timestamp"].dt.dayofweek
    df["room_id"] = (
        df["sensor_id"].apply(_room_id_from_sensor) if "sensor_id" in df.columns else "unknown"
    )
    # Outdoor temp: use the stored value when the seed provided it, else model it.
    if "outdoor_temp" not in df.columns:
        df["outdoor_temp"] = np.nan
    df["outdoor_temp"] = df["outdoor_temp"].fillna(
        df["timestamp"].apply(lambda t: _outdoor_temp(t.to_pydatetime()))
    )
    df = _attach_metadata(df)       # Tier 3: physical room metadata features
    df = _extract_expected(df)      # D1: read real plan from cognitive_action
    df = _add_actual(df)
    df[TARGET] = _compute_offset(df)
    df["is_synthetic"] = 0
    # Stored provenance, never inferred: documents older than the field read "unknown"
    # (seed, HTTP and emulator readings cannot be told from hardware).
    simulated = df["is_simulated"].fillna(False).astype(bool) if "is_simulated" in df.columns else pd.Series(False, index=df.index)
    fallback = pd.Series(np.where(simulated, "simulated", "unknown"), index=df.index)
    df["data_origin"] = df["data_origin"].fillna(fallback) if "data_origin" in df.columns else fallback
    return df[_BASE_COLS + [TARGET, "is_synthetic", "data_origin"]]


def _extract_expected(df: pd.DataFrame) -> pd.DataFrame:
    """D1 — pull the REAL planned occupancy from the stored cognitive_action."""
    if "cognitive_action" in df.columns:
        df["expected_occupancy"] = df["cognitive_action"].apply(
            lambda c: c.get("expected_occupancy") if isinstance(c, dict) else None
        )
    elif "expected_occupancy" not in df.columns:
        df["expected_occupancy"] = np.nan
    return df


def _add_actual(df: pd.DataFrame) -> pd.DataFrame:
    """Feedback signal from CO2, plus a pattern backfill for any missing plan.

    NOTE: unlike serving (occupancy_service clamps the estimate to max_capacity),
    the training estimate is unclamped — seed CO2 derives from real headcounts
    ≤ capacity, so the residual drift is at most ~1 person of sensor jitter.
    """
    df["actual_occupancy"] = _people_from_co2(df["co2_ppm"].to_numpy())

    rng = np.random.default_rng(0)
    is_class = (df["day_of_week"] < 5) & (df["hour_of_day"].between(8, 22))
    synthetic_expected = np.where(
        is_class,
        rng.integers(10, 35, len(df)),
        rng.integers(0, 5, len(df)),
    )
    df["expected_occupancy"] = df["expected_occupancy"].fillna(
        pd.Series(synthetic_expected, index=df.index)
    )
    return df


def _compute_offset(df: pd.DataFrame) -> pd.Series:
    # Cooling demand = (occupancy load + EXOGENOUS climate load) scaled by the
    # room's thermal mass. The occupancy-only heuristic baseline sees neither the
    # outdoor term nor the room factor — that is what lets the ML model win.
    people_load = bootstrap_people_load(df["actual_occupancy"], df["hour_of_day"])
    climate_load = np.maximum(0, (df["outdoor_temp"] - CLIMATE_BASELINE_C)) * CLIMATE_GAIN
    room_factor = _room_factor(df["room_id"])
    noise = np.random.default_rng(1).normal(0, 0.08, len(df))
    return np.maximum(0, (people_load + climate_load) * room_factor + noise).round(4)


def _encode_rooms(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Stable room_id → integer code mapping (sorted for determinism)."""
    cats = sorted(df["room_id"].astype(str).unique())
    room_map = {rid: i for i, rid in enumerate(cats)}
    df = df.copy()
    df["room_code"] = df["room_id"].astype(str).map(room_map).astype(int)
    return df, room_map


def _generate_synthetic(n: int) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    hours = rng.integers(0, 24, n)
    days = rng.integers(0, 7, n)
    temps = rng.uniform(15.0, 45.0, n).round(2)
    humidity = rng.uniform(30.0, 100.0, n).round(2)
    is_class = (days < 5) & (hours >= 8) & (hours <= 22)

    # Feed-forward: PLANNED occupancy (the reservation).
    expected = np.where(is_class, rng.integers(10, 35, n), rng.integers(0, 5, n))

    # Feedback: REALIZED occupancy — a fraction of the plan actually shows up
    # (no-shows / over-reservation), occasionally exceeding it (walk-ins).
    attendance_rate = rng.uniform(0.55, 0.95, n)
    actual = np.round(expected * attendance_rate).astype(int)
    actual = np.maximum(0, actual + rng.integers(-2, 3, n))  # walk-ins / stragglers

    # CO2 is generated FROM realized occupancy → the gap stays physically coherent.
    co2 = _co2_from_people(actual, rng)

    # Exogenous outdoor temp spanning the seasonal range (synthetic diversity).
    outdoor = np.round(rng.uniform(15.0, 30.0, n), 2)

    room_ids = rng.choice(_SYNTH_ROOMS, n)
    people_load = bootstrap_people_load(actual, hours)
    climate_load = np.maximum(0, (outdoor - CLIMATE_BASELINE_C)) * CLIMATE_GAIN
    room_factor = _room_factor(pd.Series(room_ids))
    noise = rng.normal(0, 0.08, n)
    target = np.maximum(0, (people_load + climate_load) * room_factor + noise).round(4)

    out = pd.DataFrame({
        "temperature": temps,
        "humidity": humidity,
        "co2_ppm": co2,
        "hour_of_day": hours,
        "day_of_week": days,
        "expected_occupancy": expected,
        "actual_occupancy": actual,
        "room_id": room_ids,
        "outdoor_temp": outdoor,
        TARGET: target,
        "is_synthetic": 1,
        "data_origin": "augmented",
    })
    return _attach_metadata(out)     # Tier 3: physical room metadata features


if __name__ == "__main__":
    main()
