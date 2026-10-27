"""v2.26.0: fewer commands to slow chargers (Growatt THOR), a status blink is
not an unplug, and less log noise at night."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from custom_components.ev_smart_charger.charger_controller import (
    ChargerController,
    OperationResult,
)
from custom_components.ev_smart_charger.fuse_math import FuseLimit, FuseLimiter

from .test_v2140_charger_buttons import BUTTON_CONFIG, _runtime
from .test_v2200_planned_night_charge import _at, _base, _component

GENERIC = list(range(6, 33))


def _limit(amps, overloaded=True):
    return FuseLimit(True, amps, 1, [], "test", overloaded=overloaded)


# --------------------------------------------------------------------------
# limiter: immediate decrease, one increase after the delay
# --------------------------------------------------------------------------


def test_increase_waits_and_jumps_once():
    limiter = FuseLimiter(GENERIC, max_a=16, increase_delay_s=60)
    limiter.update(_limit(16), now=0, charging=True)
    assert limiter.update(_limit(9), now=1, charging=True).allowed_a == 9
    # Room is back: held for 60 s, then one step to the lowest room seen.
    assert limiter.update(_limit(16, False), now=10, charging=True).allowed_a == 9
    assert limiter.update(_limit(13, False), now=40, charging=True).allowed_a == 9
    assert limiter.update(_limit(16, False), now=69, charging=True).allowed_a == 9
    assert limiter.update(_limit(16, False), now=71, charging=True).allowed_a == 13


def test_a_dip_restarts_the_increase_delay():
    limiter = FuseLimiter(GENERIC, max_a=16, increase_delay_s=60)
    limiter.update(_limit(16), now=0, charging=True)
    limiter.update(_limit(9), now=1, charging=True)
    limiter.update(_limit(16, False), now=10, charging=True)
    limiter.update(_limit(9), now=50, charging=True)  # room gone again
    assert limiter.update(_limit(16, False), now=80, charging=True).allowed_a == 9
    assert limiter.update(_limit(16, False), now=141, charging=True).allowed_a == 16


def test_decrease_is_still_immediate():
    limiter = FuseLimiter(GENERIC, max_a=16, increase_delay_s=60)
    limiter.update(_limit(16), now=0, charging=True)
    assert limiter.update(_limit(11), now=1, charging=True).allowed_a == 11
    assert limiter.update(_limit(8), now=2, charging=True).allowed_a == 8


# --------------------------------------------------------------------------
# controller: slow current confirmation, "not connected" stop
# --------------------------------------------------------------------------


async def test_slow_current_confirmation_is_not_an_error(hass):
    hass.states.async_set("number.thor_current", "16")

    async def _slow(call):
        raise asyncio.TimeoutError

    hass.services.async_register("number", "set_value", _slow)
    controller = ChargerController(hass, "e", BUTTON_CONFIG, runtime_data=_runtime(6900.0))
    await controller.async_setup()
    await controller._set_amperage_internal(10)  # must not raise


async def test_not_connected_stop_counts_as_stopped(hass):
    hass.states.async_set("number.thor_current", "10")

    async def _reject(call):
        raise RuntimeError(
            "The charger is not connected, so the requested action cannot be performed"
        )

    async def _ok(call):
        return None

    hass.services.async_register("number", "set_value", _ok)
    hass.services.async_register("button", "press", _reject)
    controller = ChargerController(hass, "e", BUTTON_CONFIG, runtime_data=_runtime(6900.0))
    await controller.async_setup()
    controller._record_command(True)
    controller._last_operation_time = None
    from unittest.mock import patch

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.stop_charger(reason="test")
    assert result.success is True


# --------------------------------------------------------------------------
# planned night charge: unplug grace, start back-off, fuse without room
# --------------------------------------------------------------------------


async def test_status_blink_does_not_stop_a_session(hass):
    base = _base()
    night, controller = await _component(hass, base)
    await night._evaluate(_at(base, 3, 0))
    assert night.is_active()

    hass.states.async_set("sensor.status", "Available")
    await night._evaluate(_at(base, 3, 0) + timedelta(seconds=20))
    controller.stop_charger.assert_not_awaited()

    hass.states.async_set("sensor.status", "Charging")
    await night._evaluate(_at(base, 3, 1))
    controller.stop_charger.assert_not_awaited()
    assert night.is_active()


async def test_lasting_unplug_stops_the_session(hass):
    base = _base()
    night, controller = await _component(hass, base)
    await night._evaluate(_at(base, 3, 0))
    hass.states.async_set("sensor.status", "Available")
    await night._evaluate(_at(base, 3, 0) + timedelta(seconds=10))
    controller.stop_charger.assert_not_awaited()
    await night._evaluate(_at(base, 3, 0) + timedelta(seconds=75))
    controller.stop_charger.assert_awaited_once()


async def test_failed_start_waits_before_retrying(hass):
    base = _base()
    night, controller = await _component(hass, base)
    controller.start_charger = AsyncMock(
        return_value=OperationResult(success=False, operation="start", reason="t")
    )
    await night._evaluate(_at(base, 3, 0))
    await night._evaluate(_at(base, 3, 0) + timedelta(seconds=1))
    await night._evaluate(_at(base, 3, 0) + timedelta(seconds=30))
    assert controller.start_charger.await_count == 1
    await night._evaluate(_at(base, 3, 1, ) + timedelta(seconds=5))
    assert controller.start_charger.await_count == 2


async def test_no_start_while_the_fuse_has_no_room(hass):
    base = _base()
    night, controller = await _component(hass, base)
    controller.fuse_ceiling = MagicMock(return_value=0)
    await night._evaluate(_at(base, 3, 0))
    controller.start_charger.assert_not_awaited()
    controller.fuse_ceiling.return_value = 10
    await night._evaluate(_at(base, 3, 1))
    controller.start_charger.assert_awaited_once()
