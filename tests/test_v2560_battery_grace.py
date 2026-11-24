"""v2.56.0: with a self-balancing home battery a decrease waits 15 s.

The battery's power sensor can lag the meter (FoxESS: 10 s), so for a few
seconds its charging looks like house load. The limiter keeps the ceiling for
``decrease_delay_s`` and only lowers it (or stops) when the lower room is
still there. A hard trip is never delayed.
"""
from __future__ import annotations

from custom_components.ev_smart_charger.const import (
    FUSE_BATTERY_BALANCE_GRACE_SECONDS,
)
from custom_components.ev_smart_charger.fuse_math import FuseLimit, FuseLimiter

LEVELS = list(range(6, 17))
GRACE = 15


def _limit(amps, overloaded=True, hard_trip=False):
    return FuseLimit(
        True, amps, 2, [], "test", overloaded=overloaded, hard_trip=hard_trip
    )


def _limiter(delay=GRACE):
    limiter = FuseLimiter(LEVELS, max_a=16, decrease_delay_s=delay)
    limiter.update(_limit(16), now=0, charging=True)
    return limiter


def test_grace_constant_is_15_seconds():
    assert FUSE_BATTERY_BALANCE_GRACE_SECONDS == 15


def test_stop_waits_and_clears_when_the_battery_is_credited():
    limiter = _limiter()
    # Battery starts: meter shows it as house load, room 0.
    decision = limiter.update(_limit(2), now=100, charging=True)
    assert decision.allowed_a == 16
    assert limiter.decrease_remaining(100) == GRACE
    # Battery sensor catches up 8 s later: room is back.
    decision = limiter.update(_limit(16), now=108, charging=True)
    assert decision.allowed_a == 16
    assert limiter.decrease_remaining(108) == 0
    assert limiter.hold_remaining(108) == 0


def test_stop_applies_after_the_grace():
    limiter = _limiter()
    limiter.update(_limit(2), now=100, charging=True)
    assert limiter.update(_limit(2), now=110, charging=True).allowed_a == 16
    decision = limiter.update(_limit(2), now=115, charging=True)
    assert decision.allowed_a == 0
    assert decision.state == FuseLimiter.STATE_STOPPED


def test_reduction_applies_after_the_grace():
    limiter = _limiter()
    assert limiter.update(_limit(9.5), now=100, charging=True).allowed_a == 16
    assert limiter.update(_limit(10.2), now=115, charging=True).allowed_a == 10


def test_hard_trip_is_never_delayed():
    limiter = _limiter()
    decision = limiter.update(_limit(0, hard_trip=True), now=100, charging=True)
    assert decision.allowed_a == 0
    assert decision.state == FuseLimiter.STATE_HARD_TRIP


def test_grace_restarts_after_the_room_returned():
    limiter = _limiter()
    limiter.update(_limit(2), now=100, charging=True)
    limiter.update(_limit(16), now=105, charging=True)
    # A new drop starts a new wait.
    assert limiter.update(_limit(2), now=110, charging=True).allowed_a == 16
    assert limiter.update(_limit(2), now=124, charging=True).allowed_a == 16
    assert limiter.update(_limit(2), now=125, charging=True).allowed_a == 0


def test_charger_off_waits_too():
    limiter = FuseLimiter(LEVELS, max_a=16, decrease_delay_s=GRACE)
    limiter.update(_limit(16), now=0, charging=False)
    assert limiter.update(_limit(2), now=100, charging=False).allowed_a == 16
    assert limiter.update(_limit(16), now=108, charging=False).allowed_a == 16
    assert limiter.hold_remaining(108) == 0


def test_without_battery_balancing_a_decrease_is_immediate():
    limiter = _limiter(delay=None)
    decision = limiter.update(_limit(2), now=100, charging=True)
    assert decision.allowed_a == 0
    assert limiter.decrease_remaining(100) == 0
