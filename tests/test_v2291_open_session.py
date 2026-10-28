"""v2.29.1: a Growatt THOR reporting 'suspended_ev' (car full / paused itself)
has an open OCPP transaction. Solar Surplus saw the charger as off (0 W) and
pressed start every minute; the charger rejected it ("a transaction is
already active")."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.util import dt as dt_util

from custom_components.ev_smart_charger.charger_controller import ChargerController
from custom_components.ev_smart_charger.const import (
    CHARGER_BUTTON_STATE_SETTLE_SECONDS,
    CONF_EV_CHARGER_STATUS,
)
from custom_components.ev_smart_charger.power_model import (
    is_ev_paused_status,
    is_session_open_status,
)

from .test_v2140_charger_buttons import BUTTON_CONFIG, START, _presses, _runtime, services  # noqa: F401

STATUS = "sensor.thor_status"
CONFIG = {**BUTTON_CONFIG, CONF_EV_CHARGER_STATUS: STATUS}


def test_status_classifiers():
    for s in ("suspended_ev", "SuspendedEV", "Suspended EV", "charging", "charger_charging", "suspended_evse"):
        assert is_session_open_status(s), s
    for s in ("available", "preparing", "finishing", None, "", "unavailable"):
        assert not is_session_open_status(s), s
    assert is_ev_paused_status("SuspendedEV")
    assert not is_ev_paused_status("suspended_evse")


async def _controller(hass):
    hass.states.async_set("number.thor_current", "6")
    controller = ChargerController(hass, "e", CONFIG, runtime_data=_runtime(0.0))
    await controller.async_setup()
    return controller


async def test_open_session_without_draw_counts_as_on(hass, services):
    hass.states.async_set(STATUS, "suspended_ev")
    controller = await _controller(hass)
    assert await controller.is_charging() is True
    assert controller.button_charger_on() is True

    hass.states.async_set(STATUS, "preparing")
    assert controller.button_charger_on() is False


async def test_stop_press_wins_within_settle(hass, services):
    hass.states.async_set(STATUS, "suspended_ev")
    controller = await _controller(hass)
    controller._record_command(False)
    assert controller.button_charger_on() is False
    controller._last_command_time = dt_util.now() - timedelta(
        seconds=CHARGER_BUTTON_STATE_SETTLE_SECONDS + 1
    )
    assert controller.button_charger_on() is True  # stop did not end it


async def test_already_active_start_is_not_an_error(hass, services):
    hass.states.async_set(STATUS, "preparing")

    async def _reject(call):
        raise RuntimeError(
            "Charging cannot be started because a transaction is already active"
        )

    hass.services.async_register("button", "press", _reject)
    controller = await _controller(hass)
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.start_charger(target_amps=6, reason="test")
    assert result.success is True
    assert controller._commanded_on is True
