"""v2.41.0: load balancing none / display only / display and adjust."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.ev_smart_charger.config_flow import _apply_features
from custom_components.ev_smart_charger.const import (
    CONF_LOAD_BALANCING,
    CONF_MAIN_FUSE_SIZE,
    FUSE_PROTECTION_ENTITIES,
    LOAD_BALANCING_CHARGER,
    LOAD_BALANCING_DISPLAY,
    LOAD_BALANCING_INTEGRATION,
    LOAD_BALANCING_NONE,
    TOTAL_INTEGRATION_ENTITIES_NO_BATTERY,
    fuse_protection_adjusts,
    get_expected_entity_count,
    get_load_balancing,
    is_fuse_protection_enabled,
)
from custom_components.ev_smart_charger.fuse_guard import FuseGuard

from .test_v2140_fuse_protection import (
    THREE_PHASE_CONFIG,
    _runtime,
    _set_charger,
    _set_phase,
)


def test_modes():
    none = {CONF_MAIN_FUSE_SIZE: 25, CONF_LOAD_BALANCING: LOAD_BALANCING_NONE}
    display = {CONF_MAIN_FUSE_SIZE: 25, CONF_LOAD_BALANCING: LOAD_BALANCING_DISPLAY}
    adjust = {CONF_MAIN_FUSE_SIZE: 25, CONF_LOAD_BALANCING: LOAD_BALANCING_INTEGRATION}

    assert is_fuse_protection_enabled(none) is False
    assert is_fuse_protection_enabled(display) is True
    assert is_fuse_protection_enabled(adjust) is True
    assert fuse_protection_adjusts(display) is False
    assert fuse_protection_adjusts(adjust) is True

    base = TOTAL_INTEGRATION_ENTITIES_NO_BATTERY
    assert get_expected_entity_count(none) == base
    assert get_expected_entity_count(display) == base + FUSE_PROTECTION_ENTITIES
    assert get_expected_entity_count(adjust) == base + FUSE_PROTECTION_ENTITIES


def test_legacy_charger_choice_reads_as_none():
    old = {CONF_MAIN_FUSE_SIZE: 25, CONF_LOAD_BALANCING: LOAD_BALANCING_CHARGER}
    assert get_load_balancing(old) == LOAD_BALANCING_NONE
    assert is_fuse_protection_enabled(old) is False
    assert _apply_features(old)[CONF_LOAD_BALANCING] == LOAD_BALANCING_NONE
    # An entry from before v2.17.0 with a fuse size keeps protecting.
    assert get_load_balancing({CONF_MAIN_FUSE_SIZE: 25}) == LOAD_BALANCING_INTEGRATION


async def test_display_only_never_adjusts(hass):
    """Overloaded phase while charging: the guard computes a lower ceiling
    but neither caps commands nor enforces it."""
    config = dict(THREE_PHASE_CONFIG, **{CONF_LOAD_BALANCING: LOAD_BALANCING_DISPLAY})
    _set_phase(hass, 1, 30)
    _set_phase(hass, 2, 10)
    _set_phase(hass, 3, 10)
    _set_charger(hass, True, 16, [16, 16, 16])
    controller = SimpleNamespace(
        async_enforce_fuse_ceiling=AsyncMock(),
        async_restore_after_fuse=AsyncMock(),
    )
    guard = FuseGuard(hass, "entry", config, _runtime(), controller)
    await guard.async_setup()
    await hass.async_block_till_done()

    assert guard.enabled is True
    assert guard.adjusts is False
    assert guard.max_allowed_amps() is None  # no cap on any command
    assert guard.last_decision is not None
    assert guard.last_decision.allowed_a < 16  # but the advice is lower
    controller.async_enforce_fuse_ceiling.assert_not_awaited()
    await guard.async_remove()


async def test_display_and_adjust_enforces(hass):
    config = dict(THREE_PHASE_CONFIG, **{CONF_LOAD_BALANCING: LOAD_BALANCING_INTEGRATION})
    _set_phase(hass, 1, 30)
    _set_phase(hass, 2, 10)
    _set_phase(hass, 3, 10)
    _set_charger(hass, True, 16, [16, 16, 16])
    controller = SimpleNamespace(
        async_enforce_fuse_ceiling=AsyncMock(),
        async_restore_after_fuse=AsyncMock(),
    )
    guard = FuseGuard(hass, "entry", config, _runtime(), controller)
    await guard.async_setup()
    await hass.async_block_till_done()

    assert guard.adjusts is True
    controller.async_enforce_fuse_ceiling.assert_awaited()
    await guard.async_remove()
