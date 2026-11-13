"""Who drives a node room: rules or the learning controller (orders every 5 min)."""
import asyncio
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from app.services import room_controller as rc  # noqa: E402


class FakeCollection:
    def __init__(self):
        self.docs = {}
        self.inserted = []

    async def find_one(self, q):
        return self.docs.get(q["_id"])

    async def replace_one(self, q, doc, upsert=False):
        self.docs[q["_id"]] = dict(doc)

    async def insert_one(self, doc):
        self.inserted.append(dict(doc))

    def find(self, q):
        items = [d for d in self.docs.values() if all(d.get(k) == v for k, v in q.items())]

        async def gen():
            for d in items:
                yield d
        return gen()


class FakeDB(dict):
    def __missing__(self, name):
        self[name] = FakeCollection()
        return self[name]


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def publish(room_id, payload):
        out.append((room_id, payload))
    monkeypatch.setattr("app.services.mqtt_ingest.publish_command", publish)
    rc._LAST_ACTION.clear()
    return out


def run(coro):
    return asyncio.run(coro)


def test_decision_maps_to_one_actuator():
    assert rc.order_for({"fan_pct": 0, "heater_pct": 100}) == ("heater", 100)
    assert rc.order_for({"fan_pct": 63.4, "heater_pct": 0}) == ("fan", 63)
    assert rc.order_for({"fan_pct": 10, "heater_pct": 0}) == ("auto", 100)   # nothing to anticipate: the rules decide
    assert rc.order_for({}) == ("auto", 100)


def test_rooms_default_to_rules():
    assert run(rc.get_controller(FakeDB(), "A403"))["mode"] == "rules"


def test_switch_to_learning_sends_a_short_order(sent):
    db, now = FakeDB(), datetime.utcnow()
    rc.remember_action("A403", now, {"fan_pct": 75, "heater_pct": 0})
    ctl = run(rc.set_controller(db, "A403", "learning", "admin"))
    assert ctl["mode"] == "learning"
    room, cmd = sent[-1]
    assert room == "A403" and cmd["accion"] == "COOLING" and cmd["nivel"] == 75
    assert cmd["duracion_s"] == rc.ORDER_MINUTES * 60 and cmd["por"] == rc.ORDER_BY
    assert db["room_controller_log"].inserted[-1]["mode"] == "learning"


def test_no_order_when_the_node_is_silent(sent):
    db = FakeDB()
    rc.remember_action("A403", datetime.utcnow() - timedelta(minutes=10), {"fan_pct": 75})
    assert run(rc.send_learning_order(db, "A403")) is None
    assert sent == []          # the last order expires and the node returns to its rules


def test_a_person_order_is_not_overridden(sent):
    db, now = FakeDB(), datetime.utcnow()
    rc.remember_action("A403", now, {"fan_pct": 75})
    db["manual_orders"].docs["A403"] = {"_id": "A403", "mode": "heater", "minutes": 30,
                                        "started_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "by": "admin"}
    assert run(rc.send_learning_order(db, "A403", now)) is None


def test_back_to_rules_ends_the_learning_order(sent):
    db, now = FakeDB(), datetime.utcnow()
    rc.remember_action("A403", now, {"fan_pct": 50})
    run(rc.set_controller(db, "A403", "learning", "admin"))
    run(rc.set_controller(db, "A403", "rules", "admin"))
    assert sent[-1] == ("A403", {"modo": "auto"})


def test_tick_only_drives_learning_rooms(sent):
    db, now = FakeDB(), datetime.utcnow()
    for room in ("A403", "M601"):
        rc.remember_action(room, now, {"fan_pct": 50})
    run(rc.set_controller(db, "A403", "learning", "admin"))
    run(rc.set_controller(db, "M601", "rules", "admin"))
    sent.clear()
    assert run(rc.learning_tick(db)) == 1
    assert [r for r, _ in sent] == ["A403"]
