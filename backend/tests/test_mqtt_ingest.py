"""Translation of ESP32 MQTT telemetry into SensorReading (app.services.mqtt_ingest)."""
import json

from app.services.mqtt_ingest import telemetry_to_reading

TELEMETRY = {
    "ciclo": 42, "uptime_s": 44.0, "temp_c": 25.6, "humedad": 55, "co2_proxy": 3535,
    "co2_ppm": 1781, "co_ppm": 4, "accion": "COOLING", "alarma_co": False,
    "ventilador_pwm": 255, "calefactor": False, "buzzer": False, "led": "AZUL (enfriando)",
    "motivo": "Aula ocupada y T >= 24.5 C: ventilador ON", "bloqueo_s": 0, "pendiente": "",
    "is_simulated": True,
}


def test_maps_sensor_fields_and_room():
    r = telemetry_to_reading("ccs/A403/telemetry", json.dumps(TELEMETRY))
    assert r is not None
    assert r.sensor_id == "s-A403"
    assert r.temperature == 25.6
    assert r.humidity == 55.0
    assert r.co2_ppm == 1781
    assert r.co_ppm == 4.0
    assert r.is_simulated is True


def test_keeps_node_decision_in_node_state():
    r = telemetry_to_reading("ccs/A403/telemetry", json.dumps(TELEMETRY).encode())
    assert r.node_state["accion"] == "COOLING"
    assert r.node_state["motivo"].startswith("Aula ocupada")
    assert r.node_state["ventilador_pwm"] == 255
    assert "temp_c" not in r.node_state  # sensor values are not duplicated


def test_keeps_node_occupancy_threshold():
    msg = {"temp_c": 27.9, "humedad": 74, "co2_proxy": 219, "umbral_ocupacion": 260}
    r = telemetry_to_reading("ccs/B101/telemetry", json.dumps(msg))
    assert r.node_state["umbral_ocupacion"] == 260


def test_skips_reading_without_dht():
    data = {**TELEMETRY, "temp_c": None, "humedad": None}
    assert telemetry_to_reading("ccs/A403/telemetry", json.dumps(data)) is None


def test_skips_other_topics_and_bad_payloads():
    assert telemetry_to_reading("ccs/A403/status", "online") is None
    assert telemetry_to_reading("ccs/telemetry", json.dumps(TELEMETRY)) is None
    assert telemetry_to_reading("ccs/A403/telemetry", "not json") is None
    assert telemetry_to_reading("ccs/A403/telemetry", "[1, 2]") is None


def test_missing_optional_fields_use_defaults():
    r = telemetry_to_reading("ccs/B201/telemetry", json.dumps({"temp_c": 22, "humedad": 40}))
    assert r.sensor_id == "s-B201"
    assert r.co2_ppm is None
    assert r.co_ppm == 0.0
    assert r.is_simulated is False
    assert r.node_state == {}


# ── Node connectivity (RF-10) ────────────────────────────────────────────────
from app.services.mqtt_ingest import NODE_STATUS, status_update  # noqa: E402


def test_status_online_and_last_will_offline():
    NODE_STATUS.clear()
    assert status_update("ccs/A403/status", b"online")["online"] is True
    entry = status_update("ccs/A403/status", b"offline")
    assert entry["online"] is False and entry["since"] is not None
    assert NODE_STATUS["A403"]["online"] is False


def test_retained_status_has_unknown_time_and_bad_messages_are_ignored():
    NODE_STATUS.clear()
    assert status_update("ccs/A403/status", b"offline", retained=True)["since"] is None
    assert status_update("ccs/A403/status", b"rebooting") is None
    assert status_update("ccs/A403/telemetry", b"online") is None
