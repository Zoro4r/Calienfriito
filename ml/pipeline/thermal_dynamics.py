"""
Thermal dynamics: predict the OBSERVED change of room temperature, ΔT(h) = T(t+h) − T(t).

Unlike train_model.py (whose target is a formula), the labels here are measurements:
the temperature the room actually reached h minutes later. They are built offline from
an export of GET /api/v1/sensors/training-export, never written back to MongoDB.

Pipeline (nothing here touches the served models in backend/app/ml):
    1. load_export     CSV exports, provenance kept (data_origin)
    2. minute_frame    one row per room-minute; missing minutes stay missing (no filling)
    3. build_dataset   features known at t + labels ΔT(h) for each horizon
    4. evaluate        walk-forward over whole local days, embargo of h minutes before
                       each test day, against persistence (ΔT=0) and the recent trend
    5. report          metrics per horizon / fold / room and an accept-or-reject verdict

Features are only what the node really reports: temperature and its recent slope,
humidity, CO2, the node's own occupancy reading, the APPLIED fan power and minutes since
it last changed, heater state, and the local hour as sine/cosine. Outdoor temperature is
NOT used: climate_service models it, it is not measured. The fan's effect is learned from
data, never assumed: in a room with a recirculation fan it may well be zero.

A better ΔT forecast is not a better controller. The report says `validated_for_control:
false` always; control is judged on discomfort minutes, switching and measured energy.

Usage (from the project root):
    python ml/pipeline/thermal_dynamics.py export_A403.csv [more.csv ...] [--origin physical]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HORIZONS_MIN = (5, 15, 30)
LOCAL_OFFSET_H = int(os.getenv("LOCAL_UTC_OFFSET_HOURS", "-5"))
MIN_ROWS_PER_HORIZON = 500      # labelled minutes needed before a horizon is trained at all
MIN_DAYS = 14                   # whole days of data before a model may be accepted
MIN_FOLDS = 3
MIN_TRAIN_DAYS = 3              # first walk-forward fold trains on this many days
IMPROVEMENT = 0.10              # must cut persistence's MAE by 10 % ...
FOLD_SHARE = 2 / 3              # ... and beat both baselines in this share of folds
ARTIFACTS = Path(__file__).parent / "artifacts"

FEATURES = [
    "temperature", "slope_5", "slope_15", "humidity", "co2_ppm", "ocupado",
    "fan_pct", "fan_on_min", "heater", "hour_sin", "hour_cos",
]


def load_export(paths: list[str | Path], origins: tuple[str, ...] = ("physical",)) -> pd.DataFrame:
    """Concatenate training exports, keeping only the requested provenance."""
    df = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    df["ts"] = pd.to_datetime(df["timestamp_server_utc"])
    if origins:
        df = df[df["data_origin"].isin(origins)]
    return df.sort_values(["room_id", "ts"]).reset_index(drop=True)


def _flag(series: pd.Series) -> pd.Series:
    return series.map({True: 1.0, False: 0.0, "True": 1.0, "False": 0.0, "true": 1.0, "false": 0.0,
                       1: 1.0, 0: 0.0, "1": 1.0, "0": 0.0}).astype(float)


def minute_frame(df: pd.DataFrame) -> pd.DataFrame:
    """One row per room and minute over each room's whole span; absent minutes stay NaN."""
    out = []
    for room, g in df.groupby("room_id"):
        g = g.assign(minute=g["ts"].dt.floor("min"),
                     ocupado=_flag(g["ocupado_nodo"]) if "ocupado_nodo" in g else np.nan,
                     heater=_flag(g["heater"]) if "heater" in g else np.nan)
        m = g.groupby("minute").agg(
            temperature=("temperature", "mean"), humidity=("humidity", "mean"), co2_ppm=("co2_ppm", "mean"),
            fan_pct=("fan_applied_pct", "mean"), ocupado=("ocupado", "max"), heater=("heater", "max"),
        )
        full = pd.date_range(m.index.min(), m.index.max(), freq="min")
        m = m.reindex(full)
        m.index.name = "minute"
        m["room_id"] = room
        out.append(m.reset_index())
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def build_dataset(minutes: pd.DataFrame, horizons=HORIZONS_MIN) -> pd.DataFrame:
    """Features known at t, labels ΔT(h) from the measured T(t+h). Rows without T(t) are dropped."""
    parts = []
    for _, g in minutes.groupby("room_id"):
        g = g.sort_values("minute").reset_index(drop=True)
        t = g["temperature"]
        g["slope_5"] = t - t.shift(5)
        g["slope_15"] = t - t.shift(15)
        on = (g["fan_pct"].fillna(0) > 0).astype(int)
        changed = on.ne(on.shift()).cumsum()
        g["fan_on_min"] = g.groupby(changed).cumcount() * np.where(on == 1, 1, -1)   # + on for n min, − off
        for h in horizons:
            g[f"dT_{h}"] = t.shift(-h) - t
        parts.append(g)
    ds = pd.concat(parts, ignore_index=True)
    local = ds["minute"] + pd.Timedelta(hours=LOCAL_OFFSET_H)
    hours = local.dt.hour + local.dt.minute / 60
    ds["hour_sin"], ds["hour_cos"] = np.sin(2 * np.pi * hours / 24), np.cos(2 * np.pi * hours / 24)
    ds["day"] = local.dt.date
    return ds[ds["temperature"].notna()].reset_index(drop=True)


def _mae(err: np.ndarray) -> float:
    return float(np.mean(np.abs(err)))


def _rmse(err: np.ndarray) -> float:
    return float(np.sqrt(np.mean(err ** 2)))


def evaluate(ds: pd.DataFrame, horizons=HORIZONS_MIN, make_model=None) -> dict:
    """Walk-forward by whole local days with an h-minute embargo before each test day."""
    if make_model is None:
        from sklearn.ensemble import HistGradientBoostingRegressor

        def make_model():
            return HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                                 l2_regularization=1.0, early_stopping=False, random_state=0)
    days = sorted(ds["day"].unique())
    report: dict = {"days": len(days), "rooms": sorted(ds["room_id"].unique().tolist()), "horizons": {}}
    for h in horizons:
        label = f"dT_{h}"
        lab = ds[ds[label].notna()]
        entry: dict = {"labelled_rows": int(len(lab))}
        if len(lab) < MIN_ROWS_PER_HORIZON:
            entry["skipped"] = f"{len(lab)} labelled minutes < {MIN_ROWS_PER_HORIZON}"
            report["horizons"][h] = entry
            continue
        folds = []
        for k in range(MIN_TRAIN_DAYS, len(days)):
            test_day = days[k]
            test = lab[lab["day"] == test_day]
            start = test["minute"].min() if len(test) else None
            # Embargo: no training row whose label time (t+h) reaches into the test day.
            train = lab[(lab["day"] < test_day) & (lab["minute"] + pd.Timedelta(minutes=h) < start)] if start is not None else lab.iloc[0:0]
            if len(test) == 0 or len(train) < MIN_ROWS_PER_HORIZON // 2:
                continue
            y = test[label].to_numpy()
            model = make_model().fit(train[FEATURES], train[label])
            pred = model.predict(test[FEATURES])
            trend = (test["slope_15"].fillna(0) / 15 * h).to_numpy()
            folds.append({
                "test_day": str(test_day), "n_train": int(len(train)), "n_test": int(len(test)),
                "mae_model": _mae(pred - y), "rmse_model": _rmse(pred - y),
                "mae_persistence": _mae(-y), "rmse_persistence": _rmse(-y),
                "mae_trend": _mae(trend - y),
                "per_room_mae_model": {r: _mae(pred[(test["room_id"] == r).to_numpy()] - y[(test["room_id"] == r).to_numpy()])
                                       for r in test["room_id"].unique()},
            })
        entry["folds"] = folds
        if folds:
            for key in ("mae_model", "rmse_model", "mae_persistence", "rmse_persistence", "mae_trend"):
                entry[key] = round(float(np.mean([f[key] for f in folds])), 4)
            entry["folds_beating_baselines"] = sum(f["mae_model"] < min(f["mae_persistence"], f["mae_trend"]) for f in folds)
        entry.update(_verdict(entry, len(days)))
        report["horizons"][h] = entry
    report["validated_for_control"] = False
    report["control_note"] = ("A lower ΔT error does not make a better controller: control must be judged on "
                              "minutes outside comfort, switching and measured energy, in shadow first.")
    return report


def _verdict(entry: dict, n_days: int) -> dict:
    folds = entry.get("folds") or []
    reasons = []
    if n_days < MIN_DAYS:
        reasons.append(f"{n_days} days of data < {MIN_DAYS}")
    if len(folds) < MIN_FOLDS:
        reasons.append(f"{len(folds)} walk-forward folds < {MIN_FOLDS}")
    if folds:
        if entry["mae_model"] > (1 - IMPROVEMENT) * entry["mae_persistence"]:
            reasons.append(f"MAE {entry['mae_model']:.3f} does not cut persistence ({entry['mae_persistence']:.3f}) by {IMPROVEMENT:.0%}")
        if entry["mae_model"] >= entry["mae_trend"]:
            reasons.append(f"MAE {entry['mae_model']:.3f} does not beat the trend ({entry['mae_trend']:.3f})")
        if entry["folds_beating_baselines"] < FOLD_SHARE * len(folds):
            reasons.append(f"beats both baselines in {entry['folds_beating_baselines']} of {len(folds)} folds")
    return {"accepted": not reasons, "rejection_reasons": reasons}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("exports", nargs="+", help="CSV files from GET /api/v1/sensors/training-export")
    ap.add_argument("--origin", default="physical", help="comma-separated data_origin values to keep")
    args = ap.parse_args()

    origins = tuple(o for o in args.origin.split(",") if o)
    raw = load_export(args.exports, origins)
    if raw.empty:
        raise SystemExit(f"No rows with data_origin in {origins}: nothing to learn from.")
    ds = build_dataset(minute_frame(raw))
    report = evaluate(ds)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ARTIFACTS / f"thermal_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    ds.to_csv(out / "dataset.csv", index=False)
    report["inputs"] = [{"file": str(p), "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in args.exports]
    report["origins"] = list(origins)
    report["features"] = FEATURES
    report["generated_at"] = stamp
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for h, e in report["horizons"].items():
        if e.get("accepted"):
            import joblib
            from sklearn.ensemble import HistGradientBoostingRegressor
            lab = ds[ds[f"dT_{h}"].notna()]
            model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
                                                  l2_regularization=1.0, early_stopping=False, random_state=0)
            joblib.dump({"model": model.fit(lab[FEATURES], lab[f"dT_{h}"]), "features": FEATURES, "horizon_min": h,
                         "target": f"T(t+{h}) - T(t)", "report": str(out / "report.json")},
                        out / f"candidate_dT_{h}.joblib")
        status ="skipped: " + e["skipped"] if "skipped" in e else ("ACCEPTED" if e["accepted"] else "rejected: " + "; ".join(e["rejection_reasons"]))
        print(f"ΔT({h} min): {status}")
    print(f"Report → {out / 'report.json'} (served models untouched; not validated for control)")


if __name__ == "__main__":
    main()
