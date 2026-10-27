"""v2.25.2: a charger that reads 'off' for one update while it still draws
must not have its own current counted as house load (Dahlbergs: stop + hold
every minute at 10 A).

Fuse 25 A, default margin 2 A -> 23 A per phase.
"""
from .test_v2140_fuse_protection import _guard, _set_charger, _set_phase


async def test_measured_charger_current_counts_as_on(hass):
    _set_phase(hass, 1, 20)  # house 10 A + charger 10 A
    _set_phase(hass, 2, 12)
    _set_phase(hass, 3, 12)
    _set_charger(hass, False, 10, [10, 10, 10])  # on/off reads off, car draws
    guard, controller = await _guard(hass)
    await hass.async_block_till_done()

    # Counted as house the room would be 23 - 20 = 3 A -> stop. The charger
    # draws 10 A, so the house is 10 A and nothing is overloaded.
    controller.async_enforce_fuse_ceiling.assert_not_awaited()
    assert guard.last_decision.state != "stopped"
    await guard.async_remove()


async def test_no_charger_current_still_uses_exact_room(hass):
    _set_phase(hass, 1, 20)
    _set_phase(hass, 2, 12)
    _set_phase(hass, 3, 12)
    _set_charger(hass, False, 10, [0, 0, 0])
    guard, _controller = await _guard(hass)
    await hass.async_block_till_done()

    assert guard.last_decision.state == "stopped"
    await guard.async_remove()
