"""Room history: one day's summary, events and state changes from node readings."""
from datetime import datetime, timedelta

from app.services.room_history import summarize_day


def reading(ts, temp=24.0, accion="STANDBY", fan=0, heat=False, occupied=True, manual=False, alarm=False, co=2.0):
    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000000"), "temperature": temp, "humidity": 55, "co2_ppm": 800, "co_ppm": co,
        "node_state": {"accion": accion if occupied else "DISABLED",   # the firmware disables an empty room
                       "ventilador_pwm": fan, "calefactor": heat, "co2_proxy": 2900 if occupied else 1500,
                       "manual": manual, "alarma_co": alarm, "motivo": "x"},
    }


T0 = datetime(2026, 10, 19, 12, 0)


def test_time_out_of_band_counts_only_class_time():
    docs = [reading(T0 + timedelta(seconds=30 * i), temp=22.0 if i < 10 else 24.0) for i in range(20)]
    docs += [reading(T0 + timedelta(seconds=600 + 30 * i), temp=18.0, occupied=False) for i in range(20)]
    s = summarize_day(docs, 180, 2000)["summary"]
    assert s["pct_out_of_band"] == 50.0           # 10 of 20 class readings below 23.5 °C
    assert s["class_hours"] > 0


def test_manual_orders_and_co_alarms_are_counted():
    docs = [reading(T0 + timedelta(seconds=30 * i), accion="COOLING", fan=127, manual=True) for i in range(4)]
    docs += [reading(T0 + timedelta(seconds=120 + 30 * i), accion="COOLING", fan=255, alarm=True, co=62) for i in range(3)]
    docs += [reading(T0 + timedelta(seconds=210 + 30 * i)) for i in range(3)]
    summary = summarize_day(docs, 180, 2000)["summary"]
    assert summary["manual_orders"] == 1
    assert summary["co_alarms"] == 1


def test_a_gap_is_not_counted_as_running_time():
    docs = [reading(T0, accion="COOLING", fan=255), reading(T0 + timedelta(minutes=20), accion="COOLING", fan=255)]
    assert summarize_day(docs, 180, 2000)["summary"]["fan_hours"] <= 0.02   # 60 s at most (rounded), not 20 min


def test_heater_periods_are_exact_on_off_intervals():
    docs = [reading(T0 + timedelta(seconds=30 * i), accion="HEATING", heat=True) for i in range(4)]
    docs += [reading(T0 + timedelta(seconds=120 + 30 * i)) for i in range(4)]
    docs += [reading(T0 + timedelta(seconds=240 + 30 * i), accion="HEATING", heat=True) for i in range(2)]
    heater = summarize_day(docs, 180, 2000)["heater"]
    assert len(heater) == 2
    assert heater[0]["start"].endswith("12:00:00") and heater[0]["end"].endswith("12:02:00")


def test_fan_periods_keep_their_power_level():
    docs = [reading(T0 + timedelta(seconds=30 * i), accion="COOLING", fan=255) for i in range(3)]
    docs += [reading(T0 + timedelta(seconds=90 + 30 * i), accion="COOLING", fan=127, manual=True) for i in range(3)]
    docs += [reading(T0 + timedelta(seconds=180))]
    fan = summarize_day(docs, 180, 2000)["fan"]
    assert [f["level"] for f in fan] == [100, 50]


def test_class_time_follows_the_timetable_when_given():
    docs = [reading(T0 + timedelta(seconds=30 * i), temp=26.0, occupied=False) for i in range(20)]   # node: empty
    timetable = [(T0, T0 + timedelta(minutes=5), 12)]                                              # but a class
    out = summarize_day(docs, 180, 2000, timetable)
    assert out["summary"]["class_hours"] > 0 and out["summary"]["pct_out_of_band"] == 100.0
    assert out["classes"][0]["people"] == 12
