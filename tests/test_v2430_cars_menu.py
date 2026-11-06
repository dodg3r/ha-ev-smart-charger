"""v2.43.0: the options flow opens with a menu; "Cars" saves the car profiles
without walking through the sensor steps."""
from unittest.mock import patch

from homeassistant import data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_smart_charger.config_flow import EVSCOptionsFlow
from custom_components.ev_smart_charger.const import (
    CONF_ACTIVE_CAR,
    CONF_BATTERY_CAPACITY,
    CONF_CAR_NAME,
    CONF_CARS,
    CONF_EV_CHARGER_SWITCH,
    CONF_GRID_IMPORT,
    CONF_SOC_CAR,
    DOMAIN,
)


def _flow(hass, data):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="switch.charger",
        data={CONF_EV_CHARGER_SWITCH: "switch.charger", **data},
    )
    entry.add_to_hass(hass)
    flow = EVSCOptionsFlow(entry)
    flow.hass = hass
    return flow


async def test_init_is_a_menu(hass):
    flow = _flow(hass, {})
    result = await flow.async_step_init()
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["menu_options"] == ["cars_only", "charger_model"]


async def test_cars_only_adds_a_car_and_saves(hass):
    flow = _flow(hass, {CONF_GRID_IMPORT: "sensor.grid"})
    result = await flow.async_step_cars_only()
    assert result["step_id"] == "cars"
    assert result["description_placeholders"]["step"] == "1"
    assert result["description_placeholders"]["total_steps"] == "1"

    await flow.async_step_cars({"car_action": "add"})
    result = await flow.async_step_car(
        {CONF_CAR_NAME: "EX90", CONF_SOC_CAR: "sensor.ex90_soc", CONF_BATTERY_CAPACITY: 107.0}
    )
    assert result["step_id"] == "cars"

    with patch.object(hass.config_entries, "async_update_entry") as update:
        result = await flow.async_step_cars({"car_action": "done"})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    data = update.call_args.kwargs["data"]
    assert [c[CONF_CAR_NAME] for c in data[CONF_CARS]] == ["EX90"]
    # the first own car becomes active; the other settings are untouched
    assert data[CONF_ACTIVE_CAR] == "EX90"
    assert data[CONF_SOC_CAR] == "sensor.ex90_soc"
    assert data[CONF_GRID_IMPORT] == "sensor.grid"
