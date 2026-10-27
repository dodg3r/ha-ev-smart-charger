"""v2.24.1: the night session sensor never restores a running session."""
from unittest.mock import AsyncMock, Mock

from homeassistant.core import State

from custom_components.ev_smart_charger.const import (
    HELPER_NIGHT_SESSION_STATE_SUFFIX,
    NIGHT_CHARGE_MODE_IDLE,
)
from custom_components.ev_smart_charger.runtime import EVSCRuntimeData
from custom_components.ev_smart_charger.sensor import EVSCNightSessionStateSensor


async def test_night_session_sensor_starts_idle_after_restart(hass):
    runtime = EVSCRuntimeData(config={}, expected_entity_count=100)
    sensor = EVSCNightSessionStateSensor(
        runtime,
        "entry_123",
        HELPER_NIGHT_SESSION_STATE_SUFFIX,
        "EVSC Night Session State",
        "mdi:weather-night",
    )
    sensor.hass = hass
    sensor.async_get_last_state = AsyncMock(
        return_value=State("sensor.night", "grid", {"x": 1})
    )
    sensor.async_write_ha_state = Mock()

    await sensor.async_added_to_hass()

    assert sensor.native_value == NIGHT_CHARGE_MODE_IDLE
    assert sensor.extra_state_attributes == {}
    sensor.async_write_ha_state.assert_called()
