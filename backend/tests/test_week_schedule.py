"""Weekly occupancy of every room (Reservas view)."""
from datetime import date, datetime, time
from types import SimpleNamespace as NS

from app.services.week_schedule import build_week, monday_of

MONDAY = date(2026, 11, 2)


def cls(room, day, a, b, people=36):
    return NS(room_id=room, day_of_week=day, start_time=time(a), end_time=time(b), expected_people=people)


def res(room, start, end, people=20, user="profesor"):
    return NS(room_id=room, start_time=start, end_time=end, expected_occupancy=people, username=user)


def test_monday_of_any_day():
    assert monday_of(date(2026, 11, 5)) == MONDAY
    assert monday_of(MONDAY) == MONDAY


def test_classes_and_reservations_in_one_sorted_list():
    blocks = build_week(
        [cls("M601", 1, 16, 18), cls("M802", 1, 9, 11)],
        [res("M802", datetime(2026, 11, 4, 9), datetime(2026, 11, 4, 11))],
        MONDAY, show_users=True,
    )
    assert [(b["room_id"], b["day"], b["start"], b["kind"]) for b in blocks] == [
        ("M802", 1, "09:00", "class"), ("M601", 1, "16:00", "class"), ("M802", 2, "09:00", "reservation"),
    ]
    assert blocks[2]["by"] == "profesor" and blocks[2]["people"] == 20


def test_reservations_outside_the_week_are_ignored():
    blocks = build_week([], [res("A403", datetime(2026, 11, 9, 8), datetime(2026, 11, 9, 10))], MONDAY, show_users=True)
    assert blocks == []


def test_guests_do_not_see_who_reserved():
    blocks = build_week([], [res("A403", datetime(2026, 11, 3, 8), datetime(2026, 11, 3, 10))], MONDAY, show_users=False)
    assert blocks[0]["by"] is None


def test_reservation_past_midnight_is_cut_at_end_of_day():
    blocks = build_week([], [res("A403", datetime(2026, 11, 3, 22), datetime(2026, 11, 4, 1))], MONDAY, show_users=True)
    assert (blocks[0]["day"], blocks[0]["start"], blocks[0]["end"]) == (1, "22:00", "23:59")


# ── Adding a weekly class ──────────────────────────────────────────────────────
from app.services.week_schedule import class_problem  # noqa: E402


def test_class_can_be_added_in_a_free_slot():
    assert class_problem(0, time(8), time(10), 25, 30, [cls("B101", 0, 10, 12)]) is None


def test_class_cannot_overlap_another_one():
    msg = class_problem(0, time(9), time(11), 25, 30, [cls("B101", 0, 10, 12)])
    assert msg == "Se cruza con la clase de 10:00 a 12:00."


def test_class_rejects_bad_times_people_and_sunday():
    assert class_problem(0, time(10), time(9), 25, 30, []) == "La hora de fin debe ser posterior a la de inicio."
    assert class_problem(0, time(8), time(9), 40, 30, []) == "El aula tiene capacidad para 30 personas."
    assert class_problem(6, time(8), time(9), 10, 30, []) == "Las clases van de lunes a sábado."


def test_week_blocks_carry_the_class_id():
    row = cls("B101", 2, 8, 10)
    row.id = 7
    assert build_week([row], [], MONDAY, show_users=False)[0]["id"] == 7


def test_reservation_blocks_carry_their_id():
    r = res("B101", datetime(2026, 11, 4, 9), datetime(2026, 11, 4, 11))
    r.id = 12
    assert build_week([], [r], MONDAY, show_users=False)[0]["id"] == 12
