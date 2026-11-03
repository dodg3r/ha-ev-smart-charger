"""v2.37.0: the active car follows the car that is home and plugged in."""
from unittest.mock import MagicMock

from custom_components.ev_smart_charger.car_auto_select import CarAutoSelect
from custom_components.ev_smart_charger.const import (
    CONF_ACTIVE_CAR,
    CONF_BATTERY_CAPACITY,
    CONF_CAR_CONNECTION,
    CONF_CAR_LOCATION,
    CONF_CAR_NAME,
    CONF_CARS,
    CONF_EV_SOC_LIMIT,
    CONF_SOC_CAR,
)


def _car(name, prefix):
    return {
        CONF_CAR_NAME: name,
        CONF_SOC_CAR: f"sensor.{prefix}_soc",
        CONF_EV_SOC_LIMIT: None,
        CONF_BATTERY_CAPACITY: 80,
        CONF_CAR_LOCATION: f"device_tracker.{prefix}_location",
        CONF_CAR_CONNECTION: f"sensor.{prefix}_charging_connection_status",
    }


def _make(hass, active="EX40"):
    entry = MagicMock()
    entry.data = {
        CONF_CARS: [_car("EX40", "ex40"), _car("EX90", "ex90")],
        CONF_ACTIVE_CAR: active,
        CONF_SOC_CAR: "sensor.ex40_soc",
    }

    def _update(_entry, data):
        entry.data = data

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update)
    return entry, CarAutoSelect(hass, entry)


def _set(hass, prefix, location, connection):
    hass.states.async_set(f"device_tracker.{prefix}_location", location)
    hass.states.async_set(f"sensor.{prefix}_charging_connection_status", connection)


async def test_car_home_and_connected_becomes_active(hass):
    _set(hass, "ex90", "Jerrys jobb", "disconnected")
    entry, auto = _make(hass)
    await auto.async_setup()

    hass.states.async_set("device_tracker.ex90_location", "home")
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry.assert_not_called()

    hass.states.async_set("sensor.ex90_charging_connection_status", "connected")
    await hass.async_block_till_done()

    assert entry.data[CONF_ACTIVE_CAR] == "EX90"
    assert entry.data[CONF_SOC_CAR] == "sensor.ex90_soc"
    await auto.async_remove()


async def test_connected_away_from_home_does_nothing(hass):
    _set(hass, "ex90", "Jerrys jobb", "disconnected")
    entry, auto = _make(hass)
    await auto.async_setup()

    hass.states.async_set("sensor.ex90_charging_connection_status", "connected")
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry.assert_not_called()
    assert entry.data[CONF_ACTIVE_CAR] == "EX40"
    await auto.async_remove()


async def test_setup_does_not_undo_a_manual_choice(hass):
    """EX90 is already home and plugged in, but the user chose EX40."""
    _set(hass, "ex90", "home", "connected")
    _, auto = _make(hass)
    await auto.async_setup()
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry.assert_not_called()
    await auto.async_remove()


async def test_already_active_car_is_not_reapplied(hass):
    _set(hass, "ex40", "home", "disconnected")
    _, auto = _make(hass)
    await auto.async_setup()

    hass.states.async_set("sensor.ex40_charging_connection_status", "connected")
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry.assert_not_called()
    await auto.async_remove()


async def test_unplug_keeps_the_choice(hass):
    _set(hass, "ex90", "home", "disconnected")
    entry, auto = _make(hass)
    await auto.async_setup()
    hass.states.async_set("sensor.ex90_charging_connection_status", "connected")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.ex90_charging_connection_status", "disconnected")
    await hass.async_block_till_done()

    assert entry.data[CONF_ACTIVE_CAR] == "EX90"
    assert hass.config_entries.async_update_entry.call_count == 1
    await auto.async_remove()


async def test_car_without_entities_is_ignored(hass):
    entry, auto = _make(hass)
    for car in entry.data[CONF_CARS]:
        car[CONF_CAR_LOCATION] = None
        car[CONF_CAR_CONNECTION] = None
    await auto.async_setup()
    assert auto.evaluate() is None
    hass.config_entries.async_update_entry.assert_not_called()
