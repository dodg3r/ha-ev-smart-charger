"""v2.34.3: a fuse pause is normal load balancing, not a warning."""
import logging

from custom_components.ev_smart_charger.fuse_guard import FuseGuard
from custom_components.ev_smart_charger.fuse_math import (
    FuseLimit,
    FuseLimiter,
    LimiterDecision,
)
from custom_components.ev_smart_charger.utils.logging_helper import EVSCLogger


def _guard(hass, last_state):
    guard = object.__new__(FuseGuard)
    guard.hass = hass
    guard.logger = EVSCLogger("MAIN FUSE GUARD")
    guard._last_state = last_state
    guard._invalid_unwarned = False
    return guard


def _log(hass, caplog, previous, state, charger_on):
    guard = _guard(hass, previous)
    decision = LimiterDecision(allowed_a=0, target_a=0, state=state, reason="")
    limit = FuseLimit(valid=True, limit_a=0, bottleneck_phase=1, reason="bottleneck L1")
    with caplog.at_level(logging.DEBUG):
        guard._log_transition(decision, limit, charger_on)
    return [r for r in caplog.records if "Fuse protection" in r.message]


async def test_stop_while_charging_is_info(hass, caplog):
    records = _log(hass, caplog, FuseLimiter.STATE_LIMITED, FuseLimiter.STATE_STOPPED, True)
    assert records and all(r.levelno == logging.INFO for r in records)


async def test_stop_while_paused_is_debug(hass, caplog):
    records = _log(hass, caplog, FuseLimiter.STATE_HOLD, FuseLimiter.STATE_STOPPED, False)
    assert records and all(r.levelno == logging.DEBUG for r in records)


async def test_hard_trip_still_warns(hass, caplog):
    records = _log(hass, caplog, FuseLimiter.STATE_LIMITED, FuseLimiter.STATE_HARD_TRIP, True)
    assert any(r.levelno == logging.WARNING for r in records)
