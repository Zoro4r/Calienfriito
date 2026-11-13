"""Who drives a node room: the node's own rules or the learning controller.

With the rules, the node decides alone and the learning controller runs in shadow.
With learning, the server turns the shadow decision into a short manual order every
ORDER_EVERY_S. Orders last ORDER_MINUTES, so if the server or the link goes down the
order expires and the node falls back to its rules by itself. The node keeps CO alarm
priority over any order.
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
_task: asyncio.Task | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def remember_action(room_id: str, at: datetime, action: dict) -> None:
    """Called by the shadow step with each new decision."""
    _LAST_ACTION[room_id] = (at.replace(tzinfo=None), action)


def order_for(action: dict) -> tuple[str, int]:
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
        return "fan", int(min(100, round(fan)))
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


async def send_learning_order(db, room_id: str, now: datetime | None = None) -> dict | None:
    """Send the latest shadow decision as an order. None when there is nothing fresh to send."""
    from app.routes.sensors import override_command
    from app.services.mqtt_ingest import publish_command

    now = now or _utcnow()
    last = _LAST_ACTION.get(room_id)
    if not last or (now - last[0]).total_seconds() > STALE_ACTION_S:
        return None                     # node silent: let the running order expire
    if await _user_order_running(db, room_id, now):
        return None
    mode, level = order_for(last[1])
    command = override_command(mode, ORDER_MINUTES, ORDER_BY, level)
    await publish_command(room_id, command)
    order = {
        "_id": room_id, "mode": mode, "minutes": None if mode == "auto" else ORDER_MINUTES,
        "level": level if mode == "fan" else None,
        "started_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "by": ORDER_BY,
    }
    await db["manual_orders"].replace_one({"_id": room_id}, order, upsert=True)
    return order


async def set_controller(db, room_id: str, mode: str, by: str) -> dict:
    """Switch the room's controller and act on it right away."""
    from app.services.mqtt_ingest import publish_command

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
        order = await db["manual_orders"].find_one({"_id": room_id})
        if order and order.get("by") == ORDER_BY:
            await publish_command(room_id, {"modo": "auto"})
            await db["manual_orders"].replace_one(
                {"_id": room_id},
                {"_id": room_id, "mode": "auto", "minutes": None, "level": None, "started_at": doc["at"], "by": by},
                upsert=True,
            )
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
