"""v2.19.0: main-fuse guard acts on the meter, settles after a reduction and
has a hard limit above the fuse.

Fuse 25 A, default margin 2 A -> 23 A per phase; hard limit 25 + 10 = 35 A.
"""
from unittest.mock import ANY

from .test_v2140_fuse_protection import _guard, _set_charger, _set_phase


async def test_no_preemptive_reduction_when_meter_is_fine(hass):
    """Car draws 10 A at a 16 A setpoint and the meter is below 23 A."""
    _set_phase(hass, 1, 20)
    _set_phase(hass, 2, 15)
    _set_phase(hass, 3, 15)
    _set_charger(hass, True, 16, [10, 10, 10])
    guard, controller = await _guard(hass)
    await hass.async_block_till_done()

    # Room on L1 is 23 - (20 - 10) = 13 A < 16 A, but nothing is overloaded.
    controller.async_enforce_fuse_ceiling.assert_not_awaited()
    await guard.async_remove()


async def test_settle_window_prevents_second_reduction(hass):
    """Kettle on L1: one reduction, no extra one while the car follows."""
    _set_phase(hass, 1, 26)  # house 10 A + charger 16 A
    _set_phase(hass, 2, 20)
    _set_phase(hass, 3, 20)
    _set_charger(hass, True, 16, [16, 16, 16])
    guard, controller = await _guard(hass)
    await hass.async_block_till_done()
    controller.async_enforce_fuse_ceiling.assert_awaited_once_with(13, ANY)

    # The setpoint entity now reads 13 A, but the car still draws 16 A and
    # the charger's current sensor has not updated yet. Without the settle
    # window the charger would count as 13 A, the house as 13 A, and the
    # charger would be reduced again to 10 A.
    _set_charger(hass, True, 13, [16, 16, 16])
    _set_phase(hass, 1, 26)
    await hass.async_block_till_done()

    controller.async_enforce_fuse_ceiling.assert_awaited_once_with(13, ANY)
    await guard.async_remove()


async def test_more_load_during_settle_still_reduces(hass):
    _set_phase(hass, 1, 26)
    _set_phase(hass, 2, 20)
    _set_phase(hass, 3, 20)
    _set_charger(hass, True, 16, [16, 16, 16])
    guard, controller = await _guard(hass)
    await hass.async_block_till_done()

    # Another 5 A on L1 while settling: house 15 A -> room 8 A.
    _set_phase(hass, 1, 31)
    await hass.async_block_till_done()

    controller.async_enforce_fuse_ceiling.assert_awaited_with(8, ANY)
    await guard.async_remove()


async def test_hard_limit_stops_at_once(hass):
    _set_phase(hass, 1, 26)
    _set_phase(hass, 2, 20)
    _set_phase(hass, 3, 20)
    _set_charger(hass, True, 16, [16, 16, 16])
    guard, controller = await _guard(hass)
    await hass.async_block_till_done()

    # 36 A on L1 is above 25 + 10 A: stop, even during the settle window.
    _set_phase(hass, 1, 36)
    await hass.async_block_till_done()

    controller.async_enforce_fuse_ceiling.assert_awaited_with(0, ANY)
    assert guard.last_decision.state == "hard_trip"
    await guard.async_remove()
