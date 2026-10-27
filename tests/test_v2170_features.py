"""v2.17.0: "What do you have?" setup step.

The answers decide which config steps and fields are shown, and switching an
answer off clears the mappings that belong to it.
"""
from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_NAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_smart_charger.config_flow import (
    EVSCConfigFlow,
    _active_steps,
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
    CONF_FUSE_CHARGER_CURRENT,
    CONF_FUSE_GRID_CURRENT_L1,
    CONF_FUSE_GRID_CURRENT_L2,
    CONF_FUSE_GRID_CURRENT_L3,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    CONF_HAS_HOME_BATTERY,
    CONF_HAS_SOLAR,
    CONF_HOME_CONSUMPTION,
    CONF_HYBRID_INVERTER_MODE,
    CONF_LOAD_BALANCING,
    CONF_MAIN_FUSE_SIZE,
    CONF_NOTIFY_SERVICES,
    CONF_PHASE_MODE,
    CONF_PV_FORECAST,
    CONF_SOC_CAR,
    CONF_SOC_HOME,
    DEFAULT_BATTERY_CAPACITY,
    DOMAIN,
    LOAD_BALANCING_CHARGER,
    LOAD_BALANCING_INTEGRATION,
    LOAD_BALANCING_NONE,
    PHASE_MODE_SINGLE,
    PHASE_MODE_THREE,
    get_load_balancing,
    has_solar,
    wants_home_battery,
)

from .test_config_flow import _schema_keys


async def _to_features(hass, phase=PHASE_MODE_SINGLE):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Test"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PHASE_MODE: phase}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGER_MODEL: CHARGER_MODEL_GENERIC}
    )
    assert result["step_id"] == "features"
    return result


async def _finish(hass, result):
    """Notifications → external connectors → dashboard → entry."""
    hass.services.async_register("notify", "mobile_app_phone", lambda call: None)
    assert result["step_id"] == "notifications"
    with patch(
        "custom_components.ev_smart_charger.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NOTIFY_SERVICES: [], CONF_CAR_OWNER: "person.test"}
        )
        assert result["step_id"] == "external_connectors"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_BATTERY_CAPACITY: 75}
        )
        assert result["step_id"] == "dashboard"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"create_dashboard": False}
        )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    return result["data"]


ENTITIES = {
    CONF_EV_CHARGER_SWITCH: "switch.charger",
    CONF_EV_CHARGER_CURRENT: "number.charger_current",
    CONF_EV_CHARGER_STATUS: "sensor.charger_status",
}


async def test_no_battery_with_load_balancing_flow(hass):
    """HiBerry: solar, no home battery, integration protects the main fuse."""
    result = await _to_features(hass, PHASE_MODE_THREE)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HAS_SOLAR: True,
            CONF_HAS_HOME_BATTERY: False,
            CONF_LOAD_BALANCING: LOAD_BALANCING_INTEGRATION,
        },
    )
    assert result["step_id"] == "entities"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTITIES)

    # v2.18.0: the meter step comes before the sensors step.
    assert result["step_id"] == "main_fuse"
    assert result["description_placeholders"]["total_steps"] == "12"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MAIN_FUSE_SIZE: "20",
            CONF_FUSE_GRID_CURRENT_L1: "sensor.i1",
            CONF_FUSE_GRID_CURRENT_L2: "sensor.i2",
            CONF_FUSE_GRID_CURRENT_L3: "sensor.i3",
            CONF_FUSE_CHARGER_CURRENT: "sensor.charger_current_import",
        },
    )
    assert result["step_id"] == "sensors"

    keys = _schema_keys(result)
    assert CONF_SOC_HOME not in keys
    assert CONF_FV_PRODUCTION in keys

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SOC_CAR: "sensor.car_soc",
            CONF_FV_PRODUCTION: "sensor.pv",
            CONF_HOME_CONSUMPTION: "sensor.house",
            CONF_GRID_IMPORT: "sensor.grid",
        },
    )
    assert result["step_id"] == "hybrid_inverter"
    assert CONF_BATTERY_POWER not in _schema_keys(result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["step_id"] == "pv_forecast"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    data = await _finish(hass, result)
    assert data[CONF_SOC_HOME] is None
    assert data[CONF_BATTERY_POWER] is None
    assert data[CONF_MAIN_FUSE_SIZE] == 20
    assert data[CONF_LOAD_BALANCING] == LOAD_BALANCING_INTEGRATION
    assert data[CONF_HAS_HOME_BATTERY] is False


async def test_no_solar_skips_solar_steps(hass):
    """Without solar panels the hybrid and forecast steps disappear."""
    result = await _to_features(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HAS_SOLAR: False,
            CONF_HAS_HOME_BATTERY: False,
            CONF_LOAD_BALANCING: LOAD_BALANCING_CHARGER,
        },
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTITIES)
    keys = _schema_keys(result)
    assert CONF_FV_PRODUCTION not in keys
    assert CONF_HOME_CONSUMPTION not in keys
    assert CONF_GRID_IMPORT in keys

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOC_CAR: "sensor.car_soc", CONF_GRID_IMPORT: "sensor.grid"},
    )
    # charger handles load balancing → no main_fuse step; no solar → no
    # hybrid / forecast steps.
    data = await _finish(hass, result)
    assert data[CONF_FV_PRODUCTION] is None
    assert data[CONF_MAIN_FUSE_SIZE] == 0
    assert data[CONF_HYBRID_INVERTER_MODE] is False
    assert data[CONF_LOAD_BALANCING] == LOAD_BALANCING_CHARGER


def test_defaults_derived_from_legacy_entry():
    """Entries created before v2.17.0 get answers from what is mapped."""
    legacy = {
        CONF_FV_PRODUCTION: "sensor.pv",
        CONF_SOC_HOME: "sensor.home_soc",
        CONF_MAIN_FUSE_SIZE: 25,
    }
    assert _features_defaults(legacy) == {
        CONF_HAS_SOLAR: True,
        CONF_HAS_HOME_BATTERY: True,
        "home_battery_control": True,
        CONF_LOAD_BALANCING: LOAD_BALANCING_INTEGRATION,
    }
    plain = {CONF_FV_PRODUCTION: "sensor.pv"}
    assert wants_home_battery(plain) is False
    assert get_load_balancing(plain) == LOAD_BALANCING_NONE
    assert has_solar(plain) is True


def test_apply_features_clears_ruled_out_fields():
    data = {
        CONF_HAS_SOLAR: False,
        CONF_HAS_HOME_BATTERY: False,
        CONF_LOAD_BALANCING: LOAD_BALANCING_NONE,
        CONF_SOC_HOME: "sensor.home_soc",
        CONF_BATTERY_POWER: "sensor.batt",
        CONF_FV_PRODUCTION: "sensor.pv",
        CONF_PV_FORECAST: "sensor.forecast",
        CONF_HYBRID_INVERTER_MODE: True,
        CONF_MAIN_FUSE_SIZE: 20,
        CONF_FUSE_GRID_CURRENT_L1: "sensor.i1",
    }
    out = _apply_features(data)
    assert out[CONF_SOC_HOME] is None
    assert out[CONF_BATTERY_POWER] is None
    assert out[CONF_FV_PRODUCTION] is None
    assert out[CONF_PV_FORECAST] is None
    assert out[CONF_HYBRID_INVERTER_MODE] is False
    assert out[CONF_MAIN_FUSE_SIZE] == 0
    assert out[CONF_FUSE_GRID_CURRENT_L1] is None


def test_active_steps_follow_answers():
    full = {
        CONF_HAS_SOLAR: True,
        CONF_HAS_HOME_BATTERY: True,
        CONF_LOAD_BALANCING: LOAD_BALANCING_INTEGRATION,
    }
    assert "fuse" in _active_steps("initial", full)
    assert "name" in _active_steps("initial", full)
    assert "name" not in _active_steps("options", full)
    minimal = {
        CONF_HAS_SOLAR: False,
        CONF_HAS_HOME_BATTERY: False,
        CONF_LOAD_BALANCING: LOAD_BALANCING_NONE,
    }
    steps = _active_steps("initial", minimal)
    assert "fuse" not in steps
    assert "hybrid" not in steps
    assert "forecast" not in steps


async def test_reconfigure_battery_off_clears_battery_sensor(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="switch.charger",
        data={
            CONF_EV_CHARGER_SWITCH: "switch.charger",
            CONF_EV_CHARGER_CURRENT: "number.charger_current",
            CONF_SOC_CAR: "sensor.car_soc",
            CONF_SOC_HOME: "sensor.home_soc",
            CONF_BATTERY_POWER: "sensor.batt",
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
    assert result["step_id"] == "reconfigure_features"
    # The form opens with the answers derived from the existing entry.
    assert result["data_schema"]({})[CONF_HAS_HOME_BATTERY] is True

    await flow.async_step_reconfigure_features(
        {
            CONF_HAS_SOLAR: True,
            CONF_HAS_HOME_BATTERY: False,
            CONF_LOAD_BALANCING: LOAD_BALANCING_NONE,
        }
    )
    result = await flow.async_step_reconfigure_entities(ENTITIES)
    assert CONF_SOC_HOME not in _schema_keys(result)
    result = await flow.async_step_reconfigure_sensors(
        {
            CONF_SOC_CAR: "sensor.car_soc",
            CONF_FV_PRODUCTION: "sensor.pv",
            CONF_HOME_CONSUMPTION: "sensor.house",
            CONF_GRID_IMPORT: "sensor.grid",
        }
    )
    assert result["step_id"] == "reconfigure_hybrid_inverter"
    result = await flow.async_step_reconfigure_hybrid_inverter({})
    assert result["step_id"] == "reconfigure_pv_forecast"
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
    assert data[CONF_SOC_HOME] is None
    assert data[CONF_BATTERY_POWER] is None
    assert data[CONF_HAS_HOME_BATTERY] is False
