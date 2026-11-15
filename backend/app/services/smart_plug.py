"""Reconcile one Smart Life plug with the effective decision reported by a node.

The ESP32 is still responsible for rules, manual overrides, learning orders and CO
priority. We use its *fresh telemetry*, not retained MQTT commands, so a replayed
command can never switch the mains plug on after the order has expired.
"""
import asyncio
from functools import partial
import logging
import time
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool

from app import config
from app.models.admin import Room

logger = logging.getLogger(__name__)
RECHECK_SECONDS = 300  # 8,640 status calls/month at most, before actual changes
RETRY_SECONDS = 30

_lock = asyncio.Lock()
_client = None
_state: dict = {"status": "awaiting_telemetry", "room_id": None, "desired_on": None,
                "actual_on": None, "checked_at": None}
_next_check = 0.0
_last_desired: bool | None = None
_last_room: str | None = None


def configured() -> bool:
    return bool(config.TUYA_API_ENDPOINT and config.TUYA_ACCESS_ID and config.TUYA_ACCESS_SECRET
                and config.TUYA_DEVICE_ID and config.TUYA_SWITCH_CODE)


def status() -> dict:
    """Non-secret status for the dashboard. No Tuya call is made here."""
    if not config.TUYA_ENABLED:
        return {"status": "disabled", "desired_on": None, "actual_on": None, "checked_at": None}
    if not configured():
        return {"status": "incomplete", "desired_on": None, "actual_on": None, "checked_at": None}
    info = dict(_state)
    if info["status"] == "confirmed" and info.get("checked_at"):
        checked = datetime.fromisoformat(info["checked_at"])
        if (datetime.now(timezone.utc) - checked).total_seconds() > 2 * RECHECK_SECONDS:
            info.update(status="stale", actual_on=None)
    return info


def desired_from_node(node_state: dict | None) -> bool | None:
    """Use the computed target when a fan is not electrically wired to the ESP32."""
    if not node_state:
        return None
    if node_state.get("alarma_co") is True:
        return True
    pwm = node_state.get("ventilador_pwm_objetivo")
    if pwm is None:
        pwm = node_state.get("ventilador_pwm")
    if isinstance(pwm, bool) or not isinstance(pwm, (int, float)):
        return None
    return pwm > 0


def _sole_smart_room(db_sql) -> str | None:
    rooms = db_sql.query(Room).filter(Room.fan_type == "smart_plug").limit(2).all()
    return rooms[0].id if len(rooms) == 1 else None


async def room_status(db_sql, room_id: str) -> dict:
    assigned = await run_in_threadpool(_sole_smart_room, db_sql)
    info = status()
    if assigned != room_id:
        return {"room_id": room_id, "status": "unassigned" if assigned else "ambiguous_room",
                "desired_on": None, "actual_on": None, "checked_at": None}
    if info.get("room_id") != room_id and info["status"] not in ("disabled", "incomplete"):
        return {"room_id": room_id, "status": "awaiting_telemetry", "desired_on": None,
                "actual_on": None, "checked_at": None}
    return {"room_id": room_id, **info}


def _tuya_client():
    global _client
    if _client is None:
        from tuya_connector import TuyaOpenAPI

        client = TuyaOpenAPI(config.TUYA_API_ENDPOINT, config.TUYA_ACCESS_ID, config.TUYA_ACCESS_SECRET)
        client.session.request = partial(client.session.request, timeout=8)
        response = client.connect()
        if not isinstance(response, dict) or not response.get("success"):
            raise RuntimeError("Tuya authentication failed")
        _client = client
    return _client


def _request(client, method: str, path: str, body: dict | None = None):
    response = client.post(path, body) if method == "POST" else client.get(path)
    if not isinstance(response, dict) or not response.get("success"):
        # Do not log a raw Tuya response: provider messages may contain identifiers.
        raise RuntimeError(f"Tuya {method} failed")
    return response.get("result")


def _read_switch(client) -> bool:
    path = f"/v1.0/iot-03/devices/{config.TUYA_DEVICE_ID}/status"
    rows = _request(client, "GET", path)
    values = [row.get("value") for row in rows or [] if row.get("code") == config.TUYA_SWITCH_CODE]
    if len(values) != 1 or not isinstance(values[0], bool):
        raise RuntimeError("Tuya switch status unavailable or not Boolean")
    return values[0]


def _reconcile(desired: bool) -> bool:
    client = _tuya_client()
    actual = _read_switch(client)
    if actual != desired:
        path = f"/v1.0/iot-03/devices/{config.TUYA_DEVICE_ID}/commands"
        result = _request(client, "POST", path, {"commands": [{"code": config.TUYA_SWITCH_CODE, "value": desired}]})
        if result is not True:
            raise RuntimeError("Tuya did not accept switch command")
        # Tuya may acknowledge before the device reports its new state. The next
        # telemetry pass rechecks shortly if the reported state has not caught up.
        actual = _read_switch(client)
    return actual


async def sync_from_telemetry(db_sql, room_id: str, node_state: dict | None,
                              is_simulated: bool = False) -> None:
    global _next_check, _last_desired, _last_room
    if not config.TUYA_ENABLED or is_simulated:
        return
    if not configured():
        _state.update(status="incomplete")
        return
    desired = desired_from_node(node_state)
    if desired is None:
        return
    sole_room = await run_in_threadpool(_sole_smart_room, db_sql)
    if sole_room is None:
        _state.update(status="ambiguous_room", desired_on=None, actual_on=None)
        return
    if room_id != sole_room:
        return

    async with _lock:
        now = time.monotonic()
        if desired == _last_desired and room_id == _last_room and now < _next_check:
            return
        _state.update(status="pending", room_id=room_id, desired_on=desired)
        try:
            actual = await asyncio.to_thread(_reconcile, desired)
        except Exception:
            logger.exception("Could not reconcile Tuya plug for room %s", room_id)
            _state.update(status="unavailable", actual_on=None)
            _next_check = time.monotonic() + RETRY_SECONDS
            return
        _last_desired = desired
        _last_room = room_id
        confirmed = actual == desired
        _state.update(status="confirmed" if confirmed else "pending", actual_on=actual,
                      checked_at=datetime.now(timezone.utc).isoformat())
        _next_check = time.monotonic() + (RECHECK_SECONDS if confirmed else RETRY_SECONDS)
