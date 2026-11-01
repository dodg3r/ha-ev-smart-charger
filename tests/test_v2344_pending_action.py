"""v2.34.4: Solar Surplus reports what it is waiting to do, and when."""
import logging
import time
from datetime import datetime

from homeassistant.util import dt as dt_util

from custom_components.ev_smart_charger.utils import state_helper

from .test_solar_surplus import automation  # noqa: F401  (fixture)


def test_no_timer_means_no_pending(hass, automation):  # noqa: F811
    pending = automation._pending_action()
    assert pending["pending_action"] is None
    assert pending["pending_until"] is None


def test_surplus_drop_at_floor_is_a_stop(hass, automation):  # noqa: F811
    hass.states.async_set("number.surplus_delay", "60")
    automation._tick_current_amps = 6
    automation._last_surplus_sufficient = time.monotonic() - 15
    pending = automation._pending_action()
    assert pending["pending_action"] == "surplus_drop"
    assert pending["pending_stop"] is True
    until = datetime.fromisoformat(pending["pending_until"])
    left = (until - dt_util.now()).total_seconds()
    assert 40 <= left <= 46


def test_grid_import_above_floor_reduces(hass, automation):  # noqa: F811
    hass.states.async_set("number.grid_delay", "30")
    automation._tick_current_amps = 10
    automation._last_grid_import_high = time.monotonic()
    pending = automation._pending_action()
    assert pending["pending_action"] == "grid_import"
    assert pending["pending_stop"] is False


def test_elapsed_timer_is_not_pending(hass, automation):  # noqa: F811
    hass.states.async_set("number.surplus_delay", "30")
    automation._last_surplus_sufficient = time.monotonic() - 120
    assert automation._pending_action()["pending_action"] is None


def test_unmapped_entity_reads_default_silently(hass, caplog):
    state_helper.start_startup_grace(hass, seconds=0)
    with caplog.at_level(logging.DEBUG):
        assert state_helper.get_float(hass, None, 0.0) == 0.0
    assert not [r for r in caplog.records if "Entity None" in r.message]
