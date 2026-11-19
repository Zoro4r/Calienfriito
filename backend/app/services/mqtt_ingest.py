"""
MQTT ingest — telemetry from the ESP32 node into the same pipeline as HTTP readings.

The node (hardware/firmware/src/sketch.ino) publishes one JSON per control cycle on
`ccs/<room>/telemetry`. This service subscribes to MQTT_TOPIC (default
`ccs/+/telemetry`), translates each message into a SensorReading and hands it to
process_reading — exactly what POST /api/v1/sensors/ does — so the reading is stored
in MongoDB and gets the backend's cognitive_action (ML / heuristic, run in shadow).

The node decides the actuators locally (heuristic with hysteresis + CO alarm). That
decision travels in the message and is stored as `node_state`, next to the backend's
`cognitive_action`, so both logics can be compared on the same reading.

Payload fields used (see publicarTelemetria in the firmware):
    temp_c, humedad, co2_ppm, co_ppm, is_simulated   -> SensorReading
    accion, motivo, alarma_co, ventilador_pwm, ...   -> node_state
"""
import asyncio
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError

from app.config import MQTT_ENABLED, MQTT_HOST, MQTT_PORT, MQTT_TOPIC, MQTT_OCCUPANCY_TOPIC
from app.models.sensor import SensorReading

logger = logging.getLogger(__name__)

RETRY_SECONDS = 5

# Node decision fields copied verbatim into node_state.
NODE_STATE_FIELDS = (
    "accion", "motivo", "alarma_co", "ventilador_pwm", "ventilador_rpm", "calefactor", "buzzer", "led",
    "bloqueo_s", "pendiente", "ciclo", "uptime_s", "co2_proxy", "manual", "manual_restante_s", "umbral_ocupacion", "umbral_origen",
    # Thermal band in force on the node (it follows the room's target temperature).
    "setpoint_c", "t_enfriar_c", "t_calentar_c", "t_nivel_75_c", "t_nivel_100_c", "setpoint_origen",
    "ventilador_conectado", "calefactor_conectado", "buzzer_conectado",
    "ventilador_pwm_objetivo", "calefactor_objetivo", "buzzer_objetivo",
    # Remote-order authority and acknowledgement (newer firmware), and the node's own
    # occupancy reading, independent of the actuator it drives.
    "ocupado", "autoridad", "comando_id", "comando_estado", "fallo_dht",
    # prediccion_ventilador.ino contract, translated below to the fields above.
    "ventilador_pct", "orden_restante_s", "calibracion_valida", "mq7_raw",
)


def unify_node_state(data: dict) -> dict:
    """One node_state shape whichever firmware sent it.

    prediccion_ventilador.ino reports `ventilador_pct`, `autoridad` and `orden_restante_s`
    where nodo.ino.tmpl reports `ventilador_pwm` (0-255), `manual` and `manual_restante_s`.
    The original fields are kept; the missing equivalents are filled in, never overwritten.
    """
    ns = {k: data[k] for k in NODE_STATE_FIELDS if k in data}
    pct = ns.get("ventilador_pct")
    if "ventilador_pwm" not in ns and isinstance(pct, (int, float)) and not isinstance(pct, bool):
        ns["ventilador_pwm"] = round(max(0.0, min(100.0, float(pct))) * 255 / 100)
    authority = ns.get("autoridad")
    if "manual" not in ns and authority in ("local", "manual", "predictivo"):
        ns["manual"] = authority != "local"
    if "manual_restante_s" not in ns and "orden_restante_s" in ns:
        ns["manual_restante_s"] = ns["orden_restante_s"]
    return ns


def declared_origin(data: dict) -> str:
    """physical only when the firmware itself says `is_simulated: false`; unknown when it says nothing."""
    flag = data.get("is_simulated")
    if flag is True:
        return "simulated"
    return "physical" if flag is False else "unknown"

# Node connectivity (RF-10), from ccs/<room>/status: "online" on connect and the
# retained Last Will "offline" the broker publishes when the node stops answering.
STATUS_TOPIC = "ccs/+/status"
NODE_STATUS: dict[str, dict] = {}

# Latest visual count per room. The next thermal telemetry message consumes this
# value and persists it beside the environmental reading. A short TTL prevents a
# disconnected camera from being presented as a live count indefinitely.
OCCUPANCY_TTL_SECONDS = 120.0
MIN_OCCUPANCY_CONFIDENCE = 0.50
OCCUPANCY_STATE: dict[str, dict] = {}

_task: asyncio.Task | None = None


def occupancy_update(topic: str, payload: bytes | str) -> dict | None:
    """Validate and cache one `ccs/<room>/occupancy` message."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[2] != "occupancy" or not parts[1]:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        logger.warning("MQTT: invalid occupancy JSON on %s", topic)
        return None
    if not isinstance(data, dict) or isinstance(data.get("people_count"), bool):
        return None
    try:
        count = int(data["people_count"])
        confidence = float(data.get("confidence", 1.0))
    except (KeyError, TypeError, ValueError):
        return None
    if count < 0 or confidence < MIN_OCCUPANCY_CONFIDENCE or confidence > 1.0:
        return None
    entry = {
        "room_id": parts[1],
        "people_count": count,
        "confidence": confidence,
        "ts": data.get("ts"),
        "received_at": time.monotonic(),
    }
    OCCUPANCY_STATE[parts[1]] = entry
    return entry


def latest_camera_occupancy(room_id: str, now: float | None = None) -> int | None:
    entry = OCCUPANCY_STATE.get(room_id)
    if entry is None:
        return None
    clock = time.monotonic() if now is None else now
    if clock - entry["received_at"] > OCCUPANCY_TTL_SECONDS:
        return None
    return entry["people_count"]


def status_update(topic: str, payload: bytes | str, retained: bool = False) -> dict | None:
    """Record an `online` / `offline` message. Returns the stored entry, or None if ignored."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[2] != "status" or not parts[1]:
        return None
    text = (payload.decode() if isinstance(payload, (bytes, bytearray)) else str(payload)).strip().lower()
    if text not in ("online", "offline"):
        return None
    entry = {
        "room_id": parts[1],
        "online": text == "online",
        # A retained message is replayed on (re)subscribe: its real time is unknown.
        "since": None if retained else datetime.now(timezone.utc).isoformat(),
    }
    NODE_STATUS[parts[1]] = entry
    return entry


async def publish_command(room_id: str, payload: dict) -> None:
    """Publish a retained command to ccs/<room>/command (RF-26; retained for RF-12)."""
    import aiomqtt

    async with aiomqtt.Client(MQTT_HOST, MQTT_PORT, identifier=f"ccs-cmd-{uuid.uuid4().hex[:8]}") as client:
        await client.publish(f"ccs/{room_id}/command", json.dumps(payload), qos=1, retain=True)


async def publish_sim(room_id: str, payload: dict) -> None:
    """Simulated sensor values for the room's node (ccs/<room>/sim, not retained): while they
    keep arriving, the node uses them instead of its sensors and its actuators answer to them."""
    import aiomqtt

    async with aiomqtt.Client(MQTT_HOST, MQTT_PORT, identifier=f"ccs-sim-{uuid.uuid4().hex[:8]}") as client:
        await client.publish(f"ccs/{room_id}/sim", json.dumps(payload), qos=0, retain=False)


async def publish_settings(room_id: str, payload: dict) -> None:
    """Publish the node's retained settings to ccs/<room>/settings (e.g. its occupancy threshold).

    Retained, so a node that is offline gets them when it reconnects; it also keeps them in flash.
    """
    import aiomqtt

    async with aiomqtt.Client(MQTT_HOST, MQTT_PORT, identifier=f"ccs-set-{uuid.uuid4().hex[:8]}") as client:
        await client.publish(f"ccs/{room_id}/settings", json.dumps(payload), qos=1, retain=True)


def room_settings(adc: int | None, target_c: float | None) -> dict:
    """What a room sets on its node. None (JSON null) hands that value back to the node:
    its calibration for the threshold, its factory setpoint for the temperature."""
    return {"umbral_ocupacion": adc, "temperatura_objetivo": target_c}


async def publish_room_settings(room_id: str, adc: int | None, target_c: float | None) -> None:
    """The room's settings for its node, retained on ccs/<room>/settings, so they reach it
    within seconds or when it reconnects. Never raises."""
    from app.config import MQTT_ENABLED
    if not MQTT_ENABLED:
        return
    try:
        await publish_settings(room_id, room_settings(adc, target_c))
    except Exception as exc:  # broker down: the next save or restart publishes it again
        logger.warning("Could not publish node settings for %s: %s", room_id, exc)


async def sync_room_settings() -> None:
    """Publish every room's settings once, at startup."""
    from app.config import MQTT_ENABLED
    if not MQTT_ENABLED:
        return
    from app.database_sql import SessionLocal
    from app.models.admin import Room
    try:
        db = SessionLocal()
        try:
            rooms = [(r.id, r.occupancy_threshold, r.target_temp) for r in db.query(Room).all()]
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Could not read room settings: %s", exc)
        return
    for room_id, adc, target_c in rooms:
        await publish_room_settings(room_id, adc, target_c)


def telemetry_to_reading(topic: str, payload: bytes | str, camera_occupancy: int | None = None) -> SensorReading | None:
    """Translate one node telemetry message into a SensorReading.

    Returns None (and the message is skipped) when the topic is not
    `ccs/<room>/telemetry` or the payload is not valid JSON. A reading without DHT data
    (temp_c/humedad null) is kept: CO2, CO and the node's state still matter.
    """
    parts = topic.split("/")
    if len(parts) != 3 or parts[2] != "telemetry" or not parts[1]:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        logger.warning("MQTT: payload is not valid JSON on %s", topic)
        return None
    if not isinstance(data, dict):
        return None

    def optional_ina(key: str) -> float | None:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    try:
        return SensorReading(
            # Seed convention: one device per room, id "s-<room>" (e.g. s-A403).
            sensor_id=f"s-{parts[1]}",
            temperature=float(data["temp_c"]) if data.get("temp_c") is not None else None,
            humidity=float(data["humedad"]) if data.get("humedad") is not None else None,
            co2_ppm=data.get("co2_ppm"),
            co_ppm=float(data.get("co_ppm") or 0.0),
            ina_voltage_v=optional_ina("ina_voltage_v"),
            ina_current_ma=optional_ina("ina_current_ma"),
            ina_power_mw=optional_ina("ina_power_mw"),
            camera_occupancy=(camera_occupancy if camera_occupancy is not None else latest_camera_occupancy(parts[1])),
            is_simulated=bool(data.get("is_simulated", False)),
            node_state=unify_node_state(data),
        )
    except (TypeError, ValueError, ValidationError) as exc:
        logger.warning("MQTT: invalid telemetry on %s: %s", topic, exc)
        return None


async def _ingest(topic: str, payload: bytes) -> None:
    reading = telemetry_to_reading(topic, payload)
    if reading is None:
        return
    try:
        origin = declared_origin(json.loads(payload))
    except (TypeError, ValueError):
        origin = "unknown"
    from app.database import get_db
    from app.database_sql import SessionLocal
    from app.services.data_service import process_reading

    db_sql = SessionLocal()
    try:
        from fastapi.concurrency import run_in_threadpool
        from app.services.smart_plug import room_uses_plug, with_plug_fan

        # A plug room's node has no fan pin: store the fan as the plug runs it.
        if (reading.node_state and reading.node_state.get("ventilador_conectado") is False
                and await run_in_threadpool(room_uses_plug, db_sql, reading.sensor_id)):
            reading.node_state = with_plug_fan(reading.node_state)
        result = await process_reading(get_db(), reading, db_sql=db_sql, origin=origin)
        # The plug follows the node's own decision, so it syncs even when the room's
        # server-side control is disabled (no room_context then, only room_id).
        room_id = result.get("room_id") if isinstance(result, dict) else None
        if room_id and reading.node_state:
            from app.services.smart_plug import sync_from_telemetry

            await sync_from_telemetry(db_sql, room_id, reading.node_state,
                                      is_simulated=reading.is_simulated)
    finally:
        db_sql.close()


async def _run() -> None:
    import aiomqtt

    client_id = f"ccs-backend-{uuid.uuid4().hex[:8]}"
    while True:
        try:
            async with aiomqtt.Client(MQTT_HOST, MQTT_PORT, identifier=client_id) as client:
                await client.subscribe(MQTT_TOPIC)
                await client.subscribe(MQTT_OCCUPANCY_TOPIC)
                await client.subscribe(STATUS_TOPIC)
                logger.info("MQTT ingest connected to %s:%d, subscribed to %s, %s and %s",
                            MQTT_HOST, MQTT_PORT, MQTT_TOPIC, MQTT_OCCUPANCY_TOPIC, STATUS_TOPIC)
                async for message in client.messages:
                    try:
                        topic = str(message.topic)
                        if topic.endswith("/status"):
                            entry = status_update(topic, message.payload, retained=bool(message.retain))
                            if entry:
                                logger.info("[NODE] %s %s", entry["room_id"], "online" if entry["online"] else "offline")
                            continue
                        if topic.endswith("/occupancy"):
                            entry = occupancy_update(topic, message.payload)
                            if entry:
                                logger.info("[OCCUPANCY] %s=%d confidence=%.2f",
                                            entry["room_id"], entry["people_count"], entry["confidence"])
                            continue
                        await _ingest(topic, message.payload)
                    except Exception:  # one bad message must not stop the subscriber
                        logger.exception("MQTT: could not ingest message on %s", message.topic)
        except aiomqtt.MqttError as exc:
            logger.warning("MQTT broker %s:%d unavailable (%s); retrying in %d s",
                           MQTT_HOST, MQTT_PORT, exc, RETRY_SECONDS)
            await asyncio.sleep(RETRY_SECONDS)


def start_mqtt_ingest() -> None:
    global _task
    if not MQTT_ENABLED:
        logger.info("MQTT ingest disabled (MQTT_ENABLED=false).")
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_run())


async def stop_mqtt_ingest() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
