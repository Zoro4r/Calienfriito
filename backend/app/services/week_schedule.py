"""Weekly occupancy of every room: recurring classes plus that week's reservations.

All times are naive LOCAL wall-clock, as stored. Days count from Monday (0) to Sunday (6).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _hm(t: time) -> str:
    return t.strftime("%H:%M")


def build_week(schedules: Iterable, reservations: Iterable, monday: date, show_users: bool) -> list[dict]:
    """One block per class or reservation in the week starting on `monday`.

    `schedules` rows have room_id, day_of_week, start_time, end_time, expected_people;
    `reservations` rows have room_id, start_time, end_time (datetimes), expected_occupancy
    and an optional `username`. Reservations outside the week are ignored; one that
    crosses midnight is cut at the end of its first day.
    """
    blocks = [{
        "id": getattr(s, "id", None), "room_id": s.room_id, "day": s.day_of_week,
        "start": _hm(s.start_time), "end": _hm(s.end_time),
        "kind": "class", "people": s.expected_people, "by": None,
    } for s in schedules]
    week_end = datetime.combine(monday + timedelta(days=7), time.min)
    week_start = datetime.combine(monday, time.min)
    for r in reservations:
        if not (week_start <= r.start_time < week_end):
            continue
        end = r.end_time if r.end_time.date() == r.start_time.date() else datetime.combine(r.start_time.date(), time(23, 59))
        blocks.append({
            "id": getattr(r, "id", None), "room_id": r.room_id, "day": (r.start_time.date() - monday).days,
            "start": _hm(r.start_time.time()), "end": _hm(end.time()),
            "kind": "reservation", "people": r.expected_occupancy,
            "by": getattr(r, "username", None) if show_users else None,
        })
    blocks.sort(key=lambda b: (b["day"], b["start"], b["room_id"]))
    return blocks


def class_problem(day: int, start: time, end: time, people: int, capacity: int | None, existing: Iterable) -> str | None:
    """Why a new weekly class cannot be added, in user language, or None if it can.

    `existing` are the room's current classes (day_of_week, start_time, end_time).
    Classes run Monday (0) to Saturday (5); two classes of a room cannot overlap.
    """
    if not 0 <= day <= 5:
        return "Las clases van de lunes a sábado."
    if end <= start:
        return "La hora de fin debe ser posterior a la de inicio."
    if people < 1:
        return "Indica cuántas personas se esperan."
    if capacity and people > capacity:
        return f"El aula tiene capacidad para {capacity} personas."
    for c in existing:
        if c.day_of_week == day and c.start_time < end and start < c.end_time:
            return f"Se cruza con la clase de {_hm(c.start_time)} a {_hm(c.end_time)}."
    return None
