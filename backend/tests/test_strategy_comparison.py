"""RF-30 coincidence and RF-31 energy per strategy."""
import pytest

from app.services.strategy_comparison import (
    DUTY_PRECOOL, FAN_POWER_W, HEATER_POWER_W, MAX_GAP_S, compare_reading, summarize,
)


def reading(ts, accion, pwm=0, ai="STANDBY", mode=None, heater=False, alarm=False, manual=False):
    return {
        "timestamp": f"2026-10-26T15:{ts}.000000",
        "temperature": 24.0, "co2_ppm": 900,
        "node_state": {"accion": accion, "ventilador_pwm": pwm, "calefactor": heater, "alarma_co": alarm, "motivo": "x", "manual": manual},
        "cognitive_action": {"ac_status": ai, "cooling_mode": mode},
    }


@pytest.mark.parametrize("accion, ai, match", [
    ("COOLING", "ON", True),
    ("PRE_COOLING", "ON", True),
    ("STANDBY", "STANDBY", True),
    ("HEATING", "STANDBY", True),      # the AI does not heat: both mean "no cooling"
    ("DISABLED", "DISABLED", True),
    ("COOLING", "STANDBY", False),
    ("STANDBY", "ON", False),
])
def test_compare_reading(accion, ai, match):
    assert compare_reading(reading("00:00", accion, ai=ai))["match"] is match


def test_co_alarm_and_http_readings_are_not_compared():
    assert compare_reading(reading("00:00", "COOLING", ai="STANDBY", alarm=True)) is None
    assert compare_reading({"timestamp": "x", "node_state": None, "cognitive_action": {"ac_status": "ON"}}) is None


def test_summary_counts_and_divergences():
    docs = [
        reading("00:00", "COOLING", 255, ai="ON"),
        reading("00:10", "COOLING", 255, ai="STANDBY"),
        reading("00:20", "STANDBY", 0, ai="STANDBY"),
        reading("00:30", "COOLING", 255, ai="ON", alarm=True),
    ]
    s = summarize(docs)
    assert s["compared"] == 3 and s["matches"] == 2
    assert s["coincidence_pct"] == pytest.approx(66.7)
    assert s["excluded_co_alarm"] == 1
    assert s["divergence_pairs"] == [{"node_action": "COOLING", "ai_status": "STANDBY", "count": 1}]
    assert s["divergences"][0]["timestamp"].startswith("2026-10-26T15:00:10")


def test_energy_uses_duty_time_and_caps_gaps():
    docs = [
        reading("00:00", "COOLING", 255, ai="ON", mode="PRE-COOLING"),   # 10 s
        reading("00:10", "HEATING", 0, ai="STANDBY", heater=True),         # 10 s
        reading("00:20", "COOLING", 255, ai="ON"),                         # gap 5 min → capped
        reading("05:20", "STANDBY", 0, ai="STANDBY"),                      # last: typical gap
    ]
    e = summarize(docs)["energy"]
    gaps = [10, 10, MAX_GAP_S, 10]
    node_fan = FAN_POWER_W * (gaps[0] + gaps[2]) / 3600
    ai_fan = FAN_POWER_W * (DUTY_PRECOOL / 255 * gaps[0] + gaps[2]) / 3600
    assert e["node_fan_wh"] == pytest.approx(node_fan, abs=1e-3)
    assert e["ai_fan_wh"] == pytest.approx(ai_fan, abs=1e-3)
    assert e["node_heater_wh"] == pytest.approx(HEATER_POWER_W * 10 / 3600, abs=1e-3)
    assert e["hours"] == pytest.approx(sum(gaps) / 3600, abs=1e-3)


def test_manual_orders_are_not_compared_but_their_energy_counts():
    docs = [
        reading("00:00", "COOLING", 255, ai="STANDBY", manual=True),
        reading("00:10", "STANDBY", 0, ai="STANDBY"),
    ]
    s = summarize(docs)
    assert compare_reading(docs[0]) is None
    assert s["compared"] == 1 and s["excluded_manual"] == 1
    assert s["energy"]["node_fan_wh"] == pytest.approx(FAN_POWER_W * 10 / 3600, abs=1e-3)


def test_smart_plug_uses_room_power_and_full_on_duty():
    docs = [
        reading("00:00", "STANDBY", ai="ON", mode="PRE-COOLING"),
        reading("00:10", "STANDBY", ai="STANDBY"),
    ]
    energy = summarize(docs, fan_power_w=40.0, fan_type="smart_plug")["energy"]
    assert energy["fan_power_w"] == 40.0
    assert energy["ai_fan_wh"] == pytest.approx(40.0 * 10 / 3600, abs=1e-3)
