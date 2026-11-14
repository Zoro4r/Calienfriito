"""
Occupancy service — separates PLANNED occupancy from REALIZED occupancy.

The system distinguishes two occupancy signals that must never be conflated:

  - expected_occupancy  → PLANNED headcount, known ahead of time from the room's
    Schedule / Reservation. Drives FEED-FORWARD (anticipatory) pre-cooling: it is
    the only signal available *before* people arrive.

  - actual_occupancy    → REALIZED headcount, preferably from a visual counter and
    otherwise inferred in real time from the CO2 sensor. Drives FEEDBACK
    (closed-loop) correction: if a room booked for 40 has only 5 people, the AC
    backs off, avoiding wasted energy.

Actual occupancy is estimated with a simple steady-state CO2 mass-balance proxy:

    people ≈ (co2_measured − co2_baseline) / co2_ppm_per_person

Both constants are calibratable. Defaults assume a naturally/lightly ventilated
classroom; recalibrate against a couple of manual headcounts for accuracy.
"""

from typing import Literal

# Outdoor / empty-room CO2 floor in ppm (no occupants present).
CO2_BASELINE_PPM = 420.0

# Steady-state CO2 rise in ppm attributable to one additional occupant.
# Higher ventilation → lower value. Calibrate per room type.
CO2_PPM_PER_PERSON = 25.0


def fuse_occupancy(
    co2_ppm: float | None,
    visual_count: int | None,
    max_capacity: int | None = None,
    mode: Literal["co2", "visual", "blend"] = "visual",
    visual_weight: float = 0.7,
) -> int | None:
    """Return the occupancy signal used by the predictor.

    A visual count is a direct observation, while CO2 is a delayed proxy. In
    ``visual`` mode it replaces the proxy; ``blend`` is intended for calibration.
    Missing visual data always falls back to the CO2 estimate.
    """
    co2_count = estimate_actual_occupancy(co2_ppm, max_capacity)
    if visual_count is None:
        return co2_count
    visual = max(0, int(visual_count))
    if max_capacity is not None:
        visual = min(visual, int(max_capacity))
    if mode == "co2":
        return co2_count
    if mode == "blend" and co2_count is not None:
        weight = min(1.0, max(0.0, float(visual_weight)))
        return round((1.0 - weight) * co2_count + weight * visual)
    return visual


def estimate_actual_occupancy(
    co2_ppm: float | None, max_capacity: int | None = None
) -> int | None:
    """Infer realized headcount from a CO2 reading (steady-state mass balance).

    Args:
        co2_ppm: Measured CO2 concentration in ppm, or None when the sensor has
                 no CO2 channel — in which case actual occupancy is unknowable.
        max_capacity: Room capacity used to clamp physically impossible estimates.

    Returns:
        Estimated occupant count (>= 0), clamped to max_capacity when provided,
        or None when co2_ppm is None (no signal → caller falls back to expected).
    """
    if co2_ppm is None:
        return None

    estimate = (co2_ppm - CO2_BASELINE_PPM) / CO2_PPM_PER_PERSON
    estimate = max(0.0, estimate)
    if max_capacity is not None:
        estimate = min(estimate, float(max_capacity))
    return round(estimate)


def occupancy_gap(expected: int | None, actual: int | None) -> int | None:
    """Signed gap between planned and realized occupancy (expected − actual).

    Positive → over-reservation / no-shows (room emptier than booked).
    Negative → overcrowding (more people than reserved).
    None when either signal is missing.
    """
    if expected is None or actual is None:
        return None
    return expected - actual
