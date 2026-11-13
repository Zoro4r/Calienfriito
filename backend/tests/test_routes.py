"""Route-level smoke tests: wiring, auth gates and RBAC — no databases required.

The TestClient is used WITHOUT its context manager on purpose: startup hooks
(Postgres retries, Mongo, model load) never run, and every assertion below is
reachable before any database query executes. Full-stack behavior is covered
by the live deployment; these tests pin the HTTP contract.
"""
import types

import pytest

# Skip cleanly in environments without the API stack (e.g. a minimal local
# venv); CI installs backend/requirements-dev.txt and runs everything.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import app.dependencies as deps  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _fake_user(role: str):
    return types.SimpleNamespace(id=1, username=f"test-{role}", role=role, status="active")


@pytest.fixture
def as_role():
    """Override auth to impersonate a role; restores the real dependency after."""
    def _impersonate(role: str):
        app.dependency_overrides[deps.get_current_user] = lambda: _fake_user(role)
    yield _impersonate
    app.dependency_overrides.clear()


def test_health_is_public_and_reports_engine():
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["engine"] in ("ml", "heuristic")


@pytest.mark.parametrize("path", [
    "/api/v1/admin/users",
    "/api/v1/admin/reservations",
    "/api/v1/reports/roi",
    "/api/v1/sensors/emergencies",
])
def test_protected_routes_reject_missing_token(path):
    assert client.get(path).status_code == 401


def test_guest_cannot_list_users(as_role):
    as_role("guest")
    assert client.get("/api/v1/admin/users").status_code == 403


def test_guest_cannot_list_reservations(as_role):
    as_role("guest")
    assert client.get("/api/v1/admin/reservations").status_code == 403


def test_guest_cannot_read_roi(as_role):
    as_role("guest")
    assert client.get("/api/v1/reports/roi").status_code == 403


def test_collaborator_cannot_reload_model(as_role):
    as_role("collaborator")
    assert client.post("/api/v1/admin/model/reload").status_code == 403


def test_admin_can_reload_model(as_role):
    # No DB involved: reload_model only touches the model file on disk.
    as_role("admin")
    res = client.post("/api/v1/admin/model/reload")
    assert res.status_code == 200
    assert res.json()["engine"] in ("ml", "heuristic")


def test_collaborator_cannot_read_node_config(as_role):
    as_role("collaborator")
    assert client.get("/api/v1/admin/node-config").status_code == 403


def test_node_config_serves_broker_without_password(as_role, monkeypatch):
    from app.routes import admin as admin_routes
    monkeypatch.setattr(admin_routes, "NODE_MQTT_HOST", "broker.example")
    as_role("admin")
    res = client.get("/api/v1/admin/node-config")
    assert res.status_code == 200
    assert res.json() == {"host": "broker.example", "port": 8883, "user": "nodo", "tls": True}


def test_node_config_without_public_broker(as_role, monkeypatch):
    from app.routes import admin as admin_routes
    monkeypatch.setattr(admin_routes, "NODE_MQTT_HOST", "")
    as_role("admin")
    assert client.get("/api/v1/admin/node-config").json()["host"] is None


def test_node_firmware_fills_room_and_broker_only(as_role, monkeypatch):
    from app.routes import admin as admin_routes
    monkeypatch.setattr(admin_routes, "NODE_MQTT_HOST", "broker.example")
    as_role("admin")
    res = client.get("/api/v1/admin/node-firmware", params={"room": "B101"})
    assert res.status_code == 200
    code = res.text
    assert '#define AULA "B101"' in code
    assert 'MQTT_HOST = "broker.example"' in code and "MQTT_PORT = 8883" in code and 'MQTT_USER = "nodo"' in code
    # WiFi and broker password are filled in the browser, never by the server.
    assert "__WIFI_SSID__" in code and "__WIFI_PASS__" in code and "__MQTT_PASS__" in code
    assert "__AULA__" not in code and "__MQTT_HOST__" not in code


@pytest.mark.parametrize("room", ["B 101", "B#1", "ccs/+", "x" * 21])
def test_node_firmware_rejects_unsafe_room_codes(as_role, monkeypatch, room):
    from app.routes import admin as admin_routes
    monkeypatch.setattr(admin_routes, "NODE_MQTT_HOST", "broker.example")
    as_role("admin")
    assert client.get("/api/v1/admin/node-firmware", params={"room": room}).status_code == 422


def test_node_firmware_needs_public_broker(as_role, monkeypatch):
    from app.routes import admin as admin_routes
    monkeypatch.setattr(admin_routes, "NODE_MQTT_HOST", "")
    as_role("admin")
    assert client.get("/api/v1/admin/node-firmware", params={"room": "B101"}).status_code == 409


def test_collaborator_cannot_download_node_firmware(as_role):
    as_role("collaborator")
    assert client.get("/api/v1/admin/node-firmware", params={"room": "B101"}).status_code == 403


def test_collaborator_cannot_change_node_settings(as_role):
    as_role("collaborator")
    res = client.put("/api/v1/sensors/rooms/B101/node-settings", json={"umbral_ocupacion": 260})
    assert res.status_code == 403


@pytest.mark.parametrize("value", [0, 4096, -5])
def test_node_settings_reject_out_of_range(as_role, value):
    as_role("admin")
    res = client.put("/api/v1/sensors/rooms/B101/node-settings", json={"umbral_ocupacion": value})
    assert res.status_code == 422


def test_admin_publishes_node_settings(as_role, monkeypatch):
    sent = []

    async def fake_publish(room_id, payload):
        sent.append((room_id, payload))
    monkeypatch.setattr("app.config.MQTT_ENABLED", True)
    monkeypatch.setattr("app.services.mqtt_ingest.publish_settings", fake_publish)
    as_role("admin")
    res = client.put("/api/v1/sensors/rooms/B101/node-settings", json={"umbral_ocupacion": 260})
    assert res.status_code == 200
    assert sent == [("B101", {"umbral_ocupacion": 260})]


def test_collaborator_cannot_add_classes(as_role):
    as_role("collaborator")
    r = client.post("/api/v1/admin/rooms/B101/schedules",
                    json={"day_of_week": 0, "start_time": "08:00", "end_time": "10:00", "expected_people": 20})
    assert r.status_code == 403


def test_class_payload_is_validated(as_role):
    as_role("admin")
    r = client.post("/api/v1/admin/rooms/B101/schedules",
                    json={"day_of_week": 6, "start_time": "8:00", "end_time": "10:00", "expected_people": 0})
    assert r.status_code == 422


def test_sensor_ingest_rejects_wrong_api_key(monkeypatch):
    monkeypatch.setattr(deps, "WATSON_EXTENSION_KEY", "expected-key")
    res = client.post("/api/v1/sensors/", json={}, headers={"X-API-Key": "wrong"})
    assert res.status_code == 403


def test_sensor_ingest_validates_payload_when_key_matches(monkeypatch):
    monkeypatch.setattr(deps, "WATSON_EXTENSION_KEY", "expected-key")
    # Correct key but empty body → schema validation fires (422), no DB touched.
    res = client.post("/api/v1/sensors/", json={}, headers={"X-API-Key": "expected-key"})
    assert res.status_code == 422


# ── Chat: JWT passthrough to Watson actions ───────────────────────────────────

@pytest.fixture
def watson_capture(monkeypatch):
    """Stub Watson and capture the skill_variables the route forwards."""
    from app.services import watson_service
    captured: dict = {}

    async def fake_create_session():
        return "sess-test"

    async def fake_send_message(session_id, text, skill_variables=None):
        captured["session_id"] = session_id
        captured["skill_variables"] = skill_variables
        return "ok"

    monkeypatch.setattr(watson_service, "create_session", fake_create_session)
    monkeypatch.setattr(watson_service, "send_message", fake_send_message)
    return captured


def test_chat_forwards_identity_from_valid_jwt(watson_capture):
    from app.core.security import create_access_token
    token = create_access_token({"sub": "kiara", "role": "collaborator"})
    res = client.post(
        "/api/v1/chat/",
        json={"message": "hola"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    vars_ = watson_capture["skill_variables"]
    assert vars_["jwt"] == token
    assert vars_["user_role"] == "collaborator"
    assert vars_["username"] == "kiara"


def test_chat_without_token_is_anonymous_but_works(watson_capture):
    res = client.post("/api/v1/chat/", json={"message": "hola"})
    assert res.status_code == 200
    vars_ = watson_capture["skill_variables"]
    assert vars_ == {"jwt": "", "user_role": "anonymous", "username": ""}


def test_chat_forwards_undecodable_token_as_anonymous(watson_capture):
    # Garbage token: identity falls back to anonymous but the opaque token is
    # still forwarded — protected endpoints are the ones that must reject it.
    res = client.post(
        "/api/v1/chat/",
        json={"message": "hola"},
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert res.status_code == 200
    vars_ = watson_capture["skill_variables"]
    assert vars_["jwt"] == "not-a-jwt"
    assert vars_["user_role"] == "anonymous"


def test_downsample_keeps_first_and_last():
    from app.routes.sensors import downsample
    pts = [{"i": i} for i in range(100)]
    out = downsample(pts, 10)
    assert len(out) == 10
    assert out[0]["i"] == 0 and out[-1]["i"] == 99
    assert downsample(pts[:5], 10) == pts[:5]
