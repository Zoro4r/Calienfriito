"""The cloud plug follows confirmed node decisions without a call every second."""
import asyncio

from app.services import smart_plug


def test_desired_state_uses_target_and_co_alarm():
    assert smart_plug.desired_from_node({"ventilador_pwm": 0, "ventilador_pwm_objetivo": 255}) is True
    assert smart_plug.desired_from_node({"ventilador_pwm": 0, "alarma_co": True}) is True
    assert smart_plug.desired_from_node({"ventilador_pwm": 0}) is False
    assert smart_plug.desired_from_node({"accion": "STANDBY"}) is None


def test_reconcile_sends_only_when_state_differs(monkeypatch):
    calls = []

    class Client:
        def get(self, path):
            calls.append(("GET", path))
            return {"success": True, "result": [{"code": "switch_1", "value": len([x for x in calls if x[0] == "POST"]) > 0}]}

        def post(self, path, body):
            calls.append(("POST", path, body))
            return {"success": True, "result": True}

    monkeypatch.setattr(smart_plug, "_tuya_client", lambda: Client())
    monkeypatch.setattr(smart_plug.config, "TUYA_DEVICE_ID", "plug-1")
    monkeypatch.setattr(smart_plug.config, "TUYA_SWITCH_CODE", "switch_1")
    assert smart_plug._reconcile(True) is True
    assert [c[0] for c in calls] == ["GET", "POST", "GET"]
    assert calls[1][2] == {"commands": [{"code": "switch_1", "value": True}]}


def test_telemetry_deduplicates_and_refuses_ambiguous_room(monkeypatch):
    monkeypatch.setattr(smart_plug.config, "TUYA_ENABLED", True)
    monkeypatch.setattr(smart_plug, "configured", lambda: True)
    monkeypatch.setattr(smart_plug, "_sole_smart_room", lambda db: "A101")
    calls = []
    monkeypatch.setattr(smart_plug, "_reconcile", lambda desired: calls.append(desired) or desired)
    monkeypatch.setattr(smart_plug, "_last_desired", None)
    monkeypatch.setattr(smart_plug, "_last_room", None)
    monkeypatch.setattr(smart_plug, "_next_check", 0.0)

    async def run():
        await smart_plug.sync_from_telemetry(None, "A101", {"ventilador_pwm": 255})
        await smart_plug.sync_from_telemetry(None, "A101", {"ventilador_pwm": 255})
        await smart_plug.sync_from_telemetry(None, "A101", {"ventilador_pwm": 0}, is_simulated=True)
        await smart_plug.sync_from_telemetry(None, "A102", {"ventilador_pwm": 0})
    asyncio.run(run())
    assert calls == [True]

    monkeypatch.setattr(smart_plug, "_sole_smart_room", lambda db: None)
    asyncio.run(smart_plug.sync_from_telemetry(None, "A101", {"ventilador_pwm": 0}))
    assert calls == [True]
    assert smart_plug.status()["status"] == "ambiguous_room"
