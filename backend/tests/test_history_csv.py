"""The streamed history CSV writes the same rows as before, one reading at a time."""
import random
from datetime import datetime, timedelta

from app.services.room_history import CSV_HEADER, ClassWalker, InaEnergy, csv_line, ina_energy_series

BASE = datetime(2026, 11, 16)


def test_class_walker_matches_scanning_every_class():
    timetable = [(BASE + timedelta(hours=h), BASE + timedelta(hours=h, minutes=m), 0) for h, m in [(1, 90), (5, 30), (30, 60)]]
    stamps = sorted((BASE + timedelta(seconds=random.Random(1).randint(0, 40 * 3600) + i)).strftime("%Y-%m-%dT%H:%M:%S.%f")
                    for i in range(5000))
    stamps += [s.strftime("%Y-%m-%dT%H:%M:%S.000000") for s, e, _ in timetable] + [e.strftime("%Y-%m-%dT%H:%M:%S.000000") for s, e, _ in timetable]
    stamps.sort()
    walker = ClassWalker(timetable)
    for iso in stamps:
        t = datetime.fromisoformat(iso[:19])
        assert walker.at(iso) == any(s <= t < e for s, e, _ in timetable)


def test_incremental_energy_matches_series():
    docs = [{"sensor_id": "s", "timestamp": (BASE + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.%f"),
             "ina_power_mw": None if i % 17 == 0 else 950.0} for i in range(500)]
    acc = InaEnergy()
    assert [acc.add(d) for d in docs] == ina_energy_series(docs)


def test_csv_line_has_one_value_per_header_column():
    d = {"timestamp": "2026-11-16T10:00:00.123456", "sensor_id": "s-1", "temperature": 24.1, "humidity": None,
         "co2_ppm": 640.0, "co_ppm": None, "node_state": {"accion": "COOLING", "ventilador_pwm": 255, "motivo": 'dijo "hola"'}}
    line = csv_line(d, 0.5, True)
    assert line.startswith("2026-11-16T10:00:00,24.1,,640.0,,COOLING,100,0,1,0,0,\"dijo 'hola'\"")
    assert len(line.split(",")) == len(CSV_HEADER.split(","))
