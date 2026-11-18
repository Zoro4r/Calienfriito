"""Who drives a node room: the node's own rules or the learning controller.

With the rules, the node decides alone and the learning controller runs in shadow.
With learning, the server turns the shadow decision into a short order every
ORDER_EVERY_S. Orders last ORDER_MINUTES, so if the server or the link goes down the
order expires and the node falls back to its rules by itself. The node keeps CO alarm
priority over any order.

Learning only drives while it is allowed to (`authorization`): the room's device has
control enabled and the forecast passes its gate (forecast_quality). When either stops
holding, the running learning order is cancelled ({"modo": "auto"}) and the rules take
over: that is the fallback, reported by GET /controller.

Orders carry `autoridad: "predictivo"`, an id and an absolute expiry (`expira_utc`), so
the node can tell them from a person's order and reject a stale retained one.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

CONTROLLERS = ("rules", "learning")
ORDER_EVERY_S = 300          # one order every 5 min
ORDER_MINUTES = 10           # each order outlives two ticks: a missed tick does not drop control
STALE_ACTION_S = 180         # older shadow decisions mean the node stopped reporting
ORDER_BY = "aprendizaje"     # author of the orders sent by the learning controller

# Latest shadow decision per room: (naive UTC time, summary from LearningController).
_LAST_ACTION: dict[str, tuple[datetime, dict]] = {}
# Why learning is not driving a room it was chosen for (None: it is driving).
_FALLBACK: dict[str, str | None] = {}
_task: asyncio.Task | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def remember_action(room_id: str, at: datetime, action: dict) -> None:
    """Called by the shadow step with each new decision."""
    _LAST_ACTION[room_id] = (at.replace(tzinfo=None), action)


def order_for(action: dict, fan_type: str = "pwm_12v") -> tuple[str, int]:
    """Manual order (mode, fan level) that reproduces a predictive decision.

    The fan at the requested power when the controller wants it on; otherwise "auto":
    nothing to anticipate, so the node's own rules decide (they still cool and keep the
    CO alarm). Fans stall under 20 %.
    """
    fan = float(action.get("fan_pct") or 0)
    heat = float(action.get("heater_pct") or 0)
    if heat >= 50:
        return "heater", 100
    if fan >= 20:
        return "fan", 100 if fan_type == "smart_plug" else int(min(100, round(fan)))
    return "auto", 100


async def get_controller(db, room_id: str) -> dict:
    doc = await db["room_controller"].find_one({"_id": room_id})
    if not doc:
        return {"mode": "rules", "by": None, "at": None}
    return {"mode": doc.get("mode", "rules"), "by": doc.get("by"), "at": doc.get("at")}


async def _user_order_running(db, room_id: str, now: datetime) -> bool:
    """A person's manual order is still running: the learning controller waits for it."""
    order = await db["manual_orders"].find_one({"_id": room_id})
    if not order or order.get("by") == ORDER_BY or order.get("mode") == "auto" or not order.get("minutes"):
        return False
    started = datetime.fromisoformat(order["started_at"])
    return now < started + timedelta(minutes=order["minutes"])


async def authorization(db, room_id: str, now: datetime | None = None) -> dict:
    """May the learning controller send orders to this room right now, and if not, why."""
    from app.services.forecast_quality import room_quality

    last = _LAST_ACTION.get(room_id)
    if last and last[1].get("control_enabled") is False:
        return {"authorized": False, "reason": "El control del nodo está deshabilitado: el modelo solo observa."}
    gate = await room_quality(db, room_id, now)
    return {"authorized": gate["authorized"], "reason": gate["reason"], "gate": gate}


async def cancel_learning_order(db, room_id: str, by: str = ORDER_BY, now: datetime | None = None) -> bool:
    """Hand the room back to the rules if the running order is the learning controller's.

    A person's manual order is left alone. Returns True when a cancel was published.
    """
    from app.services.mqtt_ingest import publish_command

    order = await db["manual_orders"].find_one({"_id": room_id})
    if not order or order.get("by") != ORDER_BY or order.get("mode") == "auto":
        return False
    at = (now or _utcnow()).strftime("%Y-%m-%dT%H:%M:%S")
    await publish_command(room_id, {"modo": "auto"})
    await db["manual_orders"].replace_one(
        {"_id": room_id},
        {"_id": room_id, "mode": "auto", "minutes": None, "level": None, "started_at": at, "by": by},
        upsert=True,
    )
    return True


async def send_learning_order(db, room_id: str, now: datetime | None = None) -> dict | None:
    """Send the latest shadow decision as an order. None when there is nothing fresh to send,
    or when learning is not authorized (its running order is then cancelled)."""
    from app.routes.sensors import override_command
    from app.services.mqtt_ingest import publish_command

    now = now or _utcnow()
    last = _LAST_ACTION.get(room_id)
    if not last or (now - last[0]).total_seconds() > STALE_ACTION_S:
        _FALLBACK[room_id] = "El nodo no reporta: la última orden vence y el nodo vuelve a sus reglas."
        return None                     # node silent: let the running order expire
    auth = await authorization(db, room_id, now)
    if not auth["authorized"]:
        _FALLBACK[room_id] = auth["reason"]
        await cancel_learning_order(db, room_id, now=now)
        return None
    _FALLBACK[room_id] = None
    if await _user_order_running(db, room_id, now):
        return None
    mode, level = order_for(last[1], last[1].get("fan_type", "pwm_12v"))
    command = override_command(mode, ORDER_MINUTES, ORDER_BY, level, authority="predictivo", now=now)
    await publish_command(room_id, command)
    order = {
        "_id": room_id, "mode": mode, "minutes": None if mode == "auto" else ORDER_MINUTES,
        "level": level if mode == "fan" else None,
        "started_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "by": ORDER_BY,
        "command_id": command.get("id"), "authority": "predictivo" if mode != "auto" else None,
    }
    await db["manual_orders"].replace_one({"_id": room_id}, order, upsert=True)
    return order


def fallback_reason(room_id: str) -> str | None:
    return _FALLBACK.get(room_id)


async def set_controller(db, room_id: str, mode: str, by: str) -> dict:
    """Switch the room's controller and act on it right away."""
    if mode not in CONTROLLERS:
        raise ValueError(mode)
    now = _utcnow()
    doc = {"_id": room_id, "mode": mode, "by": by, "at": now.strftime("%Y-%m-%dT%H:%M:%S")}
    await db["room_controller"].replace_one({"_id": room_id}, doc, upsert=True)
    await db["room_controller_log"].insert_one({"room_id": room_id, "mode": mode, "by": by, "at": doc["at"]})
    if mode == "learning":
        await send_learning_order(db, room_id, now)
    else:
        # Hand control back now instead of waiting for the learning order to expire;
        # a person's manual order is left alone.
        _FALLBACK.pop(room_id, None)
        await cancel_learning_order(db, room_id, by, now)
    return {k: doc[k] for k in ("mode", "by", "at")}


async def learning_tick(db) -> int:
    """One pass over the rooms driven by learning. Returns the orders sent."""
    sent = 0
    async for doc in db["room_controller"].find({"mode": "learning"}):
        try:
            if await send_learning_order(db, doc["_id"]):
                sent += 1
        except Exception:     # broker down, etc.: the next tick retries, the node falls back meanwhile
            logger.exception("Learning order failed for room %s", doc["_id"])
    return sent


def start_learning_loop() -> None:
    global _task
    from app.config import MQTT_ENABLED
    from app.database import get_db

    if not MQTT_ENABLED or _task is not None:
        return

    async def _run():
        while True:
            await asyncio.sleep(ORDER_EVERY_S)
            try:
                await learning_tick(get_db())
            except PyMongoError as exc:
                logger.error("Learning loop: MongoDB unavailable: %s", exc)

    _task = asyncio.create_task(_run())


async def stop_learning_loop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
