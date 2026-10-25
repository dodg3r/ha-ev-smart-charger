"""Unit tests for the main-fuse protection math (fuse_math.py).

Pure Python: no Home Assistant fixtures are needed.
"""
from __future__ import annotations

import pytest

from custom_components.ev_smart_charger.fuse_math import (
    SOURCE_CURRENT,
    SOURCE_CURRENT_WITH_DIRECTION,
    SOURCE_POWER,
    FuseLimit,
    FuseLimiter,
    PhaseReading,
    compute_fuse_limit,
    effective_charger_current,
    highest_level_at_or_below,
    signed_grid_current,
)

GENERIC = list(range(6, 33))
TUYA = [6, 8, 10, 13, 16, 20, 24, 32]


# --------------------------------------------------------------------------
# signed_grid_current
# --------------------------------------------------------------------------


def test_current_with_import_direction_is_positive():
    value, source = signed_grid_current(
        PhaseReading(grid_current_a=12.0, import_w=2700.0, export_w=0.0)
    )
    assert value == 12.0
    assert source == SOURCE_CURRENT_WITH_DIRECTION


def test_current_with_export_direction_is_negative():
    value, source = signed_grid_current(
        PhaseReading(grid_current_a=14.0, import_w=0.0, export_w=3200.0)
    )
    assert value == -14.0
    assert source == SOURCE_CURRENT_WITH_DIRECTION


def test_current_without_direction_counts_as_import():
    """Unknown direction must resolve to the conservative side (import)."""
    value, source = signed_grid_current(PhaseReading(grid_current_a=14.0))
    assert value == 14.0
    assert source == SOURCE_CURRENT


def test_current_with_only_export_power_counts_as_import():
    value, source = signed_grid_current(
        PhaseReading(grid_current_a=9.0, export_w=2000.0)
    )
    assert value == 9.0
    assert source == SOURCE_CURRENT


def test_power_fallback_uses_voltage():
    value, source = signed_grid_current(
        PhaseReading(import_w=2400.0, export_w=100.0, voltage_v=230.0)
    )
    assert value == pytest.approx(10.0)
    assert source == SOURCE_POWER


def test_power_fallback_ignores_implausible_voltage():
    value, _ = signed_grid_current(PhaseReading(import_w=2300.0, voltage_v=0.0))
    assert value == pytest.approx(10.0)


def test_power_fallback_export_is_negative():
    value, _ = signed_grid_current(PhaseReading(import_w=0.0, export_w=2300.0))
    assert value == pytest.approx(-10.0)


def test_missing_everything():
    value, _ = signed_grid_current(PhaseReading())
    assert value is None


# --------------------------------------------------------------------------
# effective_charger_current
# --------------------------------------------------------------------------


def test_charger_off_contributes_nothing():
    assert effective_charger_current(10.0, 16, charger_on=False) == 0.0


def test_charger_uses_lower_of_measured_and_commanded():
    # Stale measurement after a step down: commanded wins.
    assert effective_charger_current(16.0, 10, charger_on=True) == 10.0
    # Car takes less than allowed: measured wins.
    assert effective_charger_current(7.5, 16, charger_on=True) == 7.5


def test_charger_unknown_measurement_is_zero():
    assert effective_charger_current(None, 16, charger_on=True) == 0.0


def test_charger_without_setpoint_uses_measurement():
    assert effective_charger_current(8.0, None, charger_on=True) == 8.0


# --------------------------------------------------------------------------
# compute_fuse_limit
# --------------------------------------------------------------------------


def _phase(grid, imp=None, exp=None, charger=0.0):
    if imp is None and exp is None:
        imp, exp = (1.0, 0.0) if grid >= 0 else (0.0, 1.0)
    return PhaseReading(
        grid_current_a=abs(grid), import_w=imp, export_w=exp, charger_current_a=charger
    )


def test_idle_house_gives_fuse_minus_margin_minus_load():
    result = compute_fuse_limit(
        [_phase(5), _phase(8), _phase(3)], fuse_a=25, margin_a=2
    )
    assert result.valid
    assert result.limit_a == pytest.approx(15.0)  # 25 - 2 - 8
    assert result.bottleneck_phase == 2


def test_charger_current_is_added_back():
    """The meter includes the EV; its own draw must not limit itself."""
    result = compute_fuse_limit(
        [_phase(21, charger=16), _phase(19, charger=16), _phase(20, charger=16)],
        fuse_a=25,
        margin_a=2,
        commanded_a=16,
        charger_on=True,
    )
    # House: 5 / 3 / 4 A  -> room 18 / 20 / 19 -> 18 on L1
    assert result.limit_a == pytest.approx(18.0)
    assert result.bottleneck_phase == 1


def test_stale_high_charger_reading_cannot_hide_house_load():
    """After a step down to 10 A the meter still says 16 A for a while."""
    result = compute_fuse_limit(
        [_phase(24, charger=16)],
        fuse_a=25,
        margin_a=2,
        commanded_a=10,
        charger_on=True,
    )
    # Conservative charger value 10 A -> house 14 A -> room 9 A
    assert result.limit_a == pytest.approx(9.0)


def test_export_gives_extra_room():
    result = compute_fuse_limit(
        [_phase(-14, charger=6)], fuse_a=25, margin_a=2, commanded_a=6, charger_on=True
    )
    # house = -14 - 6 = -20 -> room 25 - 2 + 20 = 43
    assert result.limit_a == pytest.approx(43.0)


def test_unsigned_current_during_export_is_conservative():
    """Same physical situation as above, but no direction information."""
    reading = PhaseReading(grid_current_a=14.0, charger_current_a=6.0)
    result = compute_fuse_limit(
        [reading], fuse_a=25, margin_a=2, commanded_a=6, charger_on=True
    )
    # house estimated 14 - 6 = 8 -> room 15. Lower than the true 43 -> safe.
    assert result.limit_a == pytest.approx(15.0)


@pytest.mark.parametrize(
    ("house", "charger"),
    [(-20.0, 6.0), (-5.0, 10.0), (0.0, 16.0), (3.0, 8.0), (12.0, 6.0)],
)
def test_unsigned_estimate_never_exceeds_true_room(house, charger):
    """Property: without direction the limit is never above the real one."""
    grid = house + charger
    unsigned = compute_fuse_limit(
        [PhaseReading(grid_current_a=abs(grid), charger_current_a=charger)],
        fuse_a=25,
        margin_a=2,
        commanded_a=charger,
        charger_on=True,
    )
    true_room = 25 - 2 - house
    assert unsigned.limit_a <= true_room + 1e-9


def test_overload_clamps_to_zero():
    result = compute_fuse_limit([_phase(30)], fuse_a=25, margin_a=2)
    assert result.valid
    assert result.limit_a == 0.0


def test_missing_phase_invalidates_result():
    result = compute_fuse_limit(
        [_phase(5), PhaseReading(), _phase(5)], fuse_a=25, margin_a=2
    )
    assert not result.valid
    assert result.limit_a is None
    assert "L2" in result.reason


def test_unset_fuse_is_invalid():
    result = compute_fuse_limit([_phase(5)], fuse_a=0, margin_a=2)
    assert not result.valid


def test_negative_margin_treated_as_zero():
    result = compute_fuse_limit([_phase(5)], fuse_a=25, margin_a=-3)
    assert result.limit_a == pytest.approx(20.0)


# --------------------------------------------------------------------------
# highest_level_at_or_below
# --------------------------------------------------------------------------


def test_levels_snap_down():
    assert highest_level_at_or_below(11.9, TUYA) == 10
    assert highest_level_at_or_below(15.99999999, GENERIC) == 16
    assert highest_level_at_or_below(5.9, GENERIC) == 0
    assert highest_level_at_or_below(100, TUYA) == 32


# --------------------------------------------------------------------------
# FuseLimiter
# --------------------------------------------------------------------------


def _limit(amps):
    return FuseLimit(True, amps, 1, [], "test")


INVALID = FuseLimit(False, None, None, [], "no reading for L1")


def test_limiter_first_value_applies_directly():
    limiter = FuseLimiter(GENERIC, max_a=16)
    decision = limiter.update(_limit(30), now=0, charging=False)
    assert decision.allowed_a == 16  # capped by charger max


def test_limiter_decreases_immediately():
    limiter = FuseLimiter(GENERIC, max_a=16)
    limiter.update(_limit(16), now=0, charging=True)
    decision = limiter.update(_limit(9.4), now=1, charging=True)
    assert decision.allowed_a == 9


def test_limiter_ramps_up_two_amps_per_interval():
    limiter = FuseLimiter(GENERIC, max_a=16, ramp_step_a=2, ramp_interval_s=20)
    limiter.update(_limit(6), now=0, charging=True)
    assert limiter.update(_limit(16), now=1, charging=True).allowed_a == 8
    # Within the interval: held.
    assert limiter.update(_limit(16), now=10, charging=True).allowed_a == 8
    assert limiter.update(_limit(16), now=21, charging=True).allowed_a == 10
    assert limiter.update(_limit(16), now=41, charging=True).allowed_a == 12


def test_limiter_ramp_moves_at_least_one_tuya_level():
    """+2 A from 10 A is 12 A, which is not a Tuya level; must still climb."""
    limiter = FuseLimiter(TUYA, max_a=32, ramp_step_a=2, ramp_interval_s=20)
    limiter.update(_limit(10), now=0, charging=True)
    assert limiter.update(_limit(32), now=1, charging=True).allowed_a == 13


def test_limiter_does_not_ramp_when_not_charging():
    limiter = FuseLimiter(GENERIC, max_a=16)
    limiter.update(_limit(6), now=0, charging=False)
    assert limiter.update(_limit(16), now=1, charging=False).allowed_a == 16


def test_limiter_stops_below_minimum_and_holds():
    limiter = FuseLimiter(GENERIC, max_a=16, stop_hold_s=120)
    limiter.update(_limit(16), now=0, charging=True)
    stopped = limiter.update(_limit(4), now=1, charging=True)
    assert stopped.allowed_a == 0
    assert stopped.state == FuseLimiter.STATE_STOPPED
    # Room is back, but the hold keeps the charger off.
    held = limiter.update(_limit(16), now=60, charging=False)
    assert held.allowed_a == 0
    assert held.state == FuseLimiter.STATE_HOLD
    # Hold expired: charger may start again.
    released = limiter.update(_limit(16), now=122, charging=False)
    assert released.allowed_a == 16


def test_limiter_another_overload_during_hold_extends_it():
    limiter = FuseLimiter(GENERIC, max_a=16, stop_hold_s=120)
    limiter.update(_limit(3), now=0, charging=True)
    limiter.update(_limit(3), now=100, charging=False)
    assert limiter.update(_limit(16), now=130, charging=False).allowed_a == 0
    assert limiter.update(_limit(16), now=221, charging=False).allowed_a == 16


def test_limiter_invalid_data_drops_to_minimum_then_stops():
    limiter = FuseLimiter(GENERIC, max_a=16, invalid_stop_s=120)
    limiter.update(_limit(16), now=0, charging=True)
    first = limiter.update(INVALID, now=1, charging=True)
    assert first.allowed_a == 6
    assert first.state == FuseLimiter.STATE_INVALID
    assert limiter.update(INVALID, now=100, charging=True).allowed_a == 6
    final = limiter.update(INVALID, now=122, charging=True)
    assert final.allowed_a == 0
    assert final.state == FuseLimiter.STATE_INVALID_STOP


def test_limiter_invalid_data_never_raises_ceiling():
    limiter = FuseLimiter(GENERIC, max_a=16)
    limiter.update(_limit(4), now=0, charging=True)  # stopped
    assert limiter.update(INVALID, now=1, charging=False).allowed_a == 0


def test_limiter_recovers_after_invalid_period():
    limiter = FuseLimiter(GENERIC, max_a=16, invalid_stop_s=120)
    limiter.update(_limit(16), now=0, charging=True)
    limiter.update(INVALID, now=1, charging=True)
    recovered = limiter.update(_limit(16), now=5, charging=True)
    # Back to valid data, but the climb from 6 A is ramped.
    assert recovered.allowed_a == 8
    # The invalid timer was reset: a new short gap does not stop charging.
    assert limiter.update(INVALID, now=200, charging=True).allowed_a == 6
