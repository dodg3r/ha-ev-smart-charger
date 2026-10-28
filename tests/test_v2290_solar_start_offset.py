"""v2.29.0: adjustable Solar Surplus start threshold (offset on 6.5 / 5.5 A)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.ev_smart_charger import number as number_platform
from custom_components.ev_smart_charger.const import (
    CHARGER_MODEL_GENERIC,
    CONF_CHARGER_MODEL,
    CONF_EV_CHARGER_STATUS,
    CONF_FV_PRODUCTION,
    CONF_GRID_IMPORT,
    CONF_HOME_CONSUMPTION,
    CONF_SOC_CAR,
    HELPER_SOLAR_START_OFFSET_SUFFIX,
)
from custom_components.ev_smart_charger.runtime import EVSCRuntimeData
from custom_components.ev_smart_charger.solar_surplus import SolarSurplusAutomation

OFFSET = "number.solar_start_offset"


@pytest.fixture
def automation(hass, mock_charger_controller, mock_priority_balancer):
    config = {
        CONF_EV_CHARGER_STATUS: "sensor.charger_status",
        CONF_FV_PRODUCTION: "sensor.solar",
        CONF_HOME_CONSUMPTION: "sensor.consumption",
        CONF_GRID_IMPORT: "sensor.grid",
    }
    with patch("custom_components.ev_smart_charger.solar_surplus.AstralTimeService"):
        auto = SolarSurplusAutomation(
            hass, "test_entry", config, mock_priority_balancer, mock_charger_controller
        )
    auto._solar_start_offset_entity = OFFSET
    return auto


async def test_default_offset_keeps_legacy_thresholds(hass, automation):
    hass.states.async_set(OFFSET, "0")
    assert automation._start_threshold() == 6.5
    assert automation._stop_threshold() == 5.5
    assert automation._import_allowance_w() == 0
    assert automation._effective_grid_threshold(50) == 50
    # 6 A of surplus at 230 V: not enough to start
    assert automation._calculate_target_amperage(6 * 230, current_amperage=0) == 0


async def test_missing_entity_is_legacy(hass, automation):
    automation._solar_start_offset_entity = None
    assert automation._start_threshold() == 6.5
    assert automation._import_allowance_w() == 0


async def test_negative_offset_starts_on_less_sun(hass, automation):
    hass.states.async_set(OFFSET, "-2")
    assert automation._start_threshold() == 4.5
    assert automation._stop_threshold() == 3.5
    # 4.6 A of surplus starts at the 6 A floor
    assert automation._calculate_target_amperage(4.6 * 230, current_amperage=0) == 6
    # 3.8 A keeps a running 6 A session
    assert automation._calculate_target_amperage(3.8 * 230, current_amperage=6) == 6
    # 3.0 A stops it
    assert automation._calculate_target_amperage(3.0 * 230, current_amperage=6) == 0


async def test_negative_offset_allows_the_floor_gap_as_import(hass, automation):
    hass.states.async_set(OFFSET, "-2")
    # gap at the stop threshold: 6 A - 3.5 A = 2.5 A at 230 V
    assert automation._import_allowance_w() == pytest.approx(2.5 * 230)
    assert automation._effective_grid_threshold(50) == pytest.approx(50 + 2.5 * 230)


async def test_positive_offset_waits_for_more_sun(hass, automation):
    hass.states.async_set(OFFSET, "2")
    assert automation._start_threshold() == 8.5
    assert automation._import_allowance_w() == 0
    assert automation._calculate_target_amperage(8 * 230, current_amperage=0) == 0
    assert automation._calculate_target_amperage(9 * 230, current_amperage=0) == 8


async def test_offset_is_clamped(hass, automation):
    hass.states.async_set(OFFSET, "-9")
    assert automation._start_offset_amps() == -4.0
    hass.states.async_set(OFFSET, "unavailable")
    assert automation._start_offset_amps() == 0.0


async def test_offset_number_keeps_half_amp_steps_on_generic(hass):
    data = {CONF_SOC_CAR: "sensor.ev_soc", CONF_CHARGER_MODEL: CHARGER_MODEL_GENERIC}
    runtime = EVSCRuntimeData(config=data, expected_entity_count=0)
    entities = []
    await number_platform.async_setup_entry(
        hass,
        SimpleNamespace(entry_id="entry_123", runtime_data=runtime, data=data),
        lambda new, *_a: entities.extend(new),
    )
    offset = {e._entity_key: e for e in entities}[HELPER_SOLAR_START_OFFSET_SUFFIX]
    assert offset._attr_native_step == 0.5
    assert offset._attr_native_min_value == -4
    assert offset.native_value == 0
