"""v2.40.0: settle after status changes, start blocks, SuspendedEVSE hold."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ev_smart_charger.const import (
    CHARGER_SETTLE_SECONDS,
    CONF_CHARGING_POWER,
    CONF_EV_CHARGER_STATUS,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    SOLAR_RECONNECT_BLOCK_SECONDS,
    SOLAR_RESTART_BLOCK_SECONDS,
)
from custom_components.ev_smart_charger.power_model import (
    is_active_charging_status,
    is_evse_paused_status,
)
from custom_components.ev_smart_charger.solar_surplus import (
    SolarSurplusAutomation,
)

MONO = "custom_components.ev_smart_charger.solar_surplus.time.monotonic"


@pytest.fixture
def automation(hass, mock_charger_controller, mock_priority_balancer):
    config = {
        CONF_EV_CHARGER_STATUS: "sensor.charger_status",
        CONF_FV_PRODUCTION: "sensor.solar",
        CONF_CHARGING_POWER: "sensor.ev_power",
        CONF_GRID_IMPORT: "sensor.grid",
    }
    with patch(
        "custom_components.ev_smart_charger.solar_surplus.AstralTimeService"
    ) as mock_astral:
        mock_astral.return_value.is_nighttime.return_value = False
        auto = SolarSurplusAutomation(
            hass, "test_entry", config, mock_priority_balancer,
            mock_charger_controller,
        )
        auto._charging_profile_entity = "select.profile"
        auto._check_interval_entity = "number.interval"
        auto._grid_import_threshold_entity = "number.grid_threshold"
        auto._grid_import_delay_entity = "number.grid_delay"
        auto._surplus_drop_delay_entity = "number.surplus_delay"
        auto._use_home_battery_entity = "switch.use_battery"
        auto._forza_ricarica_entity = "switch.force"
        auto.priority_balancer.is_enabled.return_value = False
        auto.priority_balancer.is_ev_target_reached = AsyncMock(
            return_value=False
        )
        auto.priority_balancer.get_ev_current_soc = AsyncMock(return_value=40)
        auto.priority_balancer.get_ev_target_for_today = MagicMock(
            return_value=80
        )
        return auto


def _states(hass, status, grid="0", solar="6000"):
    hass.states.async_set("switch.force", "off")
    hass.states.async_set("select.profile", "solar_surplus")
    hass.states.async_set("sensor.charger_status", status)
    hass.states.async_set("sensor.solar", solar)
    hass.states.async_set("sensor.ev_power", "0")
    hass.states.async_set("sensor.grid", grid)
    hass.states.async_set("number.grid_threshold", "50")
    hass.states.async_set("number.grid_delay", "30")
    hass.states.async_set("number.surplus_delay", "30")
    hass.states.async_set("switch.use_battery", "off")


def test_status_classifiers():
    assert is_evse_paused_status("SuspendedEVSE")
    assert is_evse_paused_status("suspended_evse")
    assert not is_evse_paused_status("SuspendedEV")
    assert is_active_charging_status("Charging")
    assert is_active_charging_status("charger_charging")
    assert not is_active_charging_status("SuspendedEVSE")


def test_note_status_windows(automation):
    with patch(MONO, return_value=1000):
        automation._note_status("Preparing", initial=True)
        assert automation._settle_remaining() == 0
        assert automation._start_block_remaining() == 0
        # Start of a charge: settle, no start block
        automation._note_status("Charging")
        assert automation._settle_remaining() == CHARGER_SETTLE_SECONDS
        assert automation._start_block_remaining() == 0
    with patch(MONO, return_value=1100):
        # Charging stops (load balancer pause): settle + restart block
        automation._note_status("SuspendedEVSE")
        assert automation._settle_remaining() == CHARGER_SETTLE_SECONDS
        assert (
            automation._start_block_remaining()
            == SOLAR_RESTART_BLOCK_SECONDS
        )


def test_plug_in_blocks_start(automation):
    with patch(MONO, return_value=2000):
        automation._note_status("Available", initial=True)
        automation._note_status("Preparing")
        assert (
            automation._start_block_remaining()
            == SOLAR_RECONNECT_BLOCK_SECONDS
        )


def test_unavailable_status_is_ignored(automation):
    with patch(MONO, return_value=3000):
        automation._note_status("Charging", initial=True)
        automation._note_status("unavailable")
        automation._note_status("Charging")
        assert automation._settle_remaining() == 0


async def test_settle_skips_the_tick(hass, automation):
    _states(hass, "Charging")
    automation.charger_controller.is_charging.return_value = True
    automation.charger_controller.get_current_amperage.return_value = 16
    with patch(MONO, return_value=4000):
        automation._arm_settle()
        await automation._async_periodic_check(ignore_rate_limit=True)
    automation.charger_controller.set_amperage.assert_not_called()
    automation.charger_controller.stop_charger.assert_not_called()
    automation.charger_controller.start_charger.assert_not_called()


async def test_suspended_evse_holds(hass, automation):
    _states(hass, "SuspendedEVSE", grid="3000", solar="0")
    automation.charger_controller.is_charging.return_value = True
    automation.charger_controller.get_current_amperage.return_value = 16
    await automation._async_periodic_check(ignore_rate_limit=True)
    automation.charger_controller.set_amperage.assert_not_called()
    automation.charger_controller.stop_charger.assert_not_called()


async def test_start_block_prevents_start(hass, automation):
    _states(hass, "Preparing")
    automation.charger_controller.is_charging.return_value = False
    automation.charger_controller.get_current_amperage.return_value = 0
    with patch(MONO, return_value=5000):
        automation._arm_start_block(SOLAR_RESTART_BLOCK_SECONDS)
        with patch.object(
            automation, "_calculate_target_amperage", return_value=6
        ):
            await automation._async_periodic_check(ignore_rate_limit=True)
    automation.charger_controller.start_charger.assert_not_called()


async def test_start_arms_settle(hass, automation):
    _states(hass, "Preparing")
    automation.charger_controller.is_charging.return_value = False
    automation.charger_controller.get_current_amperage.return_value = 0
    with patch(MONO, return_value=6000):
        with patch.object(
            automation, "_calculate_target_amperage", return_value=6
        ):
            await automation._async_periodic_check(ignore_rate_limit=True)
        automation.charger_controller.start_charger.assert_awaited_once()
        assert automation._settle_remaining() == CHARGER_SETTLE_SECONDS


async def test_grid_timer_resets_when_not_owning(hass, automation):
    """Boost / Force / Night own the charger: the timer must not run."""
    automation._last_grid_import_high = 123.0
    hass.states.async_set("switch.force", "on")
    await automation._async_periodic_check(ignore_rate_limit=True)
    assert automation._last_grid_import_high is None


async def test_grid_protection_stops_when_no_surplus_for_lowest_level(
    hass, automation
):
    automation._last_grid_import_high = 100.0
    with patch(MONO, return_value=200.0):
        await automation._handle_grid_import_protection(
            grid_import=900, grid_threshold=50, grid_import_delay=30,
            current_amps=16, target_amps=0,
        )
    automation.charger_controller.stop_charger.assert_awaited_once()
    automation.charger_controller.set_amperage.assert_not_called()


async def test_grid_protection_steps_down_when_surplus_remains(
    hass, automation
):
    automation._last_grid_import_high = 100.0
    with patch(MONO, return_value=200.0):
        await automation._handle_grid_import_protection(
            grid_import=900, grid_threshold=50, grid_import_delay=30,
            current_amps=16, target_amps=10,
        )
    automation.charger_controller.set_amperage.assert_awaited_once()
    automation.charger_controller.stop_charger.assert_not_called()


async def test_immediate_check_arms_windows(hass, automation):
    with patch.object(
        automation, "_async_periodic_check", AsyncMock()
    ), patch(MONO, return_value=7000):
        await automation.async_request_immediate_check()
        assert automation._settle_remaining() == CHARGER_SETTLE_SECONDS
        assert (
            automation._start_block_remaining()
            == SOLAR_RESTART_BLOCK_SECONDS
        )


def test_pending_reports_settle_and_start_block(automation):
    automation._tick_current_amps = 0
    with patch(MONO, return_value=8000):
        automation._arm_settle()
        automation._arm_start_block(SOLAR_RESTART_BLOCK_SECONDS)
        pending = automation._pending_action()
    assert pending["pending_action"] == "settle"
