"""Finished days of the "Historial" tab, computed once.

A day before the room's latest data no longer changes, so its summary is stored in
`room_history_days` and served as is. It is recomputed when its inputs change (the
room's sensors, equipment, timetable or simulated scenario) or when the number of raw
readings in the day changes (late or deleted readings): one index count per request.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date

COLL = "room_history_days"


def inputs_key(sensors: list[str], equipment: dict, timetable: list, simulated: bool) -> str:
    raw = json.dumps([sorted(sensors), equipment, [[str(s), str(e), p] for s, e, p in timetable], simulated],
                     sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()


async def get(db, room_id: str, day: date, key: str, raw_n: int) -> dict | None:
    doc = await db[COLL].find_one({"_id": f"{room_id}|{day.isoformat()}"})
    if doc and doc.get("key") == key and doc.get("raw_n") == raw_n:
        return doc["result"]
    return None


async def put(db, room_id: str, day: date, key: str, raw_n: int, result: dict) -> None:
    await db[COLL].replace_one({"_id": f"{room_id}|{day.isoformat()}"},
                               {"key": key, "raw_n": raw_n, "result": result}, upsert=True)
