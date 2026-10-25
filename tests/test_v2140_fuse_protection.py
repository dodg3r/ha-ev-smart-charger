"""v2.14.0 — main-fuse protection: controller ceiling, watchdog, config flow."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest

from custom_components.ev_smart_charger import number as number_platform
from custom_components.ev_smart_charger import sensor as sensor_platform
from custom_components.ev_smart_charger.charger_controller import ChargerController
from custom_components.ev_smart_charger.config_flow import (
    EVSCConfigFlow,
    _normalize_main_fuse_input,
)
from custom_components.ev_smart_charger.const import (
    CHARGER_MODEL_GENERIC,
    CHARGER_MODEL_TUYA,
    CONF_CHARGER_MODEL,
    CONF_EV_CHARGER_CURRENT,
    CONF_EV_CHARGER_SWITCH,
    CONF_FUSE_CHARGER_CURRENT,
    CONF_FUSE_GRID_CURRENT_L1,
    CONF_FUSE_GRID_CURRENT_L2,
    CONF_FUSE_GRID_CURRENT_L3,
    CONF_FUSE_GRID_EXPORT_L1,
    CONF_FUSE_GRID_EXPORT_L2,
    CONF_FUSE_GRID_EXPORT_L3,
    CONF_FUSE_GRID_IMPORT_L1,
    CONF_FUSE_GRID_IMPORT_L2,
    CONF_FUSE_GRID_IMPORT_L3,
    CONF_FUSE_GRID_VOLTAGE_L1,
    CONF_MAIN_FUSE_SIZE,
    CONF_PHASE_MODE,
    CONF_SOC_CAR,
    CONF_SOC_HOME,
    FUSE_PROTECTION_ENTITIES,
    HELPER_FUSE_HEADROOM_SENSOR_SUFFIX,
    HELPER_FUSE_MARGIN_SUFFIX,
    HELPER_MAIN_FUSE_SENSOR_SUFFIX,
    PHASE_MODE_SINGLE,
    PHASE_MODE_THREE,
    TOTAL_INTEGRATION_ENTITIES,
    TOTAL_INTEGRATION_ENTITIES_NO_BATTERY,
    get_expected_entity_count,
)
from custom_components.ev_smart_charger.fuse_guard import FuseGuard
from custom_components.ev_smart_charger.runtime import EVSCRuntimeData


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


@pytest.fixture
def services(hass):
    """Fake switch/number services that echo their value into the state."""
    calls: list[tuple[str, str, dict]] = []

    async def turn_on(call):
        calls.append(("switch", "turn_on", dict(call.data)))
        hass.states.async_set(call.data["entity_id"], "on")

    async def turn_off(call):
        calls.append(("switch", "turn_off", dict(call.data)))
        hass.states.async_set(call.data["entity_id"], "off")

    async def set_value(call):
        calls.append(("number", "set_value", dict(call.data)))
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]))

    hass.services.async_register("switch", "turn_on", turn_on)
    hass.services.async_register("switch", "turn_off", turn_off)
    hass.services.async_register("number", "set_value", set_value)
    return calls


def _runtime(guard=None) -> EVSCRuntimeData:
    runtime = EVSCRuntimeData(config={}, expected_entity_count=0)
    runtime.fuse_guard = guard
    return runtime


def _guard_stub(ceiling):
    return SimpleNamespace(enabled=True, max_allowed_amps=lambda: ceiling)


def _controller(hass, runtime, model=CHARGER_MODEL_GENERIC) -> ChargerController:
    config = {
        CONF_EV_CHARGER_SWITCH: "switch.charger",
        CONF_EV_CHARGER_CURRENT: "number.charger_current",
        CONF_CHARGER_MODEL: model,
    }
    return ChargerController(hass, "entry", config, runtime_data=runtime)


# ----------------------------------------------------------------------------
# entity count
# ----------------------------------------------------------------------------


def test_expected_entity_count_includes_fuse_entities_only_when_enabled():
    assert get_expected_entity_count({CONF_SOC_HOME: "sensor.x"}) == TOTAL_INTEGRATION_ENTITIES
    assert get_expected_entity_count({}) == TOTAL_INTEGRATION_ENTITIES_NO_BATTERY
    assert (
        get_expected_entity_count({CONF_MAIN_FUSE_SIZE: 25})
        == TOTAL_INTEGRATION_ENTITIES_NO_BATTERY + FUSE_PROTECTION_ENTITIES
    )
    assert get_expected_entity_count({CONF_MAIN_FUSE_SIZE: "0"}) == (
        TOTAL_INTEGRATION_ENTITIES_NO_BATTERY
    )


# ----------------------------------------------------------------------------
# ChargerController: ceiling on every command
# ----------------------------------------------------------------------------


async def test_start_is_capped_to_fuse_ceiling(hass, services):
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("number.charger_current", "6")
    controller = _controller(hass, _runtime(_guard_stub(10)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.start_charger(target_amps=16, reason="test")

    assert result.success is True
    assert services[:2] == [
        ("number", "set_value", {"entity_id": "number.charger_current", "value": 10}),
        ("switch", "turn_on", {"entity_id": "switch.charger"}),
    ]


async def test_start_refused_when_ceiling_is_zero(hass, services):
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("number.charger_current", "6")
    controller = _controller(hass, _runtime(_guard_stub(0)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.start_charger(target_amps=16, reason="test")

    assert result.success is False
    assert "fuse" in (result.error_message or "").lower()
    assert ("switch", "turn_on", {"entity_id": "switch.charger"}) not in services


async def test_tuya_ceiling_snaps_down_to_a_level(hass, services):
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("number.charger_current", "6")
    controller = _controller(hass, _runtime(_guard_stub(12)), model=CHARGER_MODEL_TUYA)
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.start_charger(target_amps=16, reason="test")

    assert services[0] == (
        "number", "set_value", {"entity_id": "number.charger_current", "value": 10}
    )


async def test_set_amperage_is_capped(hass, services):
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "8")
    controller = _controller(hass, _runtime(_guard_stub(11)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.set_amperage(16, reason="solar")

    assert result.amperage == 11
    assert services == [
        ("number", "set_value", {"entity_id": "number.charger_current", "value": 11}),
    ]


async def test_set_amperage_with_zero_ceiling_stops(hass, services):
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "8")
    controller = _controller(hass, _runtime(_guard_stub(0)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.set_amperage(16, reason="solar")

    assert result.operation == "stop"
    assert services == [("switch", "turn_off", {"entity_id": "switch.charger"})]


async def test_no_guard_means_no_cap(hass, services):
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("number.charger_current", "6")
    controller = _controller(hass, _runtime(None))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.start_charger(target_amps=16, reason="test")

    assert services[0][2]["value"] == 16


# ----------------------------------------------------------------------------
# ChargerController: watchdog enforcement
# ----------------------------------------------------------------------------


async def test_enforce_reduces_generic_charger_live(hass, services):
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "16")
    controller = _controller(hass, _runtime(_guard_stub(9)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.async_enforce_fuse_ceiling(9, "test")

    assert services == [
        ("number", "set_value", {"entity_id": "number.charger_current", "value": 9}),
    ]


async def test_enforce_tuya_uses_safe_sequence(hass, services):
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "16")
    controller = _controller(hass, _runtime(_guard_stub(10)), model=CHARGER_MODEL_TUYA)
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.async_enforce_fuse_ceiling(10, "test")

    assert services == [
        ("switch", "turn_off", {"entity_id": "switch.charger"}),
        ("number", "set_value", {"entity_id": "number.charger_current", "value": 10}),
        ("switch", "turn_on", {"entity_id": "switch.charger"}),
    ]


async def test_enforce_zero_stops_charger(hass, services):
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "16")
    controller = _controller(hass, _runtime(_guard_stub(0)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.async_enforce_fuse_ceiling(0, "test")

    assert services == [("switch", "turn_off", {"entity_id": "switch.charger"})]


async def test_enforce_is_noop_when_off_or_below(hass, services):
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("number.charger_current", "16")
    controller = _controller(hass, _runtime(_guard_stub(6)))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        await controller.async_enforce_fuse_ceiling(6, "test")
        hass.states.async_set("switch.charger", "on")
        hass.states.async_set("number.charger_current", "6")
        await controller.async_enforce_fuse_ceiling(10, "test")

    assert services == []


async def test_enforce_ignores_rate_limit(hass, services):
    """A fuse reduction must not wait behind the 30 s operation interval."""
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "16")
    controller = _controller(hass, _runtime(_guard_stub(8)))
    await controller.async_setup()
    controller._record_operation_time()  # an operation just happened

    sleep = AsyncMock()
    with patch("asyncio.sleep", new=sleep):
        await controller.async_enforce_fuse_ceiling(8, "test")

    assert services[-1][2]["value"] == 8
    for call in sleep.await_args_list:
        assert call.args[0] < 30


# ----------------------------------------------------------------------------
# ChargerController: current entity that only exists during a session (OCPP)
# ----------------------------------------------------------------------------


async def test_start_with_unavailable_current_entity_sets_after_turn_on(hass):
    calls: list[tuple[str, dict]] = []

    async def turn_on(call):
        calls.append(("turn_on", dict(call.data)))
        hass.states.async_set("switch.charger", "on")
        # OCPP: the session current limit appears once the session starts.
        hass.states.async_set("number.charger_current", "32")

    async def set_value(call):
        calls.append(("set_value", dict(call.data)))
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]))

    hass.services.async_register("switch", "turn_on", turn_on)
    hass.services.async_register("number", "set_value", set_value)
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("number.charger_current", "unavailable")
    controller = _controller(hass, _runtime(None))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.start_charger(target_amps=6, reason="test")

    assert result.success is True
    assert calls == [
        ("turn_on", {"entity_id": "switch.charger"}),
        ("set_value", {"entity_id": "number.charger_current", "value": 6}),
    ]


async def test_start_with_current_entity_never_appearing_still_starts(hass):
    calls: list[str] = []

    async def turn_on(call):
        calls.append("turn_on")
        hass.states.async_set("switch.charger", "on")

    async def set_value(call):
        calls.append("set_value")

    hass.services.async_register("switch", "turn_on", turn_on)
    hass.services.async_register("number", "set_value", set_value)
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("number.charger_current", "unavailable")
    controller = _controller(hass, _runtime(None))
    await controller.async_setup()

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await controller.start_charger(target_amps=6, reason="test")

    assert result.success is True
    assert calls == ["turn_on"]


# ----------------------------------------------------------------------------
# FuseGuard against real Home Assistant states
# ----------------------------------------------------------------------------


THREE_PHASE_CONFIG = {
    CONF_MAIN_FUSE_SIZE: 25,
    CONF_PHASE_MODE: PHASE_MODE_THREE,
    CONF_CHARGER_MODEL: CHARGER_MODEL_GENERIC,
    CONF_EV_CHARGER_SWITCH: "switch.charger",
    CONF_EV_CHARGER_CURRENT: "number.charger_current",
    CONF_FUSE_GRID_CURRENT_L1: "sensor.i1",
    CONF_FUSE_GRID_CURRENT_L2: "sensor.i2",
    CONF_FUSE_GRID_CURRENT_L3: "sensor.i3",
    CONF_FUSE_GRID_IMPORT_L1: "sensor.p_in1",
    CONF_FUSE_GRID_IMPORT_L2: "sensor.p_in2",
    CONF_FUSE_GRID_IMPORT_L3: "sensor.p_in3",
    CONF_FUSE_GRID_EXPORT_L1: "sensor.p_out1",
    CONF_FUSE_GRID_EXPORT_L2: "sensor.p_out2",
    CONF_FUSE_GRID_EXPORT_L3: "sensor.p_out3",
    CONF_FUSE_CHARGER_CURRENT: "sensor.charger_current_import",
}


def _set_phase(hass, n, amps, exporting=False):
    hass.states.async_set(f"sensor.i{n}", str(abs(amps)), {"unit_of_measurement": "A"})
    watts = abs(amps) * 230
    hass.states.async_set(f"sensor.p_in{n}", "0" if exporting else str(watts))
    hass.states.async_set(f"sensor.p_out{n}", str(watts) if exporting else "0")


def _set_charger(hass, on, setpoint, per_phase):
    hass.states.async_set("switch.charger", "on" if on else "off")
    hass.states.async_set("number.charger_current", str(setpoint))
    total = max(per_phase) if per_phase else 0
    hass.states.async_set(
        "sensor.charger_current_import",
        str(total),
        {"L1": per_phase[0], "L2": per_phase[1], "L3": per_phase[2]},
    )


async def _guard(hass, config=None, controller=None):
    controller = controller or SimpleNamespace(
        async_enforce_fuse_ceiling=AsyncMock()
    )
    guard = FuseGuard(hass, "entry", config or THREE_PHASE_CONFIG, _runtime(), controller)
    await guard.async_setup()
    return guard, controller


async def test_guard_disabled_without_fuse_size(hass):
    guard = FuseGuard(hass, "entry", {}, _runtime(), SimpleNamespace())
    await guard.async_setup()
    assert guard.enabled is False
    assert guard.max_allowed_amps() is None
    await guard.async_remove()


async def test_guard_idle_house_limit(hass):
    _set_phase(hass, 1, 5)
    _set_phase(hass, 2, 8)
    _set_phase(hass, 3, 3)
    _set_charger(hass, False, 6, [0, 0, 0])
    guard, controller = await _guard(hass)

    assert guard.max_allowed_amps() == 15  # 25 - 2 (default margin) - 8
    assert guard.last_limit.bottleneck_phase == 2
    controller.async_enforce_fuse_ceiling.assert_not_awaited()
    await guard.async_remove()


async def test_guard_export_gives_room(hass):
    for n in (1, 2, 3):
        _set_phase(hass, n, 14, exporting=True)
    _set_charger(hass, False, 6, [0, 0, 0])
    guard, _ = await _guard(hass)

    assert guard.max_allowed_amps() == 32  # charger maximum
    await guard.async_remove()


async def test_guard_missing_reading_falls_back_to_minimum(hass):
    _set_phase(hass, 1, 5)
    _set_phase(hass, 3, 5)
    hass.states.async_set("sensor.i2", "unavailable")
    hass.states.async_set("sensor.p_in2", "unavailable")
    hass.states.async_set("sensor.p_out2", "unavailable")
    _set_charger(hass, False, 6, [0, 0, 0])
    guard, _ = await _guard(hass)

    assert guard.max_allowed_amps() == 6
    assert guard.last_limit.valid is False
    await guard.async_remove()


async def test_guard_enforces_when_running_above_ceiling(hass):
    # Meter includes the charger's 16 A on every phase; house is 10/4/4 A.
    _set_phase(hass, 1, 26)
    _set_phase(hass, 2, 20)
    _set_phase(hass, 3, 20)
    _set_charger(hass, True, 16, [16, 16, 16])
    guard, controller = await _guard(hass)
    await hass.async_block_till_done()

    # Room on L1: 25 - 2 - 10 = 13 A
    controller.async_enforce_fuse_ceiling.assert_awaited_with(13, ANY)
    await guard.async_remove()


async def test_guard_stops_on_overload(hass):
    _set_phase(hass, 1, 40)
    _set_phase(hass, 2, 10)
    _set_phase(hass, 3, 10)
    _set_charger(hass, True, 6, [6, 6, 6])
    guard, controller = await _guard(hass)
    await hass.async_block_till_done()

    controller.async_enforce_fuse_ceiling.assert_awaited_with(0, ANY)
    await guard.async_remove()


async def test_guard_reacts_to_meter_change(hass):
    _set_phase(hass, 1, 10)
    _set_phase(hass, 2, 10)
    _set_phase(hass, 3, 10)
    _set_charger(hass, True, 6, [6, 6, 6])
    guard, controller = await _guard(hass)
    await hass.async_block_till_done()
    controller.async_enforce_fuse_ceiling.assert_not_awaited()

    # A large load on L2 pushes the phase over the fuse.
    _set_phase(hass, 2, 30)
    await hass.async_block_till_done()

    controller.async_enforce_fuse_ceiling.assert_awaited_with(0, ANY)
    await guard.async_remove()


async def test_guard_zero_total_ignores_stale_attributes(hass):
    _set_phase(hass, 1, 10)
    _set_phase(hass, 2, 10)
    _set_phase(hass, 3, 10)
    hass.states.async_set("switch.charger", "on")
    hass.states.async_set("number.charger_current", "16")
    hass.states.async_set(
        "sensor.charger_current_import", "0", {"L1": 16, "L2": 16, "L3": 16}
    )
    guard, _ = await _guard(hass)

    # Charger contributes 0 A (not 16), so the house is 10 A -> room 13 A.
    assert guard.max_allowed_amps() == 13
    await guard.async_remove()


async def test_guard_single_phase_power_fallback(hass):
    config = {
        CONF_MAIN_FUSE_SIZE: 16,
        CONF_PHASE_MODE: PHASE_MODE_SINGLE,
        CONF_CHARGER_MODEL: CHARGER_MODEL_GENERIC,
        CONF_EV_CHARGER_SWITCH: "switch.charger",
        CONF_EV_CHARGER_CURRENT: "number.charger_current",
        CONF_FUSE_GRID_IMPORT_L1: "sensor.p_in1",
        CONF_FUSE_GRID_VOLTAGE_L1: "sensor.u1",
        CONF_FUSE_CHARGER_CURRENT: "sensor.charger_current_import",
    }
    hass.states.async_set("sensor.p_in1", "1150")
    hass.states.async_set("sensor.u1", "230")
    hass.states.async_set("switch.charger", "off")
    hass.states.async_set("number.charger_current", "6")
    hass.states.async_set("sensor.charger_current_import", "0")
    guard, _ = await _guard(hass, config)

    # 1150 W / 230 V = 5 A -> 16 - 2 - 5 = 9 A
    assert guard.max_allowed_amps() == 9
    await guard.async_remove()


# ----------------------------------------------------------------------------
# config flow
# ----------------------------------------------------------------------------


async def test_main_fuse_step_requires_readings_when_enabled(hass):
    flow = EVSCConfigFlow()
    flow.hass = hass
    flow.mode_info = {CONF_PHASE_MODE: PHASE_MODE_THREE}

    result = await flow.async_step_main_fuse({CONF_MAIN_FUSE_SIZE: "25"})

    assert result["step_id"] == "main_fuse"
    assert result["errors"] == {
        CONF_FUSE_GRID_CURRENT_L1: "fuse_phase_reading_required",
        CONF_FUSE_GRID_CURRENT_L2: "fuse_phase_reading_required",
        CONF_FUSE_GRID_CURRENT_L3: "fuse_phase_reading_required",
        CONF_FUSE_CHARGER_CURRENT: "fuse_charger_current_required",
    }


async def test_main_fuse_step_stores_normalized_data(hass):
    flow = EVSCConfigFlow()
    flow.hass = hass
    flow.mode_info = {CONF_PHASE_MODE: PHASE_MODE_THREE}
    payload = {
        CONF_MAIN_FUSE_SIZE: "25",
        CONF_FUSE_GRID_CURRENT_L1: "sensor.i1",
        CONF_FUSE_GRID_CURRENT_L2: "sensor.i2",
        CONF_FUSE_GRID_IMPORT_L3: "sensor.p_in3",
        CONF_FUSE_CHARGER_CURRENT: "sensor.charger_current_import",
    }

    result = await flow.async_step_main_fuse(payload)

    assert result["step_id"] == "pv_forecast"
    assert flow.fuse_info[CONF_MAIN_FUSE_SIZE] == 25
    assert flow.fuse_info[CONF_FUSE_GRID_CURRENT_L1] == "sensor.i1"
    assert flow.fuse_info[CONF_FUSE_GRID_CURRENT_L3] is None


async def test_main_fuse_off_skips_validation(hass):
    flow = EVSCConfigFlow()
    flow.hass = hass
    flow.mode_info = {CONF_PHASE_MODE: PHASE_MODE_THREE}

    result = await flow.async_step_main_fuse({CONF_MAIN_FUSE_SIZE: "0"})

    assert result["step_id"] == "pv_forecast"
    assert flow.fuse_info[CONF_MAIN_FUSE_SIZE] == 0


def test_normalize_drops_other_phases_in_single_phase():
    data = _normalize_main_fuse_input(
        {
            CONF_MAIN_FUSE_SIZE: "20",
            CONF_FUSE_GRID_CURRENT_L1: "sensor.i1",
            CONF_FUSE_GRID_CURRENT_L2: "sensor.i2",
        },
        three_phase=False,
    )
    assert data[CONF_FUSE_GRID_CURRENT_L1] == "sensor.i1"
    assert data[CONF_FUSE_GRID_CURRENT_L2] is None


def test_normalize_clears_removed_mapping():
    """Leaving a field empty must remove the old mapping on reconfigure."""
    data = _normalize_main_fuse_input({CONF_MAIN_FUSE_SIZE: "25"}, three_phase=True)
    assert data[CONF_FUSE_CHARGER_CURRENT] is None
    assert data[CONF_FUSE_GRID_IMPORT_L2] is None


# ----------------------------------------------------------------------------
# platforms
# ----------------------------------------------------------------------------


def _entry(runtime, data):
    return SimpleNamespace(entry_id="entry_123", runtime_data=runtime, data=data)


def _collect():
    entities = []
    return entities, lambda new, *_a: entities.extend(new)


async def test_fuse_entities_created_only_when_enabled(hass):
    base = {CONF_SOC_CAR: "sensor.ev_soc", CONF_CHARGER_MODEL: CHARGER_MODEL_GENERIC}

    for data, expected in ((base, False), ({**base, CONF_MAIN_FUSE_SIZE: 25}, True)):
        runtime = EVSCRuntimeData(config=data, expected_entity_count=0)
        numbers, add_numbers = _collect()
        sensors, add_sensors = _collect()
        await number_platform.async_setup_entry(hass, _entry(runtime, data), add_numbers)
        await sensor_platform.async_setup_entry(hass, _entry(runtime, data), add_sensors)

        number_keys = {e._entity_key: e for e in numbers}
        sensor_keys = {e._entity_key: e for e in sensors}
        assert (HELPER_FUSE_MARGIN_SUFFIX in number_keys) is expected
        assert (HELPER_MAIN_FUSE_SENSOR_SUFFIX in sensor_keys) is expected
        assert (HELPER_FUSE_HEADROOM_SENSOR_SUFFIX in sensor_keys) is expected

        if expected:
            margin = number_keys[HELPER_FUSE_MARGIN_SUFFIX]
            assert margin.native_step == 0.5  # not rewritten to 1 A on generic
            assert margin.native_value == 2.0
            assert sensor_keys[HELPER_MAIN_FUSE_SENSOR_SUFFIX].native_value == 25
            headroom = sensor_keys[HELPER_FUSE_HEADROOM_SENSOR_SUFFIX]
            headroom.publish(13, {"state": "limited"})
            assert headroom.native_value == 13
            assert headroom.extra_state_attributes == {"state": "limited"}
