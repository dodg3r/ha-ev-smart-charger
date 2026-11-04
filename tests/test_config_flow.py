"""Test ev_smart_charger config flow."""
from unittest.mock import patch
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_NAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_smart_charger.const import (
    CONF_MAIN_FUSE_SIZE,
    DOMAIN,
    DEFAULT_BATTERY_CAPACITY,
    CONF_CHARGER_MODEL,
    CONF_EV_CHARGER_SWITCH,
    CONF_EV_CHARGER_CURRENT,
    CONF_EV_CHARGER_STATUS,
    CONF_PHASE_MODE,
    CONF_SOC_CAR,
    CONF_SOC_HOME,
    CONF_FV_PRODUCTION,
    CONF_FV_PRODUCTION_L2,
    CONF_FV_PRODUCTION_L3,
    CONF_HOME_CONSUMPTION,
    CONF_HOME_CONSUMPTION_L2,
    CONF_HOME_CONSUMPTION_L3,
    CONF_GRID_EXPORT,
    CONF_GRID_EXPORT_L2,
    CONF_GRID_EXPORT_L3,
    CONF_GRID_IMPORT,
    CONF_GRID_IMPORT_L2,
    CONF_GRID_IMPORT_L3,
    CONF_PV_FORECAST,
    CONF_NOTIFY_SERVICES,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_POWER,
    CONF_CHARGING_POWER,
    CONF_CHARGING_POWER_L2,
    CONF_FUSE_CHARGER_CURRENT,
    CONF_FUSE_CHARGER_CURRENT_L2,
    CONF_FUSE_CHARGER_CURRENT_L3,
    CONF_FUSE_GRID_EXPORT_L1,
    CONF_FUSE_GRID_EXPORT_L2,
    CONF_FUSE_GRID_EXPORT_L3,
    CONF_FUSE_GRID_IMPORT_L1,
    CONF_FUSE_GRID_IMPORT_L2,
    CONF_FUSE_GRID_IMPORT_L3,
    CONF_PRICE_SENSOR,
    CONF_PV_FORECAST_TOMORROW,
    CONF_CAR_OWNER,
    CONF_ENERGY_FORECAST_TARGET,
    CHARGER_MODEL_GENERIC,
    CHARGER_MODEL_TUYA,
    CONF_HAS_HOME_BATTERY,
    CONF_HAS_SOLAR,
    CONF_LOAD_BALANCING,
    LOAD_BALANCING_NONE,
    PHASE_MODE_THREE,
)

# v2.39.0: every charger field is required (status, charging power, current).
CHARGER_INPUT = {
    CONF_EV_CHARGER_SWITCH: "switch.charger",
    CONF_EV_CHARGER_CURRENT: "number.charger_current",
    CONF_EV_CHARGER_STATUS: "sensor.charger_status",
    CONF_CHARGING_POWER: "sensor.ev_power",
    CONF_FUSE_CHARGER_CURRENT: "sensor.ev_current",
}

# v2.39.0: the sensors step with solar panels (no home battery fields).
SOLAR_SENSORS = {
    CONF_FV_PRODUCTION: "sensor.solar",
    CONF_GRID_IMPORT: "sensor.grid_net",
    CONF_FUSE_GRID_IMPORT_L1: "sensor.import_l1",
    CONF_FUSE_GRID_IMPORT_L2: "sensor.import_l2",
    CONF_FUSE_GRID_IMPORT_L3: "sensor.import_l3",
    CONF_FUSE_GRID_EXPORT_L1: "sensor.export_l1",
    CONF_FUSE_GRID_EXPORT_L2: "sensor.export_l2",
    CONF_FUSE_GRID_EXPORT_L3: "sensor.export_l3",
}

FORECAST_INPUT = {
    CONF_PV_FORECAST: "sensor.forecast",
    CONF_PV_FORECAST_TOMORROW: "sensor.forecast_tomorrow",
}

EXTERNAL_INPUT = {CONF_PRICE_SENSOR: "sensor.nordpool"}


def charger_input(**overrides):
    return {**CHARGER_INPUT, **overrides}


async def _advance_mode_steps(hass: HomeAssistant, flow_id: str):
    """Submit the charger_model step (tuya) and the features step.

    v2.39.0: the phase-mode step is gone; charger_model follows the name step.
    """
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_CHARGER_MODEL: CHARGER_MODEL_TUYA}
    )
    assert result["step_id"] == "features"
    return await _submit_features(hass, result["flow_id"])


async def _submit_features(
    hass: HomeAssistant,
    flow_id: str,
    *,
    solar: bool = True,
    battery: bool = True,
    load_balancing: str = LOAD_BALANCING_NONE,
):
    """v2.17.0: submit the "What do you have?" step."""
    return await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_HAS_SOLAR: solar,
            CONF_HAS_HOME_BATTERY: battery,
            CONF_LOAD_BALANCING: load_balancing,
        },
    )


async def test_form(hass: HomeAssistant):
    """Test we get the form and can walk the full 9-step flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    # Step 1: name → charger_model
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Test Charger"},
    )
    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["step_id"] == "charger_model"

    # charger_model + features → entities
    result3 = await _advance_mode_steps(hass, result2["flow_id"])
    assert result3["type"] == data_entry_flow.FlowResultType.FORM
    assert result3["step_id"] == "entities"

    # Step 4: Charger Entities → sensors
    result4 = await hass.config_entries.flow.async_configure(
        result3["flow_id"], CHARGER_INPUT
    )
    assert result4["type"] == data_entry_flow.FlowResultType.FORM
    assert result4["step_id"] == "sensors"

    # Step 5: Sensors → pv_forecast (v2.36.0: battery power is mapped here)
    result5b = await hass.config_entries.flow.async_configure(
        result4["flow_id"],
        {
            CONF_SOC_HOME: "sensor.home_soc",
            CONF_BATTERY_POWER: "sensor.battery_power",
            **SOLAR_SENSORS,
        },
    )
    assert result5b["type"] == data_entry_flow.FlowResultType.FORM
    assert result5b["step_id"] == "pv_forecast"

    # Step 7: PV Forecast → notifications
    result6 = await hass.config_entries.flow.async_configure(
        result5b["flow_id"], FORECAST_INPUT
    )
    assert result6["type"] == data_entry_flow.FlowResultType.FORM
    assert result6["step_id"] == "notifications"

    # Step 8: Notifications → external_connectors
    with patch(
        "custom_components.ev_smart_charger.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result7 = await hass.config_entries.flow.async_configure(
            result6["flow_id"],
            {
                CONF_NOTIFY_SERVICES: [],
                "car_owner": "person.test",
            },
        )
        assert result7["type"] == data_entry_flow.FlowResultType.FORM
        assert result7["step_id"] == "external_connectors"

        # Step 9: External connectors → dashboard
        result8 = await hass.config_entries.flow.async_configure(
            result7["flow_id"], EXTERNAL_INPUT
        )
        assert result8["type"] == data_entry_flow.FlowResultType.FORM
        # v2.32.0: car profiles at the end of the initial flow too
        assert result8["step_id"] == "cars"
        result8 = await hass.config_entries.flow.async_configure(
            result8["flow_id"], {"car_action": "add"}
        )
        assert result8["step_id"] == "car"
        result8 = await hass.config_entries.flow.async_configure(
            result8["flow_id"],
            {"name": "ABC123", CONF_SOC_CAR: "sensor.car_soc", CONF_BATTERY_CAPACITY: 13.5},
        )
        assert result8["step_id"] == "cars"
        result8 = await hass.config_entries.flow.async_configure(
            result8["flow_id"], {"car_action": "done"}
        )
        assert result8["step_id"] == "dashboard"

        # Step 10: Dashboard → create entry
        result9 = await hass.config_entries.flow.async_configure(
            result8["flow_id"],
            {"create_dashboard": False},
        )

        assert result9["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result9["title"] == "Test Charger"
        assert result9["data"][CONF_NAME] == "Test Charger"
        assert result9["data"][CONF_EV_CHARGER_SWITCH] == "switch.charger"
        assert result9["data"][CONF_PHASE_MODE] == PHASE_MODE_THREE
        assert result9["data"][CONF_CHARGING_POWER] == "sensor.ev_power"
        assert result9["data"][CONF_FUSE_CHARGER_CURRENT] == "sensor.ev_current"
        assert result9["data"][CONF_FUSE_GRID_EXPORT_L3] == "sensor.export_l3"
        assert result9["data"][CONF_PRICE_SENSOR] == "sensor.nordpool"
        assert result9["data"][CONF_CHARGER_MODEL] == CHARGER_MODEL_TUYA
        # v2.36.0: the battery-power sensor round-trips from the sensors step
        assert result9["data"][CONF_BATTERY_POWER] == "sensor.battery_power"
        # v2.32.0: the first car created is the active one
        assert result9["data"]["active_car"] == "ABC123"
        assert result9["data"][CONF_SOC_CAR] == "sensor.car_soc"
        assert result9["data"][CONF_BATTERY_CAPACITY] == 13.5

        await hass.async_block_till_done()
        assert len(mock_setup_entry.mock_calls) == 1


async def test_duplicate_config_entry_aborts_on_entities_step(hass: HomeAssistant):
    """The charger switch is used as unique_id and duplicates are rejected."""
    existing_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Existing Charger",
        data={CONF_EV_CHARGER_SWITCH: "switch.charger"},
        source=config_entries.SOURCE_USER,
        unique_id="switch.charger",
    )
    existing_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Duplicate Charger"},
    )
    result = await _advance_mode_steps(hass, result["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CHARGER_INPUT
    )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_external_connectors_validates_energy_target_entity_exists(hass: HomeAssistant):
    """Energy forecast target must refer to an existing entity."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Test Charger"},
    )
    result = await _advance_mode_steps(hass, result["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], charger_input(**{CONF_EV_CHARGER_SWITCH: "switch.charger_2"})
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOC_HOME: "sensor.home_soc", **SOLAR_SENSORS},
    )
    assert result["step_id"] == "pv_forecast"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FORECAST_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NOTIFY_SERVICES: [],
            CONF_CAR_OWNER: "person.test",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **EXTERNAL_INPUT,
            CONF_ENERGY_FORECAST_TARGET: "number.missing_energy_target",
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"energy_forecast_target": "entity_not_found"}


def _schema_keys(result) -> set[str]:
    """Return the set of field names in a shown form's data schema."""
    return {
        getattr(marker, "schema", marker)
        for marker in result["data_schema"].schema
    }


async def test_solar_sensor_form_and_optional_phases(hass: HomeAssistant):
    """v2.39.0: with solar panels the sensors step asks the net grid sensor and
    import / export per phase; L2/L3 of production, charging power and
    charger current are optional and round-trip when given."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Three Phase"}
    )
    assert result["step_id"] == "charger_model"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGER_MODEL: CHARGER_MODEL_GENERIC}
    )
    result = await _submit_features(hass, result["flow_id"], battery=False)
    assert result["step_id"] == "entities"

    keys = _schema_keys(result)
    for key in (
        CONF_EV_CHARGER_STATUS,
        CONF_CHARGING_POWER,
        CONF_CHARGING_POWER_L2,
        CONF_FUSE_CHARGER_CURRENT,
        CONF_FUSE_CHARGER_CURRENT_L2,
        CONF_FUSE_CHARGER_CURRENT_L3,
    ):
        assert key in keys, f"charger form is missing {key}"
    assert _required(result) >= {
        CONF_EV_CHARGER_CURRENT,
        CONF_EV_CHARGER_STATUS,
        CONF_CHARGING_POWER,
        CONF_FUSE_CHARGER_CURRENT,
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        charger_input(
            **{
                CONF_EV_CHARGER_SWITCH: "switch.charger3",
                CONF_FUSE_CHARGER_CURRENT_L2: "sensor.ev_current_l2",
                CONF_FUSE_CHARGER_CURRENT_L3: "sensor.ev_current_l3",
            }
        ),
    )
    assert result["step_id"] == "sensors"

    keys = _schema_keys(result)
    assert CONF_CHARGING_POWER not in keys
    assert CONF_GRID_IMPORT_L2 not in keys
    assert CONF_GRID_EXPORT not in keys
    assert _required(result) == set(SOLAR_SENSORS)
    assert {CONF_FV_PRODUCTION_L2, CONF_FV_PRODUCTION_L3} <= keys

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**SOLAR_SENSORS, CONF_FV_PRODUCTION_L2: "sensor.pv2", CONF_FV_PRODUCTION_L3: "sensor.pv3"},
    )
    assert result["step_id"] == "pv_forecast"
    assert _required(result) == {CONF_PV_FORECAST, CONF_PV_FORECAST_TOMORROW}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], FORECAST_INPUT
    )
    assert result["step_id"] == "notifications"

    with patch(
        "custom_components.ev_smart_charger.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NOTIFY_SERVICES: [], "car_owner": "person.test"},
        )
        assert result["step_id"] == "external_connectors"
        assert CONF_PRICE_SENSOR in _required(result)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], EXTERNAL_INPUT
        )
        assert result["step_id"] == "cars"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"car_action": "done"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"create_dashboard": False}
        )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_PHASE_MODE] == PHASE_MODE_THREE
    assert data[CONF_CHARGER_MODEL] == CHARGER_MODEL_GENERIC
    assert data[CONF_FV_PRODUCTION_L2] == "sensor.pv2"
    assert data[CONF_FV_PRODUCTION_L3] == "sensor.pv3"
    assert data[CONF_HOME_CONSUMPTION_L2] is None
    assert data[CONF_HOME_CONSUMPTION_L3] is None
    assert data[CONF_GRID_EXPORT] is None
    assert data[CONF_GRID_IMPORT_L2] is None
    assert data[CONF_GRID_IMPORT] == "sensor.grid_net"
    assert data[CONF_FUSE_GRID_IMPORT_L1] == "sensor.import_l1"
    assert data[CONF_FUSE_CHARGER_CURRENT_L2] == "sensor.ev_current_l2"
    assert data[CONF_CHARGING_POWER_L2] is None
    # external load balancing off: the moved meter keys are kept
    assert data[CONF_MAIN_FUSE_SIZE] == 0


def _required(result) -> set[str]:
    """Names of the required fields in a shown form."""
    import voluptuous as vol

    return {
        marker.schema
        for marker in result["data_schema"].schema
        if isinstance(marker, vol.Required)
    }


async def test_charger_status_is_required(hass: HomeAssistant):
    """v2.39.0: the charger status sensor is required again."""
    import voluptuous as vol
    import pytest

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "No Status Charger"}
    )
    result = await _advance_mode_steps(hass, result["flow_id"])
    assert result["step_id"] == "entities"
    without_status = {k: v for k, v in CHARGER_INPUT.items() if k != CONF_EV_CHARGER_STATUS}
    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(result["flow_id"], without_status)


def test_entity_counts_v230():
    """v2.6.0 (issue #42) adds 2 always-created nighttime-offset numbers → 69 / 55."""
    from custom_components.ev_smart_charger.const import (
        TOTAL_INTEGRATION_ENTITIES,
        TOTAL_INTEGRATION_ENTITIES_NO_BATTERY,
    )

    # v2.8.0 adds 1 always-created number (spike response delay) → 71 / 57.
    # v2.10.0 (issue #55) adds 1 always-created switch (stop charging) → 72 / 58.
    # v2.11.0 adds 1 always-created switch (force charge auto-disarm) → 73 / 59.
    # v2.13.0 (issue #57) adds 1 always-created number (off-grid max amperage) → 74 / 60.
    assert TOTAL_INTEGRATION_ENTITIES == 78
    assert TOTAL_INTEGRATION_ENTITIES_NO_BATTERY == 64
