"""v2.18.0: grid import, charging power and home consumption derived in the
integration from the main-meter step, so mapping them is optional.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.ev_smart_charger.config_flow import (
    EVSCConfigFlow,
    _sensor_schema,
)
from custom_components.ev_smart_charger.const import (
    CHARGER_STATUS_FREE,
    CONF_BATTERY_POWER,
    CONF_EV_CHARGER_STATUS,
    CONF_FUSE_CHARGER_CURRENT,
    CONF_FUSE_CHARGER_CURRENT_L1,
    CONF_FUSE_CHARGER_CURRENT_L2,
    CONF_FUSE_CHARGER_CURRENT_L3,
    CONF_FUSE_GRID_EXPORT_L1,
    CONF_FUSE_GRID_EXPORT_L2,
    CONF_FUSE_GRID_EXPORT_L3,
    CONF_FUSE_GRID_IMPORT_L1,
    CONF_FUSE_GRID_IMPORT_L2,
    CONF_FUSE_GRID_IMPORT_L3,
    CONF_FUSE_GRID_VOLTAGE_L1,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    CONF_HAS_HOME_BATTERY,
    CONF_HAS_SOLAR,
    CONF_HOME_CONSUMPTION,
    CONF_LOAD_BALANCING,
    CONF_MAIN_FUSE_SIZE,
    CONF_PHASE_MODE,
    LOAD_BALANCING_INTEGRATION,
    LOAD_BALANCING_NONE,
    PHASE_MODE_THREE,
    has_meter_import_power,
)
from custom_components.ev_smart_charger.power_model import ChargingModel


class _FakeStates:
    def __init__(self):
        self._states = {}

    def set(self, entity_id, state, **attributes):
        self._states[entity_id] = SimpleNamespace(
            entity_id=entity_id, state=str(state), attributes=attributes
        )

    def get(self, entity_id):
        return self._states.get(entity_id)


def _hass():
    return SimpleNamespace(states=_FakeStates())


THREE_METER = {
    CONF_PHASE_MODE: PHASE_MODE_THREE,
    CONF_LOAD_BALANCING: LOAD_BALANCING_INTEGRATION,
    CONF_MAIN_FUSE_SIZE: 20,
    CONF_FV_PRODUCTION: "sensor.pv",
    CONF_FUSE_GRID_IMPORT_L1: "sensor.imp1",
    CONF_FUSE_GRID_IMPORT_L2: "sensor.imp2",
    CONF_FUSE_GRID_IMPORT_L3: "sensor.imp3",
    CONF_FUSE_GRID_EXPORT_L1: "sensor.exp1",
    CONF_FUSE_GRID_EXPORT_L2: "sensor.exp2",
    CONF_FUSE_GRID_EXPORT_L3: "sensor.exp3",
    CONF_FUSE_CHARGER_CURRENT: "sensor.charger_current",
    CONF_EV_CHARGER_STATUS: "sensor.status",
}


def _meter_states(hass, imports=(500, 0, 0), exports=(0, 800, 700)):
    for idx, value in enumerate(imports, start=1):
        hass.states.set(f"sensor.imp{idx}", value, unit_of_measurement="W")
    for idx, value in enumerate(exports, start=1):
        hass.states.set(f"sensor.exp{idx}", value, unit_of_measurement="W")


# ---------------------------------------------------------------------------
# const helper
# ---------------------------------------------------------------------------


def test_meter_import_needs_every_phase_and_integration_balancing():
    assert has_meter_import_power(THREE_METER) is True
    partial = dict(THREE_METER, **{CONF_FUSE_GRID_IMPORT_L3: None})
    assert has_meter_import_power(partial) is False
    other = dict(THREE_METER, **{CONF_LOAD_BALANCING: LOAD_BALANCING_NONE})
    assert has_meter_import_power(other) is False


# ---------------------------------------------------------------------------
# grid import
# ---------------------------------------------------------------------------


def test_grid_import_is_import_minus_export():
    hass = _hass()
    _meter_states(hass)
    model = ChargingModel.from_config(THREE_METER)
    assert model.grid_import_derived is True
    assert model.read_grid_import(hass) == pytest.approx(500 - 800 - 700)
    assert model.validate_grid_import(hass) == (True, None)
    assert set(model.grid_import_source_entities()) == {
        "sensor.imp1", "sensor.imp2", "sensor.imp3",
        "sensor.exp1", "sensor.exp2", "sensor.exp3",
    }


def test_grid_import_normalises_kw():
    hass = _hass()
    _meter_states(hass, imports=(1.2, 0.3, 0.5), exports=(0, 0, 0))
    for idx in (1, 2, 3):
        state = hass.states.get(f"sensor.imp{idx}")
        state.attributes["unit_of_measurement"] = "kW"
    model = ChargingModel.from_config(THREE_METER)
    assert model.read_grid_import(hass) == pytest.approx(2000)


def test_grid_import_unavailable_phase_is_invalid():
    hass = _hass()
    _meter_states(hass)
    hass.states.set("sensor.imp2", "unavailable")
    model = ChargingModel.from_config(THREE_METER)
    ok, err = model.validate_grid_import(hass)
    assert ok is False and "meter" in err
    assert model.read_grid_import(hass) == 0.0


def test_mapped_grid_import_still_wins():
    hass = _hass()
    _meter_states(hass)
    hass.states.set("sensor.grid", 123)
    model = ChargingModel.from_config(dict(THREE_METER, **{CONF_GRID_IMPORT: "sensor.grid"}))
    assert model.grid_import_derived is False
    assert model.read_grid_import(hass) == 123
    assert model.grid_import_source_entities() == ["sensor.grid"]


def test_validation_lists_meter_sensors_when_derived():
    model = ChargingModel.from_config(THREE_METER)
    entities = [entity for entity, _ in model.labelled_power_entities()]
    assert "sensor.imp1" in entities and "sensor.exp3" in entities
    assert "sensor.pv" in entities


# ---------------------------------------------------------------------------
# charging power
# ---------------------------------------------------------------------------


def test_charging_power_from_phase_attributes():
    hass = _hass()
    hass.states.set("sensor.charger_current", 16, L1=16, L2=15.5, L3=16.2)
    model = ChargingModel.from_config(THREE_METER)
    assert model.charging_power_derived is True
    assert model.read_charging_power(hass) == pytest.approx((16 + 15.5 + 16.2) * 230)
    assert model.is_charging(hass) is True


def test_charging_power_uses_meter_voltage():
    hass = _hass()
    config = {
        CONF_LOAD_BALANCING: LOAD_BALANCING_INTEGRATION,
        CONF_FUSE_GRID_IMPORT_L1: "sensor.imp1",
        CONF_FUSE_GRID_VOLTAGE_L1: "sensor.v1",
        CONF_FUSE_CHARGER_CURRENT: "sensor.charger_current",
    }
    hass.states.set("sensor.charger_current", 10)
    hass.states.set("sensor.v1", 240)
    model = ChargingModel.from_config(config)
    assert model.read_charging_power(hass) == pytest.approx(2400)


def test_charging_power_from_dedicated_phase_sensors():
    hass = _hass()
    config = dict(THREE_METER)
    config.pop(CONF_FUSE_CHARGER_CURRENT)
    config.update(
        {
            CONF_FUSE_CHARGER_CURRENT_L1: "sensor.c1",
            CONF_FUSE_CHARGER_CURRENT_L2: "sensor.c2",
            CONF_FUSE_CHARGER_CURRENT_L3: "sensor.c3",
        }
    )
    for idx in (1, 2, 3):
        hass.states.set(f"sensor.c{idx}", 6)
    model = ChargingModel.from_config(config)
    assert model.read_charging_power(hass) == pytest.approx(18 * 230)


def test_zero_charger_current_reads_zero_power():
    hass = _hass()
    hass.states.set("sensor.charger_current", 0, L1=7, L2=7, L3=7)
    model = ChargingModel.from_config(THREE_METER)
    assert model.read_charging_power(hass) == 0.0
    assert model.is_charging(hass) is False


# ---------------------------------------------------------------------------
# home consumption
# ---------------------------------------------------------------------------


def test_consumption_is_production_plus_grid_minus_ev():
    hass = _hass()
    _meter_states(hass, imports=(0, 0, 0), exports=(300, 300, 400))  # −1000 W
    hass.states.set("sensor.pv", 6000)
    hass.states.set("sensor.charger_current", 6, L1=6, L2=6, L3=6)  # 4140 W
    model = ChargingModel.from_config(THREE_METER)
    assert model.consumption_derived is True
    assert model.read_consumption(hass) == pytest.approx(6000 - 1000 - 4140)
    # Surplus = production − consumption = EV draw + export.
    surplus = model.read_production(hass) - model.read_consumption(hass)
    assert surplus == pytest.approx(4140 + 1000)


def test_consumption_subtracts_battery_charging():
    hass = _hass()
    _meter_states(hass, imports=(100, 100, 100), exports=(0, 0, 0))
    hass.states.set("sensor.pv", 3000)
    hass.states.set("sensor.charger_current", 0)
    hass.states.set("sensor.batt", 1500)  # charging
    model = ChargingModel.from_config(dict(THREE_METER, **{CONF_BATTERY_POWER: "sensor.batt"}))
    assert model.read_consumption(hass) == pytest.approx(3000 + 300 - 1500)


def test_consumption_with_charger_current_unavailable_and_idle_status():
    hass = _hass()
    _meter_states(hass, imports=(200, 200, 200), exports=(0, 0, 0))
    hass.states.set("sensor.pv", 0)
    hass.states.set("sensor.charger_current", "unavailable")
    hass.states.set("sensor.status", CHARGER_STATUS_FREE)
    model = ChargingModel.from_config(THREE_METER)
    assert model.derived_power_errors(hass) == []
    assert model.read_consumption(hass) == pytest.approx(600)


def test_consumption_unknown_when_car_may_be_charging():
    hass = _hass()
    _meter_states(hass)
    hass.states.set("sensor.pv", 0)
    hass.states.set("sensor.charger_current", "unavailable")
    hass.states.set("sensor.status", "Charging")
    model = ChargingModel.from_config(THREE_METER)
    assert model.derived_power_errors(hass)


def test_mapped_consumption_still_wins():
    hass = _hass()
    hass.states.set("sensor.house", 777)
    model = ChargingModel.from_config(
        dict(THREE_METER, **{CONF_HOME_CONSUMPTION: "sensor.house"})
    )
    assert model.consumption_derived is False
    assert model.read_consumption(hass) == 777
    assert model.derived_power_errors(hass) == []


def test_nothing_derived_without_meter_step():
    model = ChargingModel.from_config({CONF_FV_PRODUCTION: "sensor.pv"})
    assert model.grid_import_derived is False
    assert model.charging_power_derived is False
    assert model.consumption_derived is False


# ---------------------------------------------------------------------------
# config flow
# ---------------------------------------------------------------------------


def _required(schema):
    return {str(key) for key in schema.schema if key.__class__.__name__ == "Required"}


def test_sensor_schema_optional_when_derivable():
    plain = _required(_sensor_schema({}, three_phase=True, home_battery=False))
    assert CONF_GRID_IMPORT in plain and CONF_HOME_CONSUMPTION in plain
    derived = _required(
        _sensor_schema({}, three_phase=True, home_battery=False, derivable=True)
    )
    assert CONF_GRID_IMPORT not in derived
    assert CONF_HOME_CONSUMPTION not in derived
    assert CONF_FV_PRODUCTION in derived


def test_flow_derivable_after_meter_step():
    flow = EVSCConfigFlow()
    flow.mode_info = {CONF_PHASE_MODE: PHASE_MODE_THREE}
    flow.features_info = {
        CONF_HAS_SOLAR: True,
        CONF_HAS_HOME_BATTERY: False,
        CONF_LOAD_BALANCING: LOAD_BALANCING_INTEGRATION,
    }
    assert flow._derivable() is False
    flow.fuse_info = {
        key: THREE_METER[key]
        for key in (
            CONF_MAIN_FUSE_SIZE,
            CONF_FUSE_GRID_IMPORT_L1,
            CONF_FUSE_GRID_IMPORT_L2,
            CONF_FUSE_GRID_IMPORT_L3,
            CONF_FUSE_CHARGER_CURRENT,
        )
    }
    assert flow._derivable() is True
    flow.features_info = dict(flow.features_info, **{CONF_LOAD_BALANCING: LOAD_BALANCING_NONE})
    assert flow._derivable() is False
