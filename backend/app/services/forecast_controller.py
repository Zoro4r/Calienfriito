"""Predictive controller: forecasts the room temperature and starts the fan early.

Each closed minute adds that minute's mean temperature to a history. Every
DECIDE_EVERY_MIN minutes a forecaster predicts the next HORIZON_MIN minutes. When the
room is in use (a class now or starting within the horizon, or the node sees people)
and the high forecast (90th percentile) crosses HOT_C anywhere in the horizon, the fan
starts now, at a level that grows with the forecast excess (never below what the rules
would do at the current temperature). Otherwise it asks for nothing and the node's rules
keep acting.

Forecasters:
  · Chronos-Bolt (Amazon, pretrained, zero-shot): used when the `chronos` package is
    installed. It needs no training on this room.
  · Persistence ("the temperature stays as it is"): the fallback and the baseline the
    pretrained model must beat.

Success metric: minutes of class above HOT_C (heat discomfort), rules vs this controller.

Pure Python: no database or framework code here (see `shadow_learning_step` in
data_service for the live wiring).
"""
from __future__ import annotations

import logging
import os
import threading
from statistics import fmean
from typing import Callable, Optional

logger = logging.getLogger(__name__)

HOT_C = 24.5                  # firmware: cooling starts here (setpoint 24 + band 0.5)
HORIZON_MIN = 30              # how far ahead it looks
CONTEXT_MIN = 180             # minutes of history given to the forecaster
DECIDE_EVERY_MIN = 5          # a new forecast and decision every 5 min
READY_MINUTES = 30            # minutes of history before it decides anything
MAX_GAP_MIN = 5               # a longer silence (node offline) restarts the history
CHRONOS_MODEL = os.getenv("CHRONOS_MODEL", "amazon/chronos-bolt-small")

# history (one value per minute, oldest first), horizon → (median, 90th percentile), one per minute ahead
Forecaster = Callable[[list[float], int], tuple[list[float], list[float]]]


def fan_level(excess_c: float) -> int:
    """Fan power for a forecast excess over HOT_C (°C): the hotter it will get, the harder now."""
    if excess_c <= 0:
        return 0
    if excess_c < 0.5:
        return 50
    if excess_c < 1.0:
        return 75
    return 100


def rules_level(temp_c: float) -> int:
    """What the firmware rules run the fan at for this temperature (occupied room)."""
    if temp_c >= 25.5:
        return 100
    if temp_c >= 25.0:
        return 75
    if temp_c >= HOT_C:
        return 50
    return 0


def persistence(history: list[float], horizon: int) -> tuple[list[float], list[float]]:
    """Baseline: the temperature stays where it is."""
    last = history[-1]
    return [last] * horizon, [last] * horizon


persistence.model_name = "persistencia"  # type: ignore[attr-defined]


class ChronosForecaster:
    """Amazon Chronos-Bolt on CPU, loaded on first use."""

    model_name = "chronos-bolt-small"

    def __init__(self, model_id: str = CHRONOS_MODEL) -> None:
        self.model_id = model_id
        self.model_name = model_id.split("/")[-1]
        self._pipe = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:                      # warm_up and the first forecast may race
            if self._pipe is not None:
                return self._pipe
            import torch
            from chronos import BaseChronosPipeline
            torch.set_num_threads(1)          # one core: ingestion keeps the other
            self._pipe = BaseChronosPipeline.from_pretrained(self.model_id, device_map="cpu", torch_dtype=torch.float32)
            return self._pipe

    def __call__(self, history: list[float], horizon: int) -> tuple[list[float], list[float]]:
        import torch
        quantiles, _mean = self._load().predict_quantiles(
            torch.tensor(history, dtype=torch.float32), prediction_length=horizon, quantile_levels=[0.5, 0.9])
        return quantiles[0, :, 0].tolist(), quantiles[0, :, 1].tolist()


_DEFAULT: Optional[Forecaster] = None


def default_forecaster() -> Forecaster:
    """Chronos when installed, otherwise the persistence baseline. One instance for all rooms."""
    global _DEFAULT
    if _DEFAULT is None:
        try:
            import chronos  # noqa: F401
            _DEFAULT = ChronosForecaster()
        except ImportError:
            logger.warning("chronos not installed: the predictive controller uses the persistence baseline")
            _DEFAULT = persistence
    return _DEFAULT


def warm_up() -> None:
    """Load the model in a background thread at startup (~30 s on the VM's CPU), so the
    first forecast does not hold up the ingestion of a reading."""
    f = default_forecaster()
    if isinstance(f, ChronosForecaster):
        threading.Thread(target=f._load, name="chronos-warm-up", daemon=True).start()


class ForecastController:
    def __init__(self, forecaster: Optional[Forecaster] = None) -> None:
        self.forecaster = forecaster or default_forecaster()
        self.history: list[float] = []
        self.bucket: Optional[int] = None          # current minute (epoch minutes)
        self._acc: list[float] = []
        self.last_decision: Optional[int] = None
        self.fan_pct = 0
        self.forecast_c: Optional[float] = None     # median forecast at the horizon
        self.peak_p90_c: Optional[float] = None     # highest 90th percentile in the horizon
        self.closed_minute = False

    @property
    def ready(self) -> bool:
        return len(self.history) >= READY_MINUTES

    def closes_minute(self, ts_s: float) -> bool:
        return self.bucket is not None and int(ts_s // 60) != self.bucket

    def step(self, ts_s: float, temp: Optional[float], in_use: Optional[bool] = None) -> None:
        """One reading. `in_use`: class now or soon, or people seen; only read when a minute closes."""
        minute = int(ts_s // 60)
        self.closed_minute = False
        if self.bucket is None:
            self.bucket = minute
        elif minute != self.bucket:
            if minute - self.bucket > MAX_GAP_MIN:
                self.history = []                   # the node was off: old minutes are not "recent"
            elif self._acc:
                self.history.append(fmean(self._acc))
                self.history = self.history[-CONTEXT_MIN:]
            self.bucket, self._acc, self.closed_minute = minute, [], True
            if self.ready and (self.last_decision is None or minute - self.last_decision >= DECIDE_EVERY_MIN):
                self.decide(minute, bool(in_use))
        if temp is not None:
            self._acc.append(float(temp))

    def decide(self, minute: int, in_use: bool) -> None:
        median, p90 = self.forecaster(self.history, HORIZON_MIN)
        self.forecast_c = round(median[-1], 2)
        self.peak_p90_c = round(max(p90), 2)
        # Anticipate, but never run the fan slower than the rules would right now.
        self.fan_pct = max(fan_level(self.peak_p90_c - HOT_C), rules_level(self.history[-1])) if in_use else 0
        self.last_decision = minute

    def summary(self) -> dict:
        """What the dashboard shows next to each reading (and what room_controller orders)."""
        return {
            "fan_pct": self.fan_pct,
            "heater_pct": 0,
            "forecast_30": self.forecast_c,          # median temperature expected in HORIZON_MIN
            "forecast_30_p90": self.peak_p90_c,
            "minutes_seen": len(self.history),
            "ready": self.ready,
            "model": getattr(self.forecaster, "model_name", "persistencia"),
        }

    def to_state(self) -> dict:
        return {"history": self.history, "bucket": self.bucket, "last_decision": self.last_decision,
                "fan_pct": self.fan_pct, "forecast_c": self.forecast_c, "peak_p90_c": self.peak_p90_c}

    @classmethod
    def from_state(cls, state: dict, forecaster: Optional[Forecaster] = None) -> "ForecastController":
        c = cls(forecaster)
        c.history = list(state.get("history") or [])[-CONTEXT_MIN:]
        c.bucket = state.get("bucket")
        c.last_decision = state.get("last_decision")
        c.fan_pct = state.get("fan_pct") or 0
        c.forecast_c = state.get("forecast_c")
        c.peak_p90_c = state.get("peak_p90_c")
        return c
