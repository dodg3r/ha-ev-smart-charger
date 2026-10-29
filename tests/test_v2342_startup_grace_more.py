"""v2.34.2: the EV SOC monitor is quiet while the car sensor is still loading."""
import logging

from custom_components.ev_smart_charger.const import CONF_SOC_CAR
from custom_components.ev_smart_charger.ev_soc_monitor import EVSOCMonitor
from custom_components.ev_smart_charger.utils import state_helper


def _monitor(hass):
    return EVSOCMonitor(hass, "entry", {CONF_SOC_CAR: "sensor.car_soc"})


async def test_missing_car_sensor_is_debug_during_grace(hass, caplog):
    state_helper.start_startup_grace(hass, seconds=60)
    monitor = _monitor(hass)
    with caplog.at_level(logging.DEBUG):
        await monitor._async_poll_source_sensor()
    records = [r for r in caplog.records if "sensor.car_soc" in r.message]
    assert records and all(r.levelno == logging.DEBUG for r in records)


async def test_missing_car_sensor_errors_once_after_grace(hass, caplog):
    state_helper.start_startup_grace(hass, seconds=0)
    monitor = _monitor(hass)
    with caplog.at_level(logging.DEBUG):
        await monitor._async_poll_source_sensor()
        await monitor._async_poll_source_sensor()
    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "sensor.car_soc" in r.message
    ]
    assert len(errors) == 1
