"""Training export: provenance kept, no labels, stored documents untouched."""
import pytest

pytest.importorskip("fastapi")


def test_training_export_keeps_provenance_and_has_no_labels():
    from app.routes.sensors import TRAINING_COLUMNS, training_export_body
    docs = [
        {"timestamp": "2026-11-17T15:00:00.000000", "sensor_id": "s-A403", "temperature": 24.1, "is_simulated": False,
         "data_origin": "physical", "node_state": {"accion": "COOLING", "ventilador_pwm": 255, "manual": False}},
        {"timestamp": "2026-11-17T15:00:05.000000", "sensor_id": "s-A403", "temperature": None, "is_simulated": False},
    ]
    body, rows = training_export_body(docs, "A403")
    assert body.splitlines()[0].split(",") == TRAINING_COLUMNS
    assert rows[0][3] == "physical" and rows[1][3] == "unknown"     # old documents are not upgraded
    assert rows[0][TRAINING_COLUMNS.index("fan_applied_pct")] == 100.0
    assert rows[0][TRAINING_COLUMNS.index("autoridad")] == "local"
    assert not any(c.startswith(("t_plus", "delta")) for c in TRAINING_COLUMNS)
    assert "temperature" not in docs[1] or docs[1]["temperature"] is None   # input untouched
