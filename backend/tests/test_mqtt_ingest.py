"""Translation of ESP32 MQTT telemetry into SensorReading (app.services.mqtt_ingest)."""
import json

from app.services.mqtt_ingest import telemetry_to_reading, OCCUPANCY_STATE, occupancy_update, latest_camera_occupancy

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


def test_keeps_reading_without_dht():
    # A failed DHT22 must not hide what the node does measure (CO2, CO, its state).
    data = {**TELEMETRY, "temp_c": None, "humedad": None}
    r = telemetry_to_reading("ccs/A403/telemetry", json.dumps(data))
    assert r is not None
    assert r.temperature is None and r.humidity is None
    assert r.co_ppm == float(TELEMETRY["co_ppm"])


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


def test_ina_measurements_are_optional_and_do_not_replace_other_telemetry():
    data = {**TELEMETRY, "ina_voltage_v": 12.1, "ina_current_ma": 185.0, "ina_power_mw": 2238.5}
    reading = telemetry_to_reading("ccs/A403/telemetry", json.dumps(data))
    assert (reading.ina_voltage_v, reading.ina_current_ma, reading.ina_power_mw) == (12.1, 185.0, 2238.5)
    assert reading.node_state["accion"] == "COOLING"
    without_ina = telemetry_to_reading("ccs/A403/telemetry", json.dumps(TELEMETRY))
    assert without_ina.ina_power_mw is None
    bad_ina = telemetry_to_reading("ccs/A403/telemetry", json.dumps({**data, "ina_power_mw": "nan"}))
    assert bad_ina is not None and bad_ina.ina_power_mw is None


def test_occupancy_message_is_cached_and_attached_to_next_telemetry():
    OCCUPANCY_STATE.clear()
    entry = occupancy_update("ccs/A403/occupancy", '{"people_count": 8, "confidence": 0.91}')
    assert entry["people_count"] == 8
    assert latest_camera_occupancy("A403", now=entry["received_at"]) == 8
    r = telemetry_to_reading("ccs/A403/telemetry", json.dumps(TELEMETRY))
    assert r.camera_occupancy == 8


def test_invalid_occupancy_is_ignored():
    OCCUPANCY_STATE.clear()
    assert occupancy_update("ccs/A403/occupancy", '{"people_count": -1}') is None
    assert occupancy_update("ccs/A403/occupancy", '{"people_count": 8, "confidence": 1.5}') is None
    assert occupancy_update("ccs/A403/occupancy", '{"people_count": 8, "confidence": 0.4}') is None
    assert latest_camera_occupancy("A403") is None


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


def test_anomaly_check_tolerates_missing_dht():
    from app.models.sensor import SensorReading
    from app.services.data_service import detect_anomaly
    assert detect_anomaly(SensorReading(sensor_id="s-X", co_ppm=10)) is False
    assert detect_anomaly(SensorReading(sensor_id="s-X", co_ppm=60)) is True
