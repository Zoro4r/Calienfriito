"""Offline ΔT(h) pipeline: measured labels, no leakage across days, honest verdicts."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ml" / "pipeline"))
import thermal_dynamics as td  # noqa: E402


def export(days: int, step_s: int = 30, origin: str = "physical", room: str = "A403") -> pd.DataFrame:
    """A room that warms 0.02 °C/min with the fan off and cools 0.03 °C/min with it on."""
    n = days * 24 * 3600 // step_s
    ts = pd.date_range("2026-11-01T05:00:00", periods=n, freq=f"{step_s}s")
    fan = ((ts.hour % 3) == 0).astype(float) * 60
    rate = np.where(fan > 0, -0.03, 0.02) * step_s / 60
    temp = 24 + np.cumsum(rate) % 3
    return pd.DataFrame({
        "timestamp_server_utc": ts.strftime("%Y-%m-%dT%H:%M:%S"), "room_id": room, "sensor_id": f"s-{room}",
        "data_origin": origin, "temperature": temp.round(2), "humidity": 60.0, "co2_ppm": 600.0,
        "ocupado_nodo": True, "fan_applied_pct": fan, "heater": False,
    })


def dataset(days: int, **kw) -> pd.DataFrame:
    df = export(days, **kw)
    df["ts"] = pd.to_datetime(df["timestamp_server_utc"])
    return td.build_dataset(td.minute_frame(df))


def test_labels_are_the_measured_change():
    ds = dataset(1)
    row = ds.iloc[100]
    later = ds[ds["minute"] == row["minute"] + pd.Timedelta(minutes=15)].iloc[0]
    assert row["dT_15"] == pytest.approx(later["temperature"] - row["temperature"])


def test_missing_minutes_stay_missing_and_break_labels():
    df = export(1)
    df["ts"] = pd.to_datetime(df["timestamp_server_utc"])
    gap = df[(df["ts"] >= "2026-11-01T10:00") & (df["ts"] < "2026-11-01T10:20")].index
    minutes = td.minute_frame(df.drop(gap))
    ds = td.build_dataset(minutes)
    before = ds[ds["minute"] == pd.Timestamp("2026-11-01T09:50")].iloc[0]
    assert np.isnan(before["dT_15"])                     # T(t+15) falls in the gap: no invented label


def test_only_requested_origins_are_loaded(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    export(1, origin="physical").to_csv(a, index=False)
    export(1, origin="unknown", room="M601").to_csv(b, index=False)
    raw = td.load_export([a, b])
    assert set(raw["data_origin"]) == {"physical"} and set(raw["room_id"]) == {"A403"}


def test_walk_forward_embargo_keeps_labels_out_of_the_test_day():
    seen = []

    class Spy:
        def fit(self, X, y):
            seen.append(X.index)
            return self

        def predict(self, X):
            return np.zeros(len(X))

    ds = dataset(5)
    td.evaluate(ds, horizons=(30,), make_model=Spy)
    days = sorted(ds["day"].unique())
    for k, idx in enumerate(seen):
        test_start = ds[ds["day"] == days[td.MIN_TRAIN_DAYS + k]]["minute"].min()
        assert (ds.loc[idx, "minute"] + pd.Timedelta(minutes=30) < test_start).all()


def test_short_history_is_never_accepted_or_validated_for_control():
    report = td.evaluate(dataset(5), horizons=(15,))
    h = report["horizons"][15]
    assert h["accepted"] is False and any("days of data" in r for r in h["rejection_reasons"])
    assert report["validated_for_control"] is False
