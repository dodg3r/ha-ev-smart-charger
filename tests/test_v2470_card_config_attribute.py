"""v2.47.0: the active-car select publishes the card config, so a dashboard
card on any view can fill in the sensors it does not list itself."""
from types import SimpleNamespace

from custom_components.ev_smart_charger.const import (
    CONF_BATTERY_POWER,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    CONF_GRID_INVERT,
    CONF_SOC_HOME,
)
from custom_components.ev_smart_charger.dashboard_manager import (
    build_card_sensor_config,
)
from custom_components.ev_smart_charger.select import EVSCCarSelect

DATA = {
    CONF_FV_PRODUCTION: "sensor.solar",
    CONF_GRID_IMPORT: "sensor.grid_net",
    CONF_GRID_INVERT: True,
    CONF_SOC_HOME: "sensor.home_soc",
    CONF_BATTERY_POWER: "sensor.batt_power",
}


def _entry():
    return SimpleNamespace(entry_id="ABC", data=dict(DATA))


def test_sensor_config_has_no_card_only_keys():
    config = build_card_sensor_config(_entry())
    for key in ("type", "title", "entity_prefix", "_build_version"):
        assert key not in config
    assert config["solar_power_entity"] == "sensor.solar"
    assert config["home_battery_soc_entity"] == "sensor.home_soc"
    assert config["battery_power_entity"] == "sensor.batt_power"
    assert config["grid_invert"] is True


def test_car_select_publishes_card_config(hass):
    entity = EVSCCarSelect(hass, _entry(), None)
    attrs = entity.extra_state_attributes
    assert attrs["card_config"] == build_card_sensor_config(_entry())
    assert "card_config" in EVSCCarSelect._unrecorded_attributes
    assert "cars" in attrs
