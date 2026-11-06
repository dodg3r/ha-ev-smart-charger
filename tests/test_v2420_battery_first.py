"""v2.42.0: home battery first up to a SOC; dead-band timer; fuse countdowns."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ev_smart_charger.const import (
    CONF_BATTERY_POWER,
    CONF_CHARGING_POWER,
    CONF_EV_CHARGER_STATUS,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    CONF_HOME_BATTERY_CONTROL,
    CONF_SOC_HOME,
    TOTAL_INTEGRATION_ENTITIES,
    TOTAL_INTEGRATION_ENTITIES_NO_BATTERY,
    get_expected_entity_count,
)
from custom_components.ev_smart_charger.fuse_math import FuseLimiter
from custom_components.ev_smart_charger.solar_surplus import SolarSurplusAutomation


@pytest.fixture
def automation(hass, mock_charger_controller, mock_priority_balancer):
    config = {
        CONF_EV_CHARGER_STATUS: "sensor.charger_status",
        CONF_FV_PRODUCTION: "sensor.solar",
        CONF_CHARGING_POWER: "sensor.ev_power",
        CONF_GRID_IMPORT: "sensor.grid",
        CONF_BATTERY_POWER: "sensor.battery_power",
        CONF_SOC_HOME: "sensor.home_soc",
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
        auto._battery_first_entity = "number.battery_first"
        auto.priority_balancer.is_enabled.return_value = False
        auto.priority_balancer.is_ev_target_reached = AsyncMock(return_value=False)
        auto.priority_balancer.get_ev_current_soc = AsyncMock(return_value=40)
        auto.priority_balancer.get_ev_target_for_today = MagicMock(return_value=80)
        return auto


def _states(hass, *, solar, grid, battery, soc, first):
    hass.states.async_set("switch.force", "off")
    hass.states.async_set("select.profile", "solar_surplus")
    hass.states.async_set("sensor.charger_status", "Preparing")
    hass.states.async_set("sensor.solar", str(solar))
    hass.states.async_set("sensor.ev_power", "0")
    hass.states.async_set("sensor.grid", str(grid))
    hass.states.async_set("sensor.battery_power", str(battery))
    hass.states.async_set("sensor.home_soc", str(soc))
    hass.states.async_set("number.battery_first", str(first))
    hass.states.async_set("number.grid_threshold", "50")
    hass.states.async_set("number.grid_delay", "30")
    hass.states.async_set("number.surplus_delay", "30")
    hass.states.async_set("switch.use_battery", "off")


def test_car_first_counts_battery_charging(hass, automation):
    _states(hass, solar=4600, grid=0, battery=3500, soc=78, first=0)
    info = automation._battery_first_info()
    assert info["battery_reserved_w"] == 0
    assert info["battery_first_active"] is False


def test_battery_first_below_limit_reserves_its_charging(hass, automation):
    _states(hass, solar=4600, grid=0, battery=3500, soc=78, first=90)
    info = automation._battery_first_info()
    assert info["battery_first_active"] is True
    assert info["battery_reserved_w"] == 3500


def test_battery_first_above_limit_releases_the_sun(hass, automation):
    _states(hass, solar=4600, grid=0, battery=3500, soc=92, first=90)
    assert automation._battery_first_info()["battery_reserved_w"] == 0


def test_battery_first_100_without_soc(hass, automation):
    _states(hass, solar=4600, grid=0, battery=3500, soc="unknown", first=100)
    assert automation._battery_first_info()["battery_reserved_w"] == 3500


def test_discharging_battery_is_never_reserved(hass, automation):
    _states(hass, solar=1000, grid=0, battery=-800, soc=50, first=90)
    assert automation._battery_first_info()["battery_reserved_w"] == 0


async def test_battery_first_holds_the_charger(hass, automation):
    """4.6 kW sun, battery takes 3.5 kW: with battery first there is no
    surplus for the car and the diagnostic says why."""
    _states(hass, solar=4600, grid=0, battery=3500, soc=78, first=90)
    automation.charger_controller.is_charging.return_value = False
    automation.charger_controller.get_current_amperage.return_value = 0
    await automation._async_periodic_check(ignore_rate_limit=True)
    automation.charger_controller.start_charger.assert_not_called()
    assert automation._tick_info["surplus_w"] == 0
    assert automation._tick_info["battery_reserved_w"] == 3500


async def test_deadband_timer_survives_idle_ticks(hass, automation):
    """The opportunistic dead-band start never came: the idle branch reset
    the timer every tick."""
    # 5.7 A of surplus on three phases (3933 W) exported, battery idle
    _states(hass, solar=4000, grid=-3933, battery=0, soc=50, first=0)
    automation.charger_controller.is_charging.return_value = False
    automation.charger_controller.get_current_amperage.return_value = 0
    await automation._async_periodic_check(ignore_rate_limit=True)
    first = automation._deadband_start_time
    assert first is not None
    await automation._async_periodic_check(ignore_rate_limit=True)
    assert automation._deadband_start_time == first


def test_entity_count_display_only_battery():
    display = {CONF_SOC_HOME: "sensor.soc", CONF_HOME_BATTERY_CONTROL: False}
    assert get_expected_entity_count(display) == TOTAL_INTEGRATION_ENTITIES_NO_BATTERY + 1
    assert get_expected_entity_count({CONF_SOC_HOME: "sensor.soc"}) == TOTAL_INTEGRATION_ENTITIES


def test_limiter_countdowns():
    limiter = FuseLimiter([6, 8, 10, 13, 16], stop_hold_s=120, increase_delay_s=60)
    assert limiter.hold_remaining(0) == 0
    limiter._hold_until = 150.0
    assert limiter.hold_remaining(100.0) == 50.0
    assert limiter.increase_remaining(100.0) == 0
    limiter._up_since = 90.0
    assert limiter.increase_remaining(100.0) == 50.0
