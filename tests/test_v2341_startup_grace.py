"""v2.34.1: missing sensor values right after startup are not warnings."""
import logging

from custom_components.ev_smart_charger.utils import state_helper


async def test_missing_value_is_debug_during_startup_grace(hass, caplog):
    state_helper.start_startup_grace(hass, seconds=60)
    with caplog.at_level(logging.DEBUG):
        assert state_helper.get_float(hass, "sensor.not_there", None) is None
    records = [r for r in caplog.records if "sensor.not_there" in r.message]
    assert records and all(r.levelno == logging.DEBUG for r in records)


async def test_missing_value_warns_after_startup_grace(hass, caplog):
    state_helper.start_startup_grace(hass, seconds=0)
    with caplog.at_level(logging.DEBUG):
        assert state_helper.get_float(hass, "sensor.not_there", 1.5) == 1.5
    assert any(
        r.levelno == logging.WARNING and "sensor.not_there" in r.message
        for r in caplog.records
    )
