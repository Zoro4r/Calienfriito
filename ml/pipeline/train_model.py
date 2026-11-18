"""
Train a HistGradientBoostingRegressor to predict target_temp_offset.

Features  : temperature, hour_of_day, expected_occupancy, actual_occupancy,
            outdoor_temp, floor, volume_m3, ac_btu
Target    : target_temp_offset  (degrees to pre-compensate)
Output    : backend/app/ml/model.joblib  — a BUNDLE dict, not a bare estimator:
              { "model", "features", "room_id_map", "metadata" }

The feature list AND ORDER below is the single contract; predictive_service.py reads
bundle["features"] and builds its inference row in that order, so adding/reordering
features here does not silently break inference.

Validation:
  - CHRONOLOGICAL holdout (last 20% of REAL rows) — no random shuffle → no time
    leakage. Synthetic bootstrap rows (is_synthetic=1) are pinned to the train
    slice so the test tail is always real data.
  - Expanding-window TimeSeriesSplit CV (3 folds) over the real rows — stability
    check beyond the single holdout.
  - Baselines (predict-mean, heuristic) printed alongside the model; the model must
    beat both to justify itself. predictive_service.load_model refuses to serve a
    bundle whose beats_baselines is false (serve-side gate).

Usage (run from project root):
    python ml/pipeline/extract_data.py   # generate data first
    python ml/pipeline/train_model.py
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import joblib
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_squared_error, r2_score
from sklearn.inspection import permutation_importance
from sklearn.model_selection import TimeSeriesSplit

from common import bootstrap_people_load

DATA_CSV = Path(__file__).parent / "data" / "features.csv"
ROOM_MAP_JSON = Path(__file__).parent / "data" / "room_map.json"
MODEL_OUTPUT = Path(__file__).resolve().parents[2] / "backend" / "app" / "ml" / "model.joblib"
# Retraining never replaces the served model by itself: the bundle goes here as a
# candidate, and only `--promote` copies it to MODEL_OUTPUT (then POST /admin/model/reload).
CANDIDATE_DIR = Path(__file__).parent / "artifacts"

# Order MUST match the ML feature vector in predictive_service.calculate_cooling_demand.
# room_code (opaque) replaced by physical metadata: floor, volume_m3, ac_btu (Tier 3).
FEATURES = [
    "temperature", "hour_of_day", "expected_occupancy",
    "actual_occupancy", "outdoor_temp", "floor", "volume_m3", "ac_btu",
]
TARGET = "target_temp_offset"

TEST_FRACTION = 0.2


def _rmse(y_true, y_pred) -> float:
    return float(np.sqrt(mean_squared_error(y_true, y_pred)))


def _make_model() -> HistGradientBoostingRegressor:
    # D7 — early stopping on an internal validation split of the training data.
    return HistGradientBoostingRegressor(
        max_iter=400,
        learning_rate=0.05,
        max_depth=4,
        early_stopping=True,
        validation_fraction=0.15,
        n_iter_no_change=15,
        random_state=42,
    )


def _heuristic_offset(X: np.ndarray) -> np.ndarray:
    """Occupancy-only bootstrap heuristic — the baseline the ML model must beat.

    Deliberately blind to outdoor_temp and room metadata, so the model can win by
    using them. X columns are in FEATURES order. Formula shared with the label
    builder via ml/pipeline/common.py — they must stay numerically identical.
    """
    hour = X[:, FEATURES.index("hour_of_day")]
    actual = X[:, FEATURES.index("actual_occupancy")]
    return np.maximum(0, bootstrap_people_load(actual, hour))


def main():
    # Windows consoles default to cp1252 — force UTF-8 so °C/R²/bar chars don't crash.
    # reconfigure exists on TextIOWrapper but not the TextIO base type — fetch dynamically.
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8")
        except Exception:
            pass

    if not DATA_CSV.exists():
        raise FileNotFoundError(f"{DATA_CSV} not found. Run extract_data.py first.")

    df = pd.read_csv(DATA_CSV)
    print(f"Loaded {len(df)} rows from {DATA_CSV}")

    room_map = json.loads(ROOM_MAP_JSON.read_text(encoding="utf-8")) if ROOM_MAP_JSON.exists() else {}

    # R3 — synthetic bootstrap rows may only train: pin them ahead of the
    # time-ordered real rows so the chronological test tail stays 100% real.
    n_synthetic = int(df["is_synthetic"].sum()) if "is_synthetic" in df.columns else 0
    if n_synthetic:
        df = pd.concat(
            [df[df["is_synthetic"] == 1], df[df["is_synthetic"] == 0]],
            ignore_index=True,
        )
        print(f"{n_synthetic} synthetic rows pinned to the train slice.")

    X = df[FEATURES]
    y = df[TARGET]

    # D3 — chronological holdout carved from REAL rows only (synthetic, if any,
    # sits up front). Falls back to a plain tail split when all rows are synthetic.
    n_real = len(df) - n_synthetic
    n_test = int((n_real if n_real else len(df)) * TEST_FRACTION) or 1
    cut = len(df) - n_test
    # Fit on numpy arrays (no column names) so inference — which passes a plain
    # np.array — matches exactly and does not warn about missing feature names.
    X_train, X_test = X.iloc[:cut].to_numpy(), X.iloc[cut:].to_numpy()
    y_train, y_test = y.iloc[:cut], y.iloc[cut:]
    print(f"Chronological split: {len(X_train)} train / {len(X_test)} test ({n_synthetic} synthetic, train-only)")

    model = _make_model()
    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    rmse = _rmse(y_test, preds)
    r2 = r2_score(y_test, preds)

    # D4 — baselines on the same test set.
    base_mean = np.full(len(y_test), y_train.mean())
    rmse_mean = _rmse(y_test, base_mean)
    rmse_heur = _rmse(y_test, _heuristic_offset(X_test))

    print(f"\nTest RMSE (model)     : {rmse:.4f} °C")
    print(f"Test R²   (model)     : {r2:.4f}")
    print(f"Baseline RMSE (mean)  : {rmse_mean:.4f} °C")
    print(f"Baseline RMSE (heur.) : {rmse_heur:.4f} °C")
    beats = rmse < rmse_heur and rmse < rmse_mean
    print(f"Model beats baselines : {'YES' if beats else 'NO — reconsider using ML'}")

    # U6 — expanding-window CV over the time-ordered REAL rows: stability check
    # beyond the single holdout (mean ± std across folds).
    cv_rmses: list[float] = []
    X_real = X.iloc[n_synthetic:].to_numpy()
    y_real = y.iloc[n_synthetic:].reset_index(drop=True)
    if len(X_real) >= 50:
        for tr_idx, te_idx in TimeSeriesSplit(n_splits=3).split(X_real):
            fold_model = _make_model().fit(X_real[tr_idx], y_real.iloc[tr_idx])
            cv_rmses.append(_rmse(y_real.iloc[te_idx], fold_model.predict(X_real[te_idx])))
        print(
            f"TimeSeriesSplit CV RMSE : {np.mean(cv_rmses):.4f} ± {np.std(cv_rmses):.4f} "
            f"({len(cv_rmses)} expanding-window folds)"
        )

    # U7 — per-room test RMSE: shows how evenly the model serves each room and
    # makes the (modest) contribution of the physical metadata visible.
    per_room_rmse: dict[str, float] = {}
    if "room_id" in df.columns:
        test_rooms = df["room_id"].iloc[cut:].astype(str).reset_index(drop=True)
        y_test_arr = y_test.to_numpy()
        for rid in sorted(test_rooms.unique()):
            mask = (test_rooms == rid).to_numpy()
            if mask.sum() >= 5:
                per_room_rmse[rid] = round(_rmse(y_test_arr[mask], preds[mask]), 4)
        if per_room_rmse:
            print(f"Per-room test RMSE      : {per_room_rmse}")

    print("\nFeature importances (permutation, mean R² drop):")
    perm = permutation_importance(model, X_test, y_test, n_repeats=10, random_state=42)
    for feat, imp in sorted(zip(FEATURES, perm.importances_mean), key=lambda x: -x[1]):
        bar = "█" * int(max(0.0, imp) * 40)
        print(f"  {feat:<22} {imp:.3f}  {bar}")

    # D6 — bundle model + contract + metadata.
    metadata = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_rows": len(df),
        "n_train": len(X_train),
        "n_test": len(X_test),
        "n_synthetic": n_synthetic,
        # Provenance of the rows (extract_data.py): physical / simulated / unknown / augmented.
        # "unknown" is not physical: seed and HTTP readings cannot be verified.
        "origins": df["data_origin"].value_counts().to_dict() if "data_origin" in df.columns else None,
        "label_kind": "synthetic",
        "features": FEATURES,
        "target": TARGET,
        "sklearn_version": sklearn.__version__,
        "data_sha256": hashlib.sha256(DATA_CSV.read_bytes()).hexdigest(),
        "metrics": {
            "rmse": round(rmse, 4),
            "r2": round(r2, 4),
            "baseline_mean_rmse": round(rmse_mean, 4),
            "baseline_heuristic_rmse": round(rmse_heur, 4),
            "beats_baselines": beats,
            "cv_rmse_mean": round(float(np.mean(cv_rmses)), 4) if cv_rmses else None,
            "cv_rmse_std": round(float(np.std(cv_rmses)), 4) if cv_rmses else None,
            "per_room_rmse": per_room_rmse or None,
        },
    }
    bundle = {
        "model": model,
        "features": FEATURES,
        "room_id_map": room_map,
        "metadata": metadata,
    }

    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    candidate = CANDIDATE_DIR / f"model_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.joblib"
    joblib.dump(bundle, candidate)
    print(f"\nCandidate bundle saved → {candidate}")
    if "--promote" not in sys.argv:
        print("Served model untouched. Re-run with --promote to replace it.")
    elif not beats:
        print("Not promoted: the bundle does not beat its baselines.")
    else:
        MODEL_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, MODEL_OUTPUT)
        print(f"Promoted → {MODEL_OUTPUT} (reload with POST /admin/model/reload)")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
