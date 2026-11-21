"""v2.54.0: "Keep weekly charging" off turns a car-ready day off after use."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.ev_smart_charger.const import (
    HELPER_KEEP_WEEKLY_CHARGE_SUFFIX,
    WEEKDAY_KEYS,
)
from custom_components.ev_smart_charger.planned_night_charge import (
    PlannedNightCharge,
)



def _monday(**kw) -> datetime:
    """Monday 23 November 2026 in HA's time zone (set by the hass fixture)."""
    return datetime(2026, 11, 23, tzinfo=dt_util.DEFAULT_TIME_ZONE).replace(**kw)


def _entities() -> dict[str, str]:
    ids = {HELPER_KEEP_WEEKLY_CHARGE_SUFFIX: "switch.keep_weekly"}
    for day in WEEKDAY_KEYS:
        ids[f"evsc_car_ready_{day}"] = f"switch.car_ready_{day}"
        ids[f"evsc_car_ready_time_{day}"] = f"time.ready_{day}"
    return ids


def _plan(hass, calls) -> PlannedNightCharge:
    ids = _entities()
    runtime = SimpleNamespace(
        power_model=None,
        get_entity_id=ids.get,
        get_entity=lambda _key: None,
    )

    async def turn_off(call):
        calls.append(call.data["entity_id"])
        hass.states.async_set(call.data["entity_id"], "off")

    hass.services.async_register("switch", "turn_off", turn_off)
    for day in WEEKDAY_KEYS:
        hass.states.async_set(f"time.ready_{day}", "07:00:00")
        hass.states.async_set(f"switch.car_ready_{day}", "off")
    return PlannedNightCharge(hass, "entry", {}, None, None, runtime)


async def test_day_is_turned_off_when_ready_time_passes(hass):
    calls: list[str] = []
    plan = _plan(hass, calls)
    hass.states.async_set("switch.keep_weekly", "off")
    hass.states.async_set("switch.car_ready_monday", "on")

    await plan._release_passed_days(_monday(hour=6, minute=59, second=30))
    assert calls == []
    await plan._release_passed_days(_monday(hour=7, minute=0, second=30))
    assert calls == ["switch.car_ready_monday"]
    # Later ticks do not act again.
    await plan._release_passed_days(_monday(hour=7, minute=1, second=30))
    assert calls == ["switch.car_ready_monday"]


async def test_keep_weekly_on_leaves_the_day(hass):
    calls: list[str] = []
    plan = _plan(hass, calls)
    hass.states.async_set("switch.keep_weekly", "on")
    hass.states.async_set("switch.car_ready_monday", "on")

    await plan._release_passed_days(_monday(hour=6, minute=59))
    await plan._release_passed_days(_monday(hour=7, minute=1))
    assert calls == []


async def test_day_switched_on_after_its_ready_time_is_left_alone(hass):
    calls: list[str] = []
    plan = _plan(hass, calls)
    hass.states.async_set("switch.keep_weekly", "off")

    await plan._release_passed_days(_monday(hour=9, minute=0))
    hass.states.async_set("switch.car_ready_monday", "on")  # for next Monday
    await plan._release_passed_days(_monday(hour=9, minute=1))
    assert calls == []


async def test_first_tick_after_start_does_nothing(hass):
    calls: list[str] = []
    plan = _plan(hass, calls)
    hass.states.async_set("switch.keep_weekly", "off")
    hass.states.async_set("switch.car_ready_monday", "on")

    await plan._release_passed_days(_monday(hour=7, minute=0, second=30))
    assert calls == []


async def test_ready_time_passed_over_midnight_gap(hass):
    calls: list[str] = []
    plan = _plan(hass, calls)
    hass.states.async_set("switch.keep_weekly", "off")
    hass.states.async_set("switch.car_ready_tuesday", "on")

    await plan._release_passed_days(_monday(hour=23, minute=59))
    await plan._release_passed_days(
        _monday(day=24, hour=7, minute=0, second=10)
    )
    assert calls == ["switch.car_ready_tuesday"]
