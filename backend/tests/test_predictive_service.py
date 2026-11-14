"""Unit tests for the dual-mode cooling engine (heuristic path — no model.joblib)."""
from datetime import datetime, timezone

import pytest

from app.services import predictive_service
from app.services.predictive_service import (
    FEEDBACK_WEIGHT,
    THERMAL_LOAD_PER_PERSON,
    calculate_cooling_demand,
)
from app.services.occupancy_service import CO2_BASELINE_PPM, CO2_PPM_PER_PERSON

TS = datetime(2026, 7, 2, 15, 0)


def make_context(**overrides):
    ctx = {
        "room_id": "A403",
        "target_temp": 20.0,
        "expected_people": 20,
        "max_capacity": 40,
        "control_policy": "auto",
    }
    ctx.update(overrides)
    return ctx


@pytest.fixture(autouse=True)
def force_heuristic(monkeypatch):
    # Ensure tests exercise the heuristic path regardless of a local model.joblib.
    monkeypatch.setattr(predictive_service, "_model", None)


async def _demand(**kwargs):
    return await calculate_cooling_demand(**kwargs)


@pytest.mark.asyncio
async def test_no_room_context_returns_standby_none():
    result = await _demand(current_temp=25.0, room_context=None, reading_timestamp=TS)
    assert result["ac_status"] == "STANDBY"
    assert result["model"] == "none"


@pytest.mark.asyncio
async def test_missing_target_temp_returns_none_model():
    ctx = make_context(target_temp=None)
    result = await _demand(current_temp=25.0, room_context=ctx, reading_timestamp=TS)
    assert result["model"] == "none"


@pytest.mark.asyncio
async def test_hot_room_turns_ac_on_precooling():
    result = await _demand(current_temp=28.0, room_context=make_context(), reading_timestamp=TS)
    assert result["ac_status"] == "ON"
    assert result["cooling_mode"] == "PRE-COOLING"
    assert result["model"] == "heuristic"


@pytest.mark.asyncio
async def test_cold_room_stays_standby():
    result = await _demand(current_temp=18.0, room_context=make_context(), reading_timestamp=TS)
    assert result["ac_status"] == "STANDBY"
    assert result["cooling_mode"] is None


@pytest.mark.asyncio
async def test_feedforward_only_without_co2():
    # No CO2 → effective occupancy is exactly the planned headcount.
    result = await _demand(current_temp=28.0, room_context=make_context(), reading_timestamp=TS)
    assert result["actual_occupancy"] is None
    assert result["effective_occupancy"] == 20.0
    assert result["thermal_load_offset"] == round(20 * THERMAL_LOAD_PER_PERSON, 3)


@pytest.mark.asyncio
async def test_feedback_blend_with_co2():
    # CO2 says 5 people in a room booked for 20 → demand is trimmed toward reality.
    co2 = CO2_BASELINE_PPM + 5 * CO2_PPM_PER_PERSON
    result = await _demand(
        current_temp=28.0, room_context=make_context(), reading_timestamp=TS, co2_ppm=co2
    )
    expected_blend = (1 - FEEDBACK_WEIGHT) * 20 + FEEDBACK_WEIGHT * 5
    assert result["actual_occupancy"] == 5
    assert result["effective_occupancy"] == round(expected_blend, 2)
    assert result["occupancy_gap"] == 15  # over-reservation / no-shows


@pytest.mark.asyncio
async def test_visual_occupancy_replaces_co2_proxy_when_available():
    co2 = CO2_BASELINE_PPM + 30 * CO2_PPM_PER_PERSON
    result = await _demand(
        current_temp=28.0, room_context=make_context(), reading_timestamp=TS,
        co2_ppm=co2, camera_occupancy=8,
    )
    assert result["actual_occupancy"] == 8
    assert result["effective_occupancy"] == round((1 - FEEDBACK_WEIGHT) * 20 + FEEDBACK_WEIGHT * 8, 2)


@pytest.mark.asyncio
async def test_target_adjusted_downward_by_thermal_load():
    result = await _demand(current_temp=28.0, room_context=make_context(), reading_timestamp=TS)
    assert result["target"] == round(20.0 - result["thermal_load_offset"], 2)


@pytest.mark.asyncio
async def test_manual_policy_holds_configured_target():
    ctx = make_context(control_policy="manual")
    result = await _demand(current_temp=28.0, room_context=ctx, reading_timestamp=TS)
    assert result["model"] == "manual"
    assert result["thermal_load_offset"] == 0.0
    assert result["target"] == 20.0


@pytest.mark.asyncio
async def test_timezone_aware_timestamp_accepted():
    aware = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
    result = await _demand(
        current_temp=28.0, room_context=make_context(), reading_timestamp=aware
    )
    assert result["ac_status"] == "ON"


def test_active_engine_reports_heuristic_without_model():
    assert predictive_service.active_engine() == "heuristic"


@pytest.mark.asyncio
async def test_ml_uses_registered_room_volume(monkeypatch):
    class Model:
        seen = None

        def predict(self, rows):
            self.seen = rows.tolist()
            return [0.5]

    model = Model()
    monkeypatch.setattr(predictive_service, "_model", model)
    monkeypatch.setattr(predictive_service, "_features", ["volume_m3"])
    monkeypatch.setattr(predictive_service, "_outdoor_temp", lambda ts: 25.0)
    result = await _demand(current_temp=28.0, room_context=make_context(volume_m3=192), reading_timestamp=TS)
    assert result["model"] == "ml"
    assert model.seen == [[192.0]]


def _dump_bundle(tmp_path, beats: bool):
    import joblib

    bundle = {
        "model": object(),  # stand-in estimator; never predicted against in these tests
        "features": ["temperature"],
        "room_id_map": {},
        "metadata": {"metrics": {"beats_baselines": beats}},
    }
    path = tmp_path / "model.joblib"
    joblib.dump(bundle, path)
    return path


def test_load_model_refuses_bundle_that_loses_to_baselines(tmp_path, monkeypatch):
    # Serve-side gate: beats_baselines=false → bundle rejected, heuristic stays.
    monkeypatch.setattr(predictive_service, "_MODEL_PATH", _dump_bundle(tmp_path, beats=False))
    predictive_service.load_model()
    assert predictive_service.active_engine() == "heuristic"


def test_load_model_serves_bundle_that_beats_baselines(tmp_path, monkeypatch):
    monkeypatch.setattr(predictive_service, "_MODEL_PATH", _dump_bundle(tmp_path, beats=True))
    predictive_service.load_model()
    assert predictive_service.active_engine() == "ml"
