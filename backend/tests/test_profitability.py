"""Rentabilidad: fan hours with the system against the fan always on (building day)."""
from datetime import datetime, timedelta

from app.services.profitability import fan_usage, in_building_day

STEP = 1800  # half an hour per row keeps the arithmetic readable
MONDAY_8_LOCAL = datetime(2026, 11, 9, 13, 0)       # 08:00 in Lima (UTC−5)


def row(ts, fan=0.0, occupied=False):
    return {"ts": ts, "fan": fan, "occupied": occupied}


def test_building_day_is_7_to_22_monday_to_saturday():
    assert in_building_day(MONDAY_8_LOCAL)
    assert not in_building_day(datetime(2026, 11, 9, 11, 0))          # 06:00 local
    assert not in_building_day(datetime(2026, 11, 10, 3, 30))         # 22:30 local, Monday
    assert not in_building_day(datetime(2026, 11, 15, 15, 0))         # Sunday


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
