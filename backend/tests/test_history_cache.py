"""Finished history days are recomputed whenever what they depend on changes."""
from datetime import datetime

from app.services.history_cache import inputs_key

EQ = {"label": "Actuadores del nodo", "fan_max_w": 2.4, "heater_w": 10.0}
CLASS = [(datetime(2026, 11, 16, 12), datetime(2026, 11, 16, 14), 30)]


def test_same_inputs_same_key():
    assert inputs_key(["b", "a"], EQ, CLASS, False) == inputs_key(["a", "b"], dict(EQ), list(CLASS), False)


def test_any_input_change_changes_key():
    base = inputs_key(["a"], EQ, CLASS, False)
    assert inputs_key(["a", "b"], EQ, CLASS, False) != base
    assert inputs_key(["a"], {**EQ, "fan_max_w": 5.0}, CLASS, False) != base
    assert inputs_key(["a"], EQ, [], False) != base
    assert inputs_key(["a"], EQ, CLASS, True) != base
