"""Unit tests for the CO2 mass-balance occupancy estimator (pure logic, no I/O)."""
import pytest

from app.services.occupancy_service import (
    CO2_BASELINE_PPM,
    CO2_PPM_PER_PERSON,
    estimate_actual_occupancy,
    occupancy_gap,
    fuse_occupancy,
)


class TestEstimateActualOccupancy:
    def test_none_co2_returns_none(self):
        # No CO2 channel → occupancy is unknowable, caller falls back to expected.
        assert estimate_actual_occupancy(None) is None

    def test_baseline_co2_means_empty_room(self):
        assert estimate_actual_occupancy(CO2_BASELINE_PPM) == 0

    def test_below_baseline_clamps_to_zero(self):
        # Outdoor-fresh air below the baseline must never yield negative people.
        assert estimate_actual_occupancy(CO2_BASELINE_PPM - 100) == 0

    def test_mass_balance_formula(self):
        # 10 people worth of CO2 above baseline.
        co2 = CO2_BASELINE_PPM + 10 * CO2_PPM_PER_PERSON
        assert estimate_actual_occupancy(co2) == 10

    def test_rounds_to_nearest_person(self):
        co2 = CO2_BASELINE_PPM + 2.6 * CO2_PPM_PER_PERSON
        assert estimate_actual_occupancy(co2) == 3

    def test_clamped_to_max_capacity(self):
        co2 = CO2_BASELINE_PPM + 500 * CO2_PPM_PER_PERSON
        assert estimate_actual_occupancy(co2, max_capacity=40) == 40

    def test_capacity_not_applied_when_none(self):
        co2 = CO2_BASELINE_PPM + 500 * CO2_PPM_PER_PERSON
        assert estimate_actual_occupancy(co2, max_capacity=None) == 500


class TestOccupancyGap:
    def test_none_when_either_signal_missing(self):
        assert occupancy_gap(None, 5) is None
        assert occupancy_gap(5, None) is None
        assert occupancy_gap(None, None) is None

    def test_positive_gap_means_no_shows(self):
        # Booked for 40, only 5 showed up.
        assert occupancy_gap(40, 5) == 35

    def test_negative_gap_means_overcrowding(self):
        assert occupancy_gap(10, 15) == -5

    def test_zero_gap(self):
        assert occupancy_gap(12, 12) == 0


class TestFuseOccupancy:
    def test_visual_replaces_co2_proxy(self):
        co2 = CO2_BASELINE_PPM + 30 * CO2_PPM_PER_PERSON
        assert fuse_occupancy(co2, 8, max_capacity=40, mode="visual") == 8

    def test_visual_falls_back_to_co2_when_missing(self):
        co2 = CO2_BASELINE_PPM + 6 * CO2_PPM_PER_PERSON
        assert fuse_occupancy(co2, None, max_capacity=40, mode="visual") == 6

    def test_blend_is_calibratable(self):
        co2 = CO2_BASELINE_PPM + 10 * CO2_PPM_PER_PERSON
        assert fuse_occupancy(co2, 4, mode="blend", visual_weight=0.5) == 7
