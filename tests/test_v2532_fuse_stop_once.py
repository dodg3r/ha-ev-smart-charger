"""v2.53.2: one fuse stop per event; a frozen charger current is ignored."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.ev_smart_charger.fuse_guard import FuseGuard

from .test_v2140_fuse_protection import (
    THREE_PHASE_CONFIG,
    _controller,
    _guard,
    _guard_stub,
    _runtime,
    _set_charger,
    _set_phase,
)


async def test_fuse_stop_is_sent_once_while_the_switch_lags(hass):
    """The OCPP switch keeps reading "on" after the stop; no repeats."""
    calls = []

    async def turn_off(call):
        calls.append(call.data["entity_id"])  # state deliberately not updated

    hass.services.async_register("switch", "turn_off", turn_off)
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "6")
    controller = _controller(hass, _runtime(_guard_stub(0)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        for _ in range(10):
            await controller.async_enforce_fuse_ceiling(0, "test")

    assert calls == ["switch.charger"]
    assert controller.is_fuse_paused()


async def test_stop_is_resent_after_the_window(hass):
    calls = []

    async def turn_off(call):
        calls.append(call.data["entity_id"])

    hass.services.async_register("switch", "turn_off", turn_off)
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "6")
    controller = _controller(hass, _runtime(_guard_stub(0)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.async_enforce_fuse_ceiling(0, "test")
        controller._fuse_stop_sent_at -= 31
        await controller.async_enforce_fuse_ceiling(0, "test")

    assert len(calls) == 2


async def _guard_with_power(hass, watts):
    runtime = _runtime()
    runtime.power_model = SimpleNamespace(read_charging_power=lambda _hass: watts)
    controller = SimpleNamespace(async_enforce_fuse_ceiling=AsyncMock())
    guard = FuseGuard(hass, "entry", THREE_PHASE_CONFIG, runtime, controller)
    await guard.async_setup()
    await hass.async_block_till_done()
    return guard, controller


async def test_frozen_charger_current_is_ignored_when_switched_off(hass):
    """Switch off and 0 W: the 5.9 A reading is stale, the house is 10 A."""
    for n in (1, 2, 3):
        _set_phase(hass, n, 10)
    _set_charger(hass, False, 6, [5.9, 5.9, 5.9])
    guard, controller = await _guard_with_power(hass, 0.0)

    assert guard.last_limit.phases[0].charger_current_a == 0
    assert guard.max_allowed_amps() == 13  # 25 - 2 - 10
    controller.async_enforce_fuse_ceiling.assert_not_awaited()
    await guard.async_remove()


async def test_charger_current_counts_while_switched_on(hass):
    for n in (1, 2, 3):
        _set_phase(hass, n, 16)
    _set_charger(hass, True, 6, [6, 6, 6])
    guard, _ = await _guard(hass)
    await hass.async_block_till_done()

    assert guard.last_limit.phases[0].charger_current_a == 6
    await guard.async_remove()


async def test_charger_current_counts_without_a_power_reading(hass):
    """No power sensor: a switch reading 'off' cannot overrule the current."""
    for n in (1, 2, 3):
        _set_phase(hass, n, 16)
    _set_charger(hass, False, 10, [10, 10, 10])
    guard, _ = await _guard(hass)
    await hass.async_block_till_done()

    assert guard.last_limit.phases[0].charger_current_a == 10
    await guard.async_remove()
