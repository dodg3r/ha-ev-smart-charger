"""v2.36.1: an unavailable charger status is not a connected car."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ev_smart_charger.const import CONF_EV_CHARGER_STATUS
from custom_components.ev_smart_charger.power_model import is_unavailable_status

from .test_boost_charge import boost  # noqa: F401  (fixture)
from .test_solar_surplus import automation  # noqa: F401  (fixture)


@pytest.mark.parametrize("status", [None, "", "unknown", "unavailable", "Unavailable"])
def test_unavailable_statuses(status):
    assert is_unavailable_status(status) is True


@pytest.mark.parametrize("status", ["charging", "available", "charger_free", "SuspendedEV"])
def test_real_statuses_are_not_unavailable(status):
    assert is_unavailable_status(status) is False


async def test_solar_surplus_skips_when_charger_status_unavailable(hass, automation):  # noqa: F811
    """The log said "Charger status: 'unavailable' - proceeding"."""
    hass.states.async_set("switch.force", "off")
    hass.states.async_set("select.profile", "solar_surplus")
    hass.states.async_set("sensor.charger_status", "unavailable")
    automation.priority_balancer.is_enabled.return_value = True
    automation._has_control = MagicMock(return_value=False)
    automation._update_diagnostic_sensor = AsyncMock()

    await automation._async_periodic_check()

    automation.charger_controller.start_charger.assert_not_called()
    automation.charger_controller.set_amperage.assert_not_called()
    automation.priority_balancer.calculate_priority.assert_not_called()
    states = [c.args[0] for c in automation._update_diagnostic_sensor.await_args_list]
    assert "SKIPPED: Charger offline" in states


async def test_boost_does_not_start_while_charger_offline(hass, boost):  # noqa: F811
    boost.config[CONF_EV_CHARGER_STATUS] = "sensor.charger_status"
    hass.states.async_set("sensor.charger_status", "unavailable")

    await boost._start_boost_charge()

    assert boost.is_active() is False
    boost.charger_controller.start_charger.assert_not_awaited()


async def test_running_boost_survives_a_short_outage(hass, boost):  # noqa: F811
    """Only the start is blocked: an offline status is not an unplug."""
    boost.config[CONF_EV_CHARGER_STATUS] = "sensor.charger_status"
    hass.states.async_set("sensor.charger_status", "unavailable")

    assert boost._car_disconnected() is False
    assert boost._charger_offline() is True
