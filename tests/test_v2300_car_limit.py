"""v2.30.0: the car's own charge limit counts as full, and a button charger
gets no start/stop press the charger would only reject."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.ev_smart_charger.charger_controller import ChargerController
from custom_components.ev_smart_charger.const import (
    CHARGER_BUTTON_STATE_SETTLE_SECONDS,
    CONF_EV_CHARGER_STATUS,
    CONF_EV_SOC_LIMIT,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    CONF_HOME_CONSUMPTION,
    CONF_SOC_CAR,
)
from custom_components.ev_smart_charger.power_model import (
    cap_ev_target,
    is_ev_full,
    is_session_closed_status,
    read_ev_soc_limit,
)
from custom_components.ev_smart_charger.priority_balancer import PriorityBalancer
from custom_components.ev_smart_charger.solar_surplus import SolarSurplusAutomation

from .test_v2140_charger_buttons import (  # noqa: F401
    BUTTON_CONFIG,
    START,
    STOP,
    _presses,
    _runtime,
    services,
)

LIMIT = "sensor.car_charge_limit"
STATUS = "sensor.thor_status"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def test_read_limit(hass):
    assert read_ev_soc_limit(hass, {}) is None
    config = {CONF_EV_SOC_LIMIT: LIMIT}
    assert read_ev_soc_limit(hass, config) is None  # entity missing
    hass.states.async_set(LIMIT, "unavailable")
    assert read_ev_soc_limit(hass, config) is None
    hass.states.async_set(LIMIT, "0")
    assert read_ev_soc_limit(hass, config) is None
    hass.states.async_set(LIMIT, "150")
    assert read_ev_soc_limit(hass, config) is None
    hass.states.async_set(LIMIT, "90")
    assert read_ev_soc_limit(hass, config) == 90.0


def test_cap_and_full():
    assert cap_ev_target(80, None) == 80
    assert cap_ev_target(95, 90.0) == 90
    assert isinstance(cap_ev_target(95, 90.0), int)
    assert cap_ev_target(70.0, 90.0) == 70.0
    assert cap_ev_target(None, 90.0) is None
    assert is_ev_full(90, 90.0)
    assert not is_ev_full(89, 90.0)
    assert not is_ev_full(99, None)
    assert is_ev_full(100, None)
    assert not is_ev_full(None, 90.0)


def test_session_closed_status():
    for s in ("preparing", "Idle", "finishing", "available", "charger_free", "charger_end"):
        assert is_session_closed_status(s), s
    for s in ("charging", "suspended_ev", "SuspendedEVSE", "unavailable", "unknown", None, "weird"):
        assert not is_session_closed_status(s), s


async def test_today_target_capped_to_limit(hass):
    hass.states.async_set(LIMIT, "40")
    balancer = PriorityBalancer(hass, "e", {CONF_SOC_CAR: "sensor.ev", CONF_EV_SOC_LIMIT: LIMIT})
    assert balancer.get_ev_target_for_today() == 40
    hass.states.async_set(LIMIT, "unavailable")
    assert balancer.get_ev_target_for_today() >= 50  # default target again


# --------------------------------------------------------------------------
# Solar Surplus: a full car is not started
# --------------------------------------------------------------------------


@pytest.fixture
def automation(hass, mock_charger_controller, mock_priority_balancer):
    config = {
        CONF_EV_CHARGER_STATUS: "sensor.charger_status",
        CONF_FV_PRODUCTION: "sensor.solar",
        CONF_HOME_CONSUMPTION: "sensor.consumption",
        CONF_GRID_IMPORT: "sensor.grid",
        CONF_EV_SOC_LIMIT: LIMIT,
    }
    with patch("custom_components.ev_smart_charger.solar_surplus.AstralTimeService"):
        auto = SolarSurplusAutomation(
            hass, "test_entry", config, mock_priority_balancer, mock_charger_controller
        )
    auto._update_diagnostic_sensor = AsyncMock()
    return auto


async def test_full_car_is_skipped(hass, automation, mock_priority_balancer):
    hass.states.async_set(LIMIT, "90")
    mock_priority_balancer.get_ev_current_soc = AsyncMock(return_value=90.0)
    assert await automation._car_full_skip(charger_is_on=False) is True
    automation._update_diagnostic_sensor.assert_awaited()
    assert automation._update_diagnostic_sensor.await_args.args[0] == "SKIPPED: Car full"


async def test_below_limit_or_running_is_not_skipped(hass, automation, mock_priority_balancer):
    hass.states.async_set(LIMIT, "90")
    mock_priority_balancer.get_ev_current_soc = AsyncMock(return_value=85.0)
    assert await automation._car_full_skip(charger_is_on=False) is False
    mock_priority_balancer.get_ev_current_soc = AsyncMock(return_value=95.0)
    assert await automation._car_full_skip(charger_is_on=True) is False


async def test_no_limit_sensor_is_legacy(hass, automation, mock_priority_balancer):
    hass.states.async_set(LIMIT, "unavailable")
    mock_priority_balancer.get_ev_current_soc = AsyncMock(return_value=100.0)
    assert await automation._car_full_skip(charger_is_on=False) is False


# --------------------------------------------------------------------------
# controller: no redundant presses
# --------------------------------------------------------------------------

CONFIG = {**BUTTON_CONFIG, CONF_EV_CHARGER_STATUS: STATUS}


async def _controller(hass, watts=0.0):
    hass.states.async_set("number.thor_current", "6")
    controller = ChargerController(hass, "e", CONFIG, runtime_data=_runtime(watts))
    await controller.async_setup()
    controller._last_operation_time = None
    return controller


async def test_no_start_press_with_open_session(hass, services):
    hass.states.async_set(STATUS, "suspended_ev")
    controller = await _controller(hass)
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.start_charger(target_amps=10, reason="test")
    assert result.success is True
    assert START not in _presses(services)
    assert controller._commanded_on is True


async def test_no_stop_press_without_session(hass, services):
    hass.states.async_set(STATUS, "preparing")
    controller = await _controller(hass)
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.stop_charger(reason="test")
    assert result.success is True
    assert STOP not in _presses(services)
    assert controller._commanded_on is False


async def test_stop_pressed_right_after_start(hass, services):
    """Status still reads 'preparing' a few seconds after a start press."""
    hass.states.async_set(STATUS, "preparing")
    controller = await _controller(hass)
    controller._record_command(True)
    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.stop_charger(reason="test")
    assert _presses(services) == [STOP]


async def test_stop_pressed_when_status_unknown_or_drawing(hass, services):
    hass.states.async_set(STATUS, "unavailable")
    controller = await _controller(hass)
    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.stop_charger(reason="test")
    assert _presses(services) == [STOP]

    services.clear()
    hass.states.async_set(STATUS, "preparing")
    controller = await _controller(hass, watts=6900.0)
    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.stop_charger(reason="test")
    assert _presses(services) == [STOP]


async def test_start_pressed_right_after_stop(hass, services):
    """Status can still read 'charging' a moment after a stop press."""
    hass.states.async_set(STATUS, "charging")
    controller = await _controller(hass)
    controller._record_command(False)
    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.start_charger(target_amps=10, reason="test")
    assert START in _presses(services)
    # after the settle window an open session needs no press
    services.clear()
    controller._last_command_time = dt_util.now() - timedelta(
        seconds=CHARGER_BUTTON_STATE_SETTLE_SECONDS + 1
    )
    controller._commanded_on = False
    controller._last_operation_time = None
    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.start_charger(target_amps=10, reason="test")
    assert START not in _presses(services)
