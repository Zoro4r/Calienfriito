"""Safety of the predictive layer: who may act, stale orders, provenance, the forecast gate."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from app.models.sensor import SensorReading  # noqa: E402
from app.services import forecast_quality as fq  # noqa: E402
from app.services import room_controller as rc  # noqa: E402
from app.services.data_service import node_in_use, reading_origin  # noqa: E402
from app.services.mqtt_ingest import declared_origin, telemetry_to_reading, unify_node_state  # noqa: E402
from tests.test_room_controller import FakeDB  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def publish(room_id, payload):
        out.append((room_id, payload))
    monkeypatch.setattr("app.services.mqtt_ingest.publish_command", publish)
    rc._LAST_ACTION.clear()
    rc._FALLBACK.clear()
    return out


def learning_order(db, room, now):
    db["manual_orders"].docs[room] = {"_id": room, "mode": "fan", "minutes": 10, "level": 75,
                                      "started_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "by": rc.ORDER_BY}


# --- Authority -------------------------------------------------------------------------

def test_unvalidated_forecast_never_drives_and_cancels_its_order(sent):
    db, now = FakeDB(), datetime.utcnow()
    rc.remember_action("A403", now, {"fan_pct": 75, "control_enabled": True})
    learning_order(db, "A403", now)
    assert run(rc.send_learning_order(db, "A403", now)) is None     # no forecast_log evidence: gate closed
    assert sent == [("A403", {"modo": "auto"})]                     # the running learning order is cancelled
    assert db["manual_orders"].docs["A403"]["mode"] == "auto"
    assert "Datos insuficientes" in rc.fallback_reason("A403") or "persistencia" in rc.fallback_reason("A403")


def test_control_disabled_blocks_orders_even_with_open_gate(sent, monkeypatch):
    async def gate_open(db, room_id, now=None):
        return {"validated": True, "authorized": True, "reason": "ok"}
    monkeypatch.setattr("app.services.forecast_quality.room_quality", gate_open)
    db, now = FakeDB(), datetime.utcnow()
    rc.remember_action("A403", now, {"fan_pct": 75, "control_enabled": False})
    learning_order(db, "A403", now)
    assert run(rc.send_learning_order(db, "A403", now)) is None
    assert sent == [("A403", {"modo": "auto"})]
    assert "deshabilitado" in rc.fallback_reason("A403")


def test_cancel_leaves_a_person_order_alone(sent):
    db, now = FakeDB(), datetime.utcnow()
    db["manual_orders"].docs["A403"] = {"_id": "A403", "mode": "heater", "minutes": 30,
                                        "started_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "by": "admin"}
    assert run(rc.cancel_learning_order(db, "A403")) is False
    assert sent == []


def test_learning_orders_carry_authority_id_and_absolute_expiry(sent, monkeypatch):
    async def gate_open(db, room_id, now=None):
        return {"validated": True, "authorized": True, "reason": "ok"}
    monkeypatch.setattr("app.services.forecast_quality.room_quality", gate_open)
    db, now = FakeDB(), datetime(2026, 11, 17, 15, 0, 0)
    rc.remember_action("A403", now, {"fan_pct": 75})
    order = run(rc.send_learning_order(db, "A403", now))
    cmd = sent[-1][1]
    assert cmd["modo"] == "manual"                       # older firmware keeps understanding it
    assert cmd["autoridad"] == "predictivo"
    assert cmd["expira_utc"] - cmd["emitida_utc"] == rc.ORDER_MINUTES * 60
    assert cmd["emitida_utc"] == int((now - datetime(1970, 1, 1)).total_seconds())
    assert order["command_id"] == cmd["id"]


def test_manual_orders_are_tagged_manual():
    from app.routes.sensors import override_command
    cmd = override_command("fan", 30, "admin", 60)
    assert cmd["autoridad"] == "manual" and cmd["expira_utc"] - cmd["emitida_utc"] == 1800
    assert override_command("fan", 30, "admin")["id"] != cmd["id"]


# --- Occupancy is never read from the actuator ---------------------------------------

def test_occupancy_ignores_the_action_forced_by_an_order():
    assert node_in_use({"accion": "COOLING", "manual": True}) is None          # timetable decides
    assert node_in_use({"accion": "COOLING", "autoridad": "predictivo"}) is None
    assert node_in_use({"accion": "COOLING", "manual": True, "ocupado": False}) is False
    assert node_in_use({"accion": "STANDBY"}) is True                          # rules: action means people
    assert node_in_use({"accion": "DISABLED"}) is False
    assert node_in_use({"accion": "COOLING", "manual": True}, camera_occupancy=4) is True


# --- Provenance ------------------------------------------------------------------------

def test_origin_is_physical_only_when_the_firmware_declares_it():
    assert declared_origin({"is_simulated": False}) == "physical"
    assert declared_origin({"is_simulated": True}) == "simulated"
    assert declared_origin({}) == "unknown"
    r = SensorReading(sensor_id="s-A403", temperature=24.0, is_simulated=False)
    assert reading_origin(r) == "unknown"                 # HTTP / seed: provenance not verifiable
    assert reading_origin(r, "physical") == "physical"
    assert reading_origin(SensorReading(sensor_id="s", temperature=1.0, is_simulated=True), "physical") == "simulated"


def test_predictive_firmware_contract_is_unified():
    payload = json.dumps({"temp_c": 25.1, "humedad": 60, "accion": "COOLING", "autoridad": "predictivo",
                          "ventilador_pct": 60, "orden_restante_s": 120, "ocupado": True, "is_simulated": False})
    r = telemetry_to_reading("ccs/A403/telemetry", payload)
    ns = r.node_state
    assert ns["ventilador_pwm"] == 153 and ns["manual"] is True and ns["manual_restante_s"] == 120
    assert ns["ventilador_pct"] == 60 and ns["ocupado"] is True               # originals kept
    assert unify_node_state({"ventilador_pwm": 10, "ventilador_pct": 99})["ventilador_pwm"] == 10   # never overwritten


# --- The gate ------------------------------------------------------------------------

def log(minute, temp, fc, origin="physical", p90=None):
    return {"ts": datetime(2026, 11, 17) + timedelta(minutes=minute), "temp_now": temp, "forecast_30": fc,
            "forecast_30_p90": p90, "data_origin": origin, "model": "chronos-bolt-small"}


def rising(n, model_err=0.0):
    """Temperature rising 0.02 °C/min; the model forecasts it (plus an error), persistence does not."""
    return [log(5 * k, 24 + 0.1 * k, 24 + 0.1 * k + 0.6 + model_err, p90=24 + 0.1 * k + 0.8) for k in range(n)]


def test_score_pairs_each_forecast_with_the_reading_30_min_later():
    q = fq.score(rising(20))
    assert q["pairs"] == 14                                     # last 6 have no t+30 yet
    assert q["mae_model"] == 0.0 and q["mae_persistence"] == 0.6
    assert q["p90_peak_coverage"] == 1.0


def test_score_ignores_unknown_and_simulated_origins():
    docs = [dict(d, data_origin="unknown") for d in rising(20)] + [dict(d, data_origin="simulated") for d in rising(20)]
    assert fq.score(docs)["pairs"] == 0


def test_verdict_requires_enough_pairs_and_beating_persistence(monkeypatch):
    monkeypatch.delenv("PREDICTIVE_GATE", raising=False)
    few = fq.score(rising(20))
    assert fq.verdict(few, "chronos-bolt-small")["validated"] is False      # 14 < 36 pairs
    good = fq.score(rising(60))
    assert fq.verdict(good, "chronos-bolt-small")["validated"] is True
    bad = fq.score(rising(60, model_err=0.7))                                 # worse than persistence
    assert fq.verdict(bad, "chronos-bolt-small")["validated"] is False
    assert fq.verdict(good, "persistencia")["validated"] is False             # the baseline never validates itself


def test_gate_off_authorizes_without_claiming_validation(monkeypatch):
    monkeypatch.setenv("PREDICTIVE_GATE", "off")
    v = fq.verdict(fq.score([]), "chronos-bolt-small")
    assert v == {"validated": False, "authorized": True, "reason": v["reason"]}
    assert "PREDICTIVE_GATE=off" in v["reason"]


# --- Recommendations -------------------------------------------------------------------

def rec(**kw):
    base = dict(ready=True, temp_c=24.0, forecast_c=24.8, p90_c=25.0, in_use=True, co_alarm=False, validated=True)
    return fq.recommend(**{**base, **kw})["code"]


def test_recommendations_follow_explicit_rules():
    assert rec(co_alarm=True) == "alarma_co"
    assert rec(temp_c=None) == "sin_datos"
    assert rec(ready=False) == "sin_datos"
    assert rec(forecast_c=None) == "sin_datos"
    assert rec(validated=False) == "baja_confianza"
    assert rec(in_use=False) == "mantener"
    assert rec() == "aumentar"
    assert rec(temp_c=24.8, forecast_c=24.0, p90_c=24.2) == "reducir"
    assert rec(temp_c=23.0, forecast_c=23.2, p90_c=23.4) == "mantener"
