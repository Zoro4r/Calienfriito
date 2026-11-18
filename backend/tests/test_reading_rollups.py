"""Rollups must give the same history as the raw readings they replace."""
import random
from datetime import datetime, timedelta

import pytest

from app.services.reading_rollups import (
    bucket_of, build_minutes, ina_energy, live_aggregates, summarize_minutes,
)
from app.services.room_history import ina_energy_series, summarize_day

START = datetime(2026, 11, 16, 5, 0, 0)


def _readings(seed: int, seconds: int = 6 * 3600) -> list[dict]:
    """1 Hz node telemetry with state changes, silences, missing sensors and late microseconds."""
    rnd = random.Random(seed)
    docs, t = [], START
    pwm, heat, manual, alarm, accion = 0, False, False, False, "STANDBY"
    while (t - START).total_seconds() < seconds:
        r = rnd.random()
        if r < 0.002:
            t += timedelta(seconds=rnd.choice([70, 90, 130, 400, 1200]))      # silences of every kind
        elif r < 0.01:
            pwm = rnd.choice([0, 0, 64, 128, 129, 255])                         # 128/129 share a level
            heat = not pwm and rnd.random() < 0.4
            accion = "COOLING" if pwm else "HEATING" if heat else rnd.choice(["STANDBY", "DISABLED"])
        elif r < 0.012:
            manual = not manual
        elif r < 0.014:
            alarm = not alarm
        temp = None if rnd.random() < 0.03 else 22.5 + 3 * rnd.random()
        power = None if rnd.random() < 0.05 else 900 + 100 * rnd.random()
        docs.append({
            "sensor_id": "s-1",
            "timestamp": (t + timedelta(microseconds=rnd.randint(0, 999_999))).strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "temperature": temp,
            "humidity": None if rnd.random() < 0.03 else 50 + 10 * rnd.random(),
            "co2_ppm": None if rnd.random() < 0.05 else 500 + 500 * rnd.random(),
            "co_ppm": None if rnd.random() < 0.05 else (60 if alarm else 2) * rnd.random(),
            "ina_power_mw": power,
            "node_state": {"accion": accion, "ventilador_pwm": pwm, "calefactor": heat,
                           "manual": manual, "alarma_co": alarm},
        })
        t += timedelta(seconds=1)
    # An HTTP reading without node decision: counted as raw, ignored by the history.
    docs.insert(10, {"sensor_id": "s-1", "timestamp": docs[10]["timestamp"], "temperature": 30.0})
    return docs


def _node(docs):
    return [d for d in docs if "accion" in (d.get("node_state") or {})]


def _close(a, b, path=""):
    if isinstance(a, dict):
        assert a.keys() == b.keys(), path
        for k in a:
            _close(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _close(x, y, f"{path}[{i}]")
    elif isinstance(a, float) and isinstance(b, float):
        assert a == pytest.approx(b, abs=0.011), path   # sums in another order: last-digit rounding
    else:
        assert a == b, path


@pytest.mark.parametrize("seed", range(6))
def test_rollups_match_raw_history(seed):
    docs = _readings(seed)
    expected = summarize_day(_node(docs), 2.4, 10.0)
    got, _ = summarize_minutes(build_minutes(docs, "s-1"), 2.4, 10.0)
    _close(got, expected)


@pytest.mark.parametrize("seed", range(3))
def test_rollups_match_raw_history_with_timetable(seed):
    docs = _readings(seed)
    timetable = [(START + timedelta(minutes=37), START + timedelta(minutes=125), 30),
                 (START + timedelta(hours=3, minutes=1), START + timedelta(hours=4, minutes=59), 12)]
    expected = summarize_day(_node(docs), 2.4, 10.0, timetable)
    got, _ = summarize_minutes(build_minutes(docs, "s-1"), 2.4, 10.0, timetable)
    _close(got, expected)


@pytest.mark.parametrize("seed", range(4))
def test_rollup_energy_matches_raw_integration(seed):
    docs = _node(_readings(seed))
    expected = next((v for v in reversed(ina_energy_series(docs)) if v is not None), None)
    assert ina_energy(build_minutes(docs, "s-1")) == pytest.approx(expected, abs=1e-6)


def test_minutes_split_anywhere_give_the_same_history():
    """Stored rollups + raw tail rolled on the fly = one pass over everything."""
    docs = _readings(7, seconds=3600)
    cut = START + timedelta(minutes=23)
    head = [d for d in docs if d["timestamp"] < cut.strftime("%Y-%m-%dT%H:%M:%S")]
    tail = [d for d in docs if d["timestamp"] >= cut.strftime("%Y-%m-%dT%H:%M:%S")]
    whole, _ = summarize_minutes(build_minutes(docs, "s-1"), 2.4, 10.0)
    split, _ = summarize_minutes(build_minutes(head, "s-1") + build_minutes(tail, "s-1"), 2.4, 10.0)
    assert split == whole


def test_raw_count_includes_readings_without_node_decision():
    docs = _readings(1, seconds=120)
    minutes = build_minutes(docs, "s-1")
    assert sum(m["raw_n"] for m in minutes) == len(docs)
    assert sum(m["n"] for m in minutes) == len(_node(docs))


def test_bucket_matches_mongo_date_trunc_reference():
    # $dateTrunc binSize counts from 2000-01-01T00:00Z: 120-min bins start on even UTC hours.
    assert bucket_of(datetime(2026, 11, 16, 13, 59), 120) == datetime(2026, 11, 16, 12, 0)
    assert bucket_of(datetime(2026, 11, 16, 13, 59), 30) == datetime(2026, 11, 16, 13, 30)


def test_live_aggregates_match_per_reading_averages():
    docs = _node(_readings(2, seconds=2 * 3600))
    rows, totals, minutes = live_aggregates(build_minutes(docs, "s-1"), 5)
    assert totals["n"] == len(docs)
    assert totals["first"] == docs[0]["timestamp"][:19]
    assert totals["fan3"] == pytest.approx(sum(((d["node_state"]["ventilador_pwm"] or 0) / 255) ** 3 for d in docs))
    first = rows[0]
    in_bucket = [d for d in docs if bucket_of(datetime.fromisoformat(d["timestamp"][:16]), 5) == first["_id"]["b"]]
    temps = [d["temperature"] for d in in_bucket if d["temperature"] is not None]
    assert first["temp"] == pytest.approx(sum(temps) / len(temps))
    assert first["fan"] == pytest.approx(sum(d["node_state"]["ventilador_pwm"] / 255 for d in in_bucket) / len(in_bucket))
    assert len(minutes) == len({d["timestamp"][:16] for d in docs})


def test_retention_never_deletes_what_is_not_rolled_up():
    from app.services.reading_rollups import retention_cutoff
    now = datetime(2026, 11, 20)
    assert retention_cutoff(datetime(2026, 11, 19), now, 0) is None                       # disabled
    assert retention_cutoff(datetime(2026, 11, 19), now, 7) == datetime(2026, 11, 13)     # by age
    assert retention_cutoff(datetime(2026, 11, 10), now, 7) == datetime(2026, 11, 10)     # rollups behind
