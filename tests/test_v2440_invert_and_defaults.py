"""v2.44.0: invert-sign checkboxes, new install defaults, charged-today sensor."""
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.ev_smart_charger import select as select_platform
from custom_components.ev_smart_charger import switch as switch_platform
from custom_components.ev_smart_charger.config_flow import (
    _battery_schema,
    _meter_schema,
    _normalize_battery_input,
    _normalize_meter_input,
)
from custom_components.ev_smart_charger.const import (
    CONF_BATTERY_POWER,
    CONF_BATTERY_POWER_INVERT,
    CONF_CHARGING_POWER,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    CONF_GRID_INVERT,
    CONF_SOC_HOME,
    DEFAULT_CAR_READY_WEEKDAY,
    DEFAULT_CAR_READY_WEEKEND,
    DEFAULT_EV_MIN_SOC_WEEKDAY,
    DEFAULT_EV_MIN_SOC_WEEKEND,
    PROFILE_SOLAR_SURPLUS,
)
from custom_components.ev_smart_charger.power_model import ChargingModel
from custom_components.ev_smart_charger.runtime import EVSCRuntimeData
from custom_components.ev_smart_charger.sensor import EVSCChargedTodaySensor

BASE = {
    CONF_FV_PRODUCTION: "sensor.solar",
    CONF_GRID_IMPORT: "sensor.grid",
    CONF_BATTERY_POWER: "sensor.battery",
    CONF_CHARGING_POWER: "sensor.ev_power",
}


def _set(hass, solar, grid, battery, ev=0):
    hass.states.async_set("sensor.solar", str(solar))
    hass.states.async_set("sensor.grid", str(grid))
    hass.states.async_set("sensor.battery", str(battery))
    hass.states.async_set("sensor.ev_power", str(ev))


async def test_without_invert_the_signs_are_used_as_is(hass):
    _set(hass, 4000, -500, 1500)
    model = ChargingModel.from_config(BASE)
    assert model.read_grid_import(hass) == -500
    assert model.read_battery_power(hass) == 1500
    assert model.read_battery_discharge(hass) == 0
    # 4000 − 500 − 0 − 1500
    assert model.read_consumption(hass) == 2000


async def test_inverted_grid_and_battery_are_negated(hass):
    # The house exports 500 W and the battery discharges 1500 W, but these
    # sensors report it the other way round.
    _set(hass, 4000, 500, 1500)
    model = ChargingModel.from_config(
        {**BASE, CONF_GRID_INVERT: True, CONF_BATTERY_POWER_INVERT: True}
    )
    assert model.read_grid_import(hass) == -500
    assert model.read_battery_power(hass) == -1500
    assert model.read_battery_discharge(hass) == 1500
    # 4000 − 500 − 0 + 1500
    assert model.read_consumption(hass) == 5000


def test_flow_writes_the_invert_answers():
    assert _normalize_meter_input({CONF_GRID_IMPORT: "sensor.grid", CONF_GRID_INVERT: True})[
        CONF_GRID_INVERT
    ] is True
    assert _normalize_meter_input({CONF_GRID_IMPORT: "sensor.grid"})[CONF_GRID_INVERT] is False
    battery = _normalize_battery_input(
        {CONF_SOC_HOME: "sensor.soc", CONF_BATTERY_POWER: "sensor.b", CONF_BATTERY_POWER_INVERT: True}
    )
    assert battery[CONF_BATTERY_POWER_INVERT] is True


def test_invert_checkbox_comes_right_after_its_sensor():
    meter_keys = [str(k) for k in _meter_schema({}).schema]
    assert meter_keys[meter_keys.index(CONF_GRID_IMPORT) + 1] == CONF_GRID_INVERT
    battery_keys = [str(k) for k in _battery_schema({}).schema]
    assert battery_keys[battery_keys.index(CONF_BATTERY_POWER) + 1] == CONF_BATTERY_POWER_INVERT


def test_new_install_defaults():
    assert DEFAULT_EV_MIN_SOC_WEEKDAY == 80
    assert DEFAULT_EV_MIN_SOC_WEEKEND == 80
    assert DEFAULT_CAR_READY_WEEKDAY is False
    assert DEFAULT_CAR_READY_WEEKEND is False


def _entry(runtime):
    return SimpleNamespace(entry_id="entry_123", data={}, runtime_data=runtime)


async def test_new_install_profile_and_night_switch(hass):
    runtime = EVSCRuntimeData(config={}, expected_entity_count=0)
    entities = []
    with patch(
        "custom_components.ev_smart_charger.select.get_runtime_data", return_value=runtime
    ), patch(
        "custom_components.ev_smart_charger.switch.get_runtime_data", return_value=runtime
    ):
        await select_platform.async_setup_entry(hass, _entry(runtime), entities.extend)
        await switch_platform.async_setup_entry(hass, _entry(runtime), entities.extend)
    profile = next(e for e in entities if e._entity_key == "evsc_charging_profile")
    assert profile.current_option == PROFILE_SOLAR_SURPLUS
    night = next(e for e in entities if e._entity_key == "evsc_night_smart_charge_enabled")
    assert night.is_on is True
    ready = [e for e in entities if e._entity_key.startswith("evsc_car_ready_")]
    assert ready and not any(e.is_on for e in ready)


async def test_charged_today_integrates_the_charging_power(hass):
    runtime = EVSCRuntimeData(config=BASE, expected_entity_count=0)
    runtime.power_model = ChargingModel.from_config(BASE)
    sensor = EVSCChargedTodaySensor(runtime, "entry_123", "evsc_charged_today", "x", "mdi:x")
    sensor.hass = hass
    sensor.entity_id = "sensor.charged_today"
    clock = [1000.0]
    with patch(
        "custom_components.ev_smart_charger.sensor.time.monotonic", side_effect=lambda: clock[0]
    ), patch.object(sensor, "async_write_ha_state"):
        _set(hass, 0, 0, 0, ev=7200)
        sensor._tick()  # first reading: nothing counted yet
        for _ in range(50):  # 7.2 kW for 500 s = 1 kWh
            clock[0] += 10
            sensor._tick()
        assert sensor.native_value == 1.0
        _set(hass, 0, 0, 0, ev=0)
        clock[0] += 10
        sensor._tick()  # the last 10 s still at 7.2 kW
        clock[0] += 3600
        sensor._tick()  # a long gap counts at most 50 s, and the power is 0 W
    assert round(sensor._energy_kwh, 3) == 1.02
