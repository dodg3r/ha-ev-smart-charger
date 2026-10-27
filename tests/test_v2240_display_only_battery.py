"""v2.24.0: a home battery that is shown on the dashboard only.

A battery with its own control (e.g. an AI optimiser) is mapped for the
dashboard, but the charging logic runs as if there were no battery.
"""
from types import SimpleNamespace
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_smart_charger.config_flow import (
    EVSCConfigFlow,
    _apply_features,
    _features_defaults,
)
from custom_components.ev_smart_charger.const import (
    CHARGER_MODEL_GENERIC,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_POWER,
    CONF_CAR_OWNER,
    CONF_CHARGER_MODEL,
    CONF_EV_CHARGER_CURRENT,
    CONF_EV_CHARGER_STATUS,
    CONF_EV_CHARGER_SWITCH,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    CONF_HAS_HOME_BATTERY,
    CONF_HAS_SOLAR,
    CONF_HOME_BATTERY_CONTROL,
    CONF_HOME_CONSUMPTION,
    CONF_LOAD_BALANCING,
    CONF_NOTIFY_SERVICES,
    CONF_PHASE_MODE,
    CONF_SOC_CAR,
    CONF_SOC_HOME,
    DEFAULT_BATTERY_CAPACITY,
    DOMAIN,
    LOAD_BALANCING_NONE,
    PHASE_MODE_SINGLE,
    TOTAL_INTEGRATION_ENTITIES,
    TOTAL_INTEGRATION_ENTITIES_NO_BATTERY,
    controls_home_battery,
    get_expected_entity_count,
    has_home_battery,
    shows_home_battery,
)
from custom_components.ev_smart_charger.dashboard_manager import _build_card_config
from custom_components.ev_smart_charger.priority_balancer import PriorityBalancer

DISPLAY_ONLY = {
    CONF_SOC_CAR: "sensor.car_soc",
    CONF_SOC_HOME: "sensor.home_soc",
    CONF_HAS_HOME_BATTERY: True,
    CONF_HOME_BATTERY_CONTROL: False,
}


def test_display_only_battery_is_shown_but_not_used():
    assert shows_home_battery(DISPLAY_ONLY) is True
    assert has_home_battery(DISPLAY_ONLY) is False
    assert controls_home_battery(DISPLAY_ONLY) is False
    assert get_expected_entity_count(DISPLAY_ONLY) == TOTAL_INTEGRATION_ENTITIES_NO_BATTERY


def test_legacy_entry_keeps_controlling_the_battery():
    legacy = {CONF_SOC_HOME: "sensor.home_soc"}
    assert has_home_battery(legacy) is True
    assert controls_home_battery(legacy) is True
    assert _features_defaults(legacy)[CONF_HOME_BATTERY_CONTROL] is True
    assert get_expected_entity_count(legacy) == TOTAL_INTEGRATION_ENTITIES


def test_apply_features_keeps_display_only_sensors():
    data = _apply_features({**DISPLAY_ONLY, CONF_BATTERY_POWER: "sensor.batt"})
    assert data[CONF_SOC_HOME] == "sensor.home_soc"
    assert data[CONF_BATTERY_POWER] == "sensor.batt"


async def test_priority_balancer_ignores_display_only_battery(hass):
    hass.states.async_set("sensor.car_soc", "60")
    hass.states.async_set("sensor.home_soc", "10")
    balancer = PriorityBalancer(hass, "test_entry", dict(DISPLAY_ONLY))
    assert await balancer.get_home_current_soc() == 100.0
    assert balancer.get_home_target_for_today() == 0
    assert await balancer.is_home_target_reached() is True


def test_card_config_shows_battery_and_hides_priority():
    entry = SimpleNamespace(entry_id="ABC", data=dict(DISPLAY_ONLY))
    config = _build_card_config(entry)
    assert config["home_battery_soc_entity"] == "sensor.home_soc"
    assert config["home_battery_controlled"] is False

    entry = SimpleNamespace(entry_id="ABC", data={CONF_SOC_HOME: "sensor.home_soc"})
    assert _build_card_config(entry)["home_battery_controlled"] is True


async def test_reconfigure_to_display_only_keeps_the_soc_sensor(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="switch.charger",
        data={
            CONF_EV_CHARGER_SWITCH: "switch.charger",
            CONF_EV_CHARGER_CURRENT: "number.charger_current",
            CONF_SOC_CAR: "sensor.car_soc",
            CONF_SOC_HOME: "sensor.home_soc",
            CONF_FV_PRODUCTION: "sensor.pv",
            CONF_HOME_CONSUMPTION: "sensor.house",
            CONF_GRID_IMPORT: "sensor.grid",
            CONF_NOTIFY_SERVICES: [],
            CONF_CAR_OWNER: "person.owner",
            CONF_BATTERY_CAPACITY: DEFAULT_BATTERY_CAPACITY,
        },
    )
    entry.add_to_hass(hass)
    flow = EVSCConfigFlow()
    flow.hass = hass
    flow.context = {"entry_id": entry.entry_id}

    await flow.async_step_reconfigure({CONF_PHASE_MODE: PHASE_MODE_SINGLE})
    result = await flow.async_step_reconfigure_charger_model(
        {CONF_CHARGER_MODEL: CHARGER_MODEL_GENERIC}
    )
    assert result["data_schema"]({})[CONF_HOME_BATTERY_CONTROL] is True
    await flow.async_step_reconfigure_features(
        {
            CONF_HAS_SOLAR: True,
            CONF_HAS_HOME_BATTERY: True,
            CONF_HOME_BATTERY_CONTROL: False,
            CONF_LOAD_BALANCING: LOAD_BALANCING_NONE,
        }
    )
    await flow.async_step_reconfigure_entities(
        {
            CONF_EV_CHARGER_SWITCH: "switch.charger",
            CONF_EV_CHARGER_CURRENT: "number.charger_current",
            CONF_EV_CHARGER_STATUS: "sensor.charger_status",
        }
    )
    await flow.async_step_reconfigure_sensors(
        {
            CONF_SOC_CAR: "sensor.car_soc",
            CONF_SOC_HOME: "sensor.home_soc",
            CONF_FV_PRODUCTION: "sensor.pv",
            CONF_HOME_CONSUMPTION: "sensor.house",
            CONF_GRID_IMPORT: "sensor.grid",
        }
    )
    await flow.async_step_reconfigure_hybrid_inverter({})
    await flow.async_step_reconfigure_pv_forecast({})
    hass.services.async_register("notify", "mobile_app_phone", lambda call: None)
    await flow.async_step_reconfigure_notifications(
        {CONF_NOTIFY_SERVICES: [], CONF_CAR_OWNER: "person.owner"}
    )
    await flow.async_step_reconfigure_external_connectors(
        {CONF_BATTERY_CAPACITY: DEFAULT_BATTERY_CAPACITY}
    )
    with patch.object(hass.config_entries, "async_update_entry") as update_entry:
        await flow.async_step_reconfigure_dashboard({"create_dashboard": False})

    data = update_entry.call_args.kwargs["data"]
    assert data[CONF_SOC_HOME] == "sensor.home_soc"
    assert data[CONF_HOME_BATTERY_CONTROL] is False
    assert has_home_battery(data) is False
