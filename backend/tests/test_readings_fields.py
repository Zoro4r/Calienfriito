from app.routes.sensors import CHART_PROJECTION, SensorReadingOut


def test_chart_projection_keeps_chart_fields():
    for key in ("timestamp", "sensor_id", "temperature", "humidity", "co2_ppm", "co_ppm", "is_simulated"):
        assert CHART_PROJECTION.get(key) == 1, key


def test_chart_projection_keeps_node_state_keys_used_by_the_ui():
    for sub in ("accion", "alarma_co", "manual", "co2_proxy", "uptime_s"):
        assert CHART_PROJECTION.get(f"node_state.{sub}") == 1, sub
    assert "node_state" not in CHART_PROJECTION  # only subfields, never the whole document


def test_chart_projection_drops_heavy_fields():
    assert CHART_PROJECTION.get("_id") == 0
    for key in ("cognitive_action", "learning_action", "ina_voltage_v", "ina_current_ma",
                "ina_power_mw", "camera_occupancy"):
        assert key not in CHART_PROJECTION, key


def test_projected_document_validates():
    doc = {"timestamp": "2026-11-17T10:00:00", "sensor_id": "s1", "temperature": 24.1,
           "node_state": {"accion": "COOLING", "co2_proxy": 1800, "uptime_s": 120}}
    out = SensorReadingOut(**doc)
    assert out.cognitive_action is None and out.learning_action is None
    assert SensorReadingOut(timestamp="2026-11-17T10:00:00", sensor_id="s1").node_state is None
