"""Rentabilidad: fan hours with the system against the fan always on (building day)."""
from datetime import datetime, timedelta

from datetime import time

from app.services.profitability import fan_usage, in_building_day, minute_usage, occupancy

STEP = 1800  # half an hour per row keeps the arithmetic readable
MONDAY_8_LOCAL = datetime(2026, 11, 9, 13, 0)       # 08:00 in Lima (UTC−5)


def row(ts, fan=0.0, occupied=False):
    return {"ts": ts, "fan": fan, "occupied": occupied}


def test_always_on_means_every_hour_of_every_day():
    assert in_building_day(MONDAY_8_LOCAL)
    assert in_building_day(datetime(2026, 11, 9, 8, 0))               # 03:00 local
    assert in_building_day(datetime(2026, 11, 15, 15, 0))             # Sunday


def test_fan_usage_counts_on_hours_and_cube_energy():
    rows = [row(MONDAY_8_LOCAL + timedelta(seconds=STEP * k), fan=f, occupied=f > 0)
            for k, f in enumerate([0.0, 0.5, 1.0, 0.0])]
    u = fan_usage(rows, STEP)
    assert u["on_hours"] == 1.0                      # two half hours on
    assert abs(u["eq_hours"] - (0.125 + 1.0) * 0.5) < 1e-9
    assert u["reference_hours"] == 2.0               # always on: the four rows are in the building day
    assert u["class_hours"] == 1.0
    assert u["ref_by_hour"][8] == 1.0 and u["ref_by_hour"][9] == 1.0     # 08:00–10:00 local
    assert abs(u["eq_by_hour"][8] - 0.0625) < 1e-9 and u["eq_by_hour"][9] == 0.5


def test_empty_simulation_gives_zero():
    u = fan_usage([], STEP)
    assert (u["on_hours"], u["eq_hours"], u["reference_hours"], u["class_hours"]) == (0.0, 0.0, 0.0, 0.0)


def test_real_usage_applies_the_heuristic_only_during_classes():
    # Monday 08:00–08:03 local: a class from 08:00 to 08:02 at 25.2 °C, then no class.
    occupied = occupancy([(0, time(8, 0), time(8, 2))], [])
    minutes = [{"ts": MONDAY_8_LOCAL + timedelta(minutes=k), "temp": 25.2, "lfan": None} for k in range(4)]
    u = minute_usage(minutes, occupied)
    assert abs(u["reference_hours"] - 4 / 60) < 1e-9          # always on: every minute of the day
    assert abs(u["class_hours"] - 2 / 60) < 1e-9
    assert abs(u["rules_hours"] - 2 / 60) < 1e-9              # 25.2 °C → 75 %
    assert abs(u["rules_eq_hours"] - 2 / 60 * 0.75 ** 3) < 1e-9


def test_one_day_classes_count_as_occupied():
    occupied = occupancy([], [(datetime(2026, 11, 13, 15, 0), datetime(2026, 11, 13, 18, 30))])
    assert occupied(datetime(2026, 11, 13, 16, 0)) and not occupied(datetime(2026, 11, 13, 19, 0))
