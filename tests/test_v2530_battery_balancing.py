"""v2.53.0: a home battery that balances against the main fuse by itself."""
from __future__ import annotations

from unittest.mock import ANY

from custom_components.ev_smart_charger.config_flow import (
    _apply_features,
    _battery_errors,
    _normalize_battery_input,
)
from custom_components.ev_smart_charger.const import (
    CONF_BATTERY_PHASE_INVERT,
    CONF_BATTERY_PHASE_POWER_L1,
    CONF_BATTERY_PHASE_POWER_L2,
    CONF_BATTERY_PHASE_POWER_L3,
    CONF_BATTERY_SELF_BALANCING,
    CONF_HAS_HOME_BATTERY,
    CONF_SOC_HOME,
    battery_self_balances,
)
from custom_components.ev_smart_charger.fuse_math import (
    PhaseReading,
    compute_fuse_limit,
)

from .test_v2140_fuse_protection import (
    THREE_PHASE_CONFIG,
    _guard,
    _set_charger,
    _set_phase,
)

PHASES = {
    CONF_BATTERY_PHASE_POWER_L1: "sensor.inv_r",
    CONF_BATTERY_PHASE_POWER_L2: "sensor.inv_s",
    CONF_BATTERY_PHASE_POWER_L3: "sensor.inv_t",
}


def _reading(grid, charger=0.0, flex=None):
    return PhaseReading(grid_current_a=grid, charger_current_a=charger, flexible_a=flex)


def test_credit_gives_the_car_room():
    # L1: meter 26 A, car 16 A, battery takes 8 A -> firm house 2 A.
    readings = [_reading(26, 16, 8), _reading(20, 16, 0), _reading(20, 16, 0)]
    limit = compute_fuse_limit(readings, 25, 2, commanded_a=16, charger_on=True)
    assert limit.phases[0].flexible_a == 8
    assert limit.phases[0].house_current_a == 2
    assert limit.overloaded is False
    assert limit.limit_a == 19  # L2/L3: 25 - 2 - 4


def test_without_credit_behaviour_is_unchanged():
    readings = [_reading(26, 16), _reading(20, 16), _reading(20, 16)]
    limit = compute_fuse_limit(readings, 25, 2, commanded_a=16, charger_on=True)
    assert limit.overloaded is True
    assert limit.limit_a == 13


def test_credit_is_capped_to_the_house_load():
    limit = compute_fuse_limit(
        [_reading(18, 16, 10)], 25, 2, commanded_a=16, charger_on=True
    )
    assert limit.phases[0].flexible_a == 2
    assert limit.phases[0].house_current_a == 0


def test_overload_the_battery_cannot_remove_still_counts():
    # House 20 A + battery 4 A + car 6 A = 30 A; firm 26 A > 23 A.
    limit = compute_fuse_limit(
        [_reading(30, 6, 4)], 25, 2, commanded_a=6, charger_on=True
    )
    assert limit.overloaded is True


def test_hard_limit_uses_the_raw_meter():
    limit = compute_fuse_limit(
        [_reading(36, 6, 20)],
        25,
        2,
        commanded_a=6,
        charger_on=True,
        hard_limit_a=35,
    )
    assert limit.overloaded is False
    assert limit.hard_trip is True


def test_flow_requires_every_phase():
    data = _normalize_battery_input(
        {CONF_SOC_HOME: "sensor.soc", CONF_BATTERY_SELF_BALANCING: True,
         CONF_BATTERY_PHASE_POWER_L1: "sensor.inv_r"}
    )
    assert data[CONF_BATTERY_PHASE_POWER_L2] is None
    assert _battery_errors(data) == {"base": "battery_phase_required"}
    data.update(PHASES)
    assert _battery_errors(data) == {}
    assert _battery_errors(_normalize_battery_input({})) == {}


def test_no_battery_clears_the_fields():
    data = _apply_features(
        {CONF_HAS_HOME_BATTERY: False, CONF_BATTERY_SELF_BALANCING: True, **PHASES}
    )
    assert data[CONF_BATTERY_SELF_BALANCING] is False
    assert data[CONF_BATTERY_PHASE_POWER_L1] is None


def test_self_balancing_needs_soc_and_all_phases():
    base = {CONF_SOC_HOME: "sensor.soc", CONF_BATTERY_SELF_BALANCING: True}
    assert battery_self_balances({**base, **PHASES}) is True
    assert battery_self_balances(base) is False
    assert battery_self_balances({**PHASES, CONF_BATTERY_SELF_BALANCING: True}) is False


async def test_guard_lets_the_battery_yield_first(hass):
    """FoxESS-style sensors: kW, minus while charging, invert ticked."""
    config = dict(
        THREE_PHASE_CONFIG,
        **PHASES,
        **{
            CONF_SOC_HOME: "sensor.soc",
            CONF_BATTERY_SELF_BALANCING: True,
            CONF_BATTERY_PHASE_INVERT: True,
        },
    )
    _set_phase(hass, 1, 26)
    _set_phase(hass, 2, 20)
    _set_phase(hass, 3, 20)
    _set_charger(hass, True, 16, [16, 16, 16])
    hass.states.async_set("sensor.inv_r", "-1.84", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.inv_s", "0", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.inv_t", "0.5", {"unit_of_measurement": "kW"})
    guard, controller = await _guard(hass, config)
    await hass.async_block_till_done()

    assert round(guard.last_limit.phases[0].flexible_a, 1) == 8.0
    assert guard.last_limit.phases[2].flexible_a == 0  # discharging: no credit
    assert guard.max_allowed_amps() >= 16
    controller.async_enforce_fuse_ceiling.assert_not_awaited()
    await guard.async_remove()


async def test_guard_without_the_box_still_lowers(hass):
    config = dict(THREE_PHASE_CONFIG, **PHASES, **{CONF_SOC_HOME: "sensor.soc"})
    _set_phase(hass, 1, 26)
    _set_phase(hass, 2, 20)
    _set_phase(hass, 3, 20)
    _set_charger(hass, True, 16, [16, 16, 16])
    hass.states.async_set("sensor.inv_r", "-1.84", {"unit_of_measurement": "kW"})
    guard, controller = await _guard(hass, config)
    await hass.async_block_till_done()

    controller.async_enforce_fuse_ceiling.assert_awaited_with(13, ANY)
    await guard.async_remove()
