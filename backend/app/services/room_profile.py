"""
Room profile — static physical metadata per room (Tier 3).

Replaces the opaque `room_code` integer with the physical attributes that actually
drive a room's cooling demand: floor (roof/solar exposure), interior volume (thermal
mass) and A/C nominal capacity. Exposing these as model features lets the regressor
learn each room's thermal behavior directly, instead of memorizing an arbitrary code
(which in Tier 2 carried near-zero permutation importance).

`thermal_factor` is the multiplier applied to the cooling-demand label; the metadata
above is engineered to correlate with it, so a model given the metadata can recover
the factor. Seed generator, extract_data and predictive_service import these
fallback profiles; a registered room's dimensions override the estimated volume
at inference.

Values are plausible estimates for UTEC Barranco (no on-site survey); adjust when
real floor plans / A/C nameplates are available.
"""

# room_id → physical metadata.
#   floor      : building level (higher → more roof/solar gain)
#   volume_m3  : interior air volume (thermal mass)
#   ac_btu     : A/C nominal cooling capacity
#   orientation: 0=N, 1=E, 2=S, 3=W (minor solar-facing effect)
_PROFILE: dict[str, dict] = {
    "A403":  {"floor": 4,  "volume_m3": 190, "ac_btu": 36000, "orientation": 1},
    "M601":  {"floor": 6,  "volume_m3": 150, "ac_btu": 30000, "orientation": 2},
    "M604":  {"floor": 6,  "volume_m3": 150, "ac_btu": 30000, "orientation": 0},
    "M602":  {"floor": 6,  "volume_m3": 140, "ac_btu": 24000, "orientation": 3},
    "M802":  {"floor": 8,  "volume_m3": 150, "ac_btu": 30000, "orientation": 1},
    "M1001": {"floor": 10, "volume_m3": 190, "ac_btu": 36000, "orientation": 2},
}

# room_id → thermal-mass multiplier on cooling demand (higher floor/volume → more).
_THERMAL_FACTOR: dict[str, float] = {
    "A403": 1.00,
    "M601": 0.95,
    "M604": 0.95,
    "M602": 0.90,
    "M802": 1.10,
    "M1001": 1.20,
}

# Neutral defaults for an unknown room (keeps inference safe, no crash).
_DEFAULT_META = {"floor": 5, "volume_m3": 150, "ac_btu": 30000, "orientation": 0}
_DEFAULT_FACTOR = 1.0

# Physical metadata columns exposed as model features (stable order).
METADATA_FEATURES = ["floor", "volume_m3", "ac_btu"]


def get_metadata(room_id) -> dict:
    """Return the physical metadata dict for a room (neutral default if unknown)."""
    return _PROFILE.get(str(room_id), _DEFAULT_META)


def thermal_factor(room_id) -> float:
    """Cooling-demand multiplier for a room (1.0 default if unknown)."""
    return _THERMAL_FACTOR.get(str(room_id), _DEFAULT_FACTOR)
