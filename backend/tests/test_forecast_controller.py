"""Predictive controller: forecasts the temperature and starts the fan before it gets hot."""
import asyncio
from datetime import datetime, timedelta

from app.models.sensor import SensorReading
from app.services import data_service
from app.services.forecast_controller import (
    HOT_C, READY_MINUTES, ForecastController, fan_level, persistence, rules_level,
)
from app.services.strategy_series import forecast_accuracy


def rising(step_c: float):
    """A fake forecaster: the temperature keeps rising `step_c` per minute."""
    def f(history, horizon):
        path = [history[-1] + step_c * (k + 1) for k in range(horizon)]
        return path, [t + 0.2 for t in path]
    return f


def feed(ctrl, minutes, temp=24.0, in_use=True, start_min=0):
    for m in range(start_min, start_min + minutes):
        ctrl.step(m * 60, temp, in_use)
        ctrl.step(m * 60 + 30, temp, in_use)


def test_fan_level_grows_with_the_forecast_excess():
    assert [fan_level(x) for x in (-0.3, 0.0, 0.2, 0.7, 1.5)] == [0, 0, 50, 75, 100]


def test_never_below_the_rules_when_it_is_already_hot():
    ctrl = ForecastController(persistence)              # flat forecast at 25.2 °C
    feed(ctrl, READY_MINUTES + 2, temp=25.2)
    assert ctrl.fan_pct == rules_level(25.2) == 75


def test_no_decision_before_enough_history():
    ctrl = ForecastController(rising(0.05))
    feed(ctrl, READY_MINUTES - 5)
    assert not ctrl.ready and ctrl.fan_pct == 0


def test_starts_the_fan_before_the_room_gets_hot():
    ctrl = ForecastController(rising(0.05))            # +1.5 °C in 30 min from 24.0 °C
    feed(ctrl, READY_MINUTES + 2, temp=24.0)
    assert 24.0 < HOT_C                                 # it is not hot yet…
    assert ctrl.fan_pct == 100                          # …but it will be: fan on now
    assert ctrl.summary()["forecast_30"] > HOT_C


def test_stays_off_when_the_room_is_not_in_use():
    ctrl = ForecastController(rising(0.05))
    feed(ctrl, READY_MINUTES + 2, in_use=False)
    assert ctrl.fan_pct == 0


def test_persistence_baseline_never_anticipates():
    ctrl = ForecastController(persistence)
    feed(ctrl, READY_MINUTES + 2, temp=24.0)
    assert ctrl.fan_pct == 0 and ctrl.summary()["model"] == "persistencia"


def test_a_long_silence_restarts_the_history():
    ctrl = ForecastController(persistence)
    feed(ctrl, 40)
    feed(ctrl, 3, start_min=500)                        # the node was off for hours
    assert len(ctrl.history) < 5


def test_state_round_trip():
    ctrl = ForecastController(rising(0.05))
    feed(ctrl, READY_MINUTES + 2)
    clone = ForecastController.from_state(ctrl.to_state(), rising(0.05))
    assert clone.history == ctrl.history and clone.fan_pct == ctrl.fan_pct


class FakeCollection:
    def __init__(self):
        self.docs = {}

    async def find_one(self, query):
        return self.docs.get(query["_id"])

    async def replace_one(self, query, doc, upsert=False):
        self.docs[query["_id"]] = doc


def test_shadow_step_reads_the_node_and_persists_each_minute(monkeypatch):
    data_service._SHADOW.clear()
    monkeypatch.setattr("app.services.forecast_controller.default_forecaster", lambda: persistence)
    db = {"forecast_state": FakeCollection()}
    start = datetime(2026, 10, 24, 15, 0, 0)
    summary = None
    for i in range(13):                                 # a bit over two minutes, every 10 s
        reading = SensorReading(
            sensor_id="s-B101", temperature=25.0, humidity=55.0, co2_ppm=900.0, co_ppm=2.0,
            timestamp=start + timedelta(seconds=10 * i),
            node_state={"accion": "COOLING", "ventilador_pwm": 255},
        )
        summary = asyncio.run(data_service.shadow_learning_step(db, "B101", reading))
    assert {"fan_pct", "forecast_30", "ready", "model"} <= set(summary)
    assert "B101" in db["forecast_state"].docs
    data_service._SHADOW.clear()


def test_shadow_step_ignores_readings_without_a_node_decision():
    reading = SensorReading(sensor_id="s-B101", temperature=25.0, humidity=55.0, co2_ppm=900.0)
    assert asyncio.run(data_service.shadow_learning_step({}, "B101", reading)) is None


def test_forecast_accuracy_scores_model_and_baseline_on_the_same_pairs():
    t0 = datetime(2026, 11, 13, 15, 0)
    rows = [{"_id": {"b": t0 + timedelta(minutes=5 * k)}, "temp": 24.0 + 0.1 * k,
             "pred": 24.0 + 0.1 * (k + 6)} for k in range(12)]   # perfect 30-min forecasts
    out = forecast_accuracy(rows, 5)
    assert out["mae"] == 0.0 and out["mae_baseline"] == 0.6 and out["pairs"] == 6
    assert forecast_accuracy(rows, 120) is None


def test_simulation_mode_skips_forecasts_while_the_room_is_idle():
    calls = []
    def counting(history, horizon):
        calls.append(1)
        return persistence(history, horizon)
    ctrl = ForecastController(counting, forecast_idle=False)
    feed(ctrl, READY_MINUTES + 20, in_use=False)
    assert calls == [] and ctrl.fan_pct == 0
    feed(ctrl, 6, in_use=True, start_min=READY_MINUTES + 20)
    assert calls                                      # forecasts again once the room is in use


def test_class_intervals_mix_weekly_and_one_day_classes():
    from datetime import time as t_
    from app.services.strategy_series import class_intervals
    weekly = [(4, t_(15, 0), t_(17, 0), 20)]                                  # Fridays 15–17 local
    one_off = [(datetime(2026, 11, 12, 20, 0), datetime(2026, 11, 12, 22, 0), 1)]
    start, end = datetime(2026, 11, 12, 5, 0), datetime(2026, 11, 14, 5, 0)   # Thu–Fri local, in UTC
    got = class_intervals(weekly, one_off, start, end)
    assert got == [(datetime(2026, 11, 13, 1, 0), datetime(2026, 11, 13, 3, 0), 1),
                   (datetime(2026, 11, 13, 20, 0), datetime(2026, 11, 13, 22, 0), 20)]
