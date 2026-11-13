"""Savings report: monthly fan hours and cost per room against the fan always on.

Figures come from the month-long strategy simulation (see services/profitability).
Each room reports the strategy it runs today, so switching a room to the predictive
controller shows up here. Access: admin only.
"""
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import get_current_user
from app.models.admin import User
from app.services import profitability as pf
from app.services.room_controller import get_controller

router = APIRouter()


@router.get("/roi")
async def get_roi(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")

    metas = await db["strategy_simulation_meta"].find({}, {"timetable": 0}).to_list(length=None)
    rooms = []
    for meta in sorted(metas, key=lambda m: m["_id"]):
        usage = await pf.room_usage(db, meta)
        usage["strategy"] = (await get_controller(db, meta["_id"]))["mode"]
        rooms.append(usage)

    return {
        "tariff": {"pen_per_kwh": pf.TARIFF_PEN_PER_KWH, "label": pf.TARIFF_LABEL},
        "fan_w": pf.FAN_W,
        "building": {"from": pf.BUILDING_FROM, "to": pf.BUILDING_TO, "days": "lunes a sábado"},
        "month_days": pf.MONTH_DAYS,
        "rooms": rooms,
        "simulated": True,
    }
