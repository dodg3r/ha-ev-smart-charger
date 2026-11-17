"""v2.52.0: the charge plan uses every hour; the night before a sunny day is a reserve."""
from __future__ import annotations

from datetime import timedelta

from custom_components.ev_smart_charger.charge_planner import build_plan, window_hours
from custom_components.ev_smart_charger.const import (
    CONF_BATTERY_CAPACITY,
    CONF_EV_CHARGER_STATUS,
    CONF_PRICE_SENSOR,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_TOMORROW,
    CONF_SOC_CAR,
    WEEKDAY_KEYS,
)
from custom_components.ev_smart_charger.planned_night_charge import (
    PlannedNightCharge,
)

from .test_v2200_planned_night_charge import _at, _base, _controller, _runtime
from unittest.mock import Mock


def _set_prices(hass, base, prices: dict[int, float]) -> None:
    raw = [
        {
            "start": _at(base, h).isoformat(),
            "end": (_at(base, h) + timedelta(hours=1)).isoformat(),
            "value": v,
        }
        for h, v in sorted(prices.items())
    ]
    hass.states.async_set(
        "sensor.prices",
        "0.5",
        {"raw_today": raw, "raw_tomorrow": [], "unit_of_measurement": "SEK/kWh"},
    )


async def _night(hass, base, *, ready="18:00:00", soc="60", forecast=None, threshold="15"):
    """Plan component with prices for every hour of the day (0.9) unless overridden."""
    runtime = _runtime(hass)
    runtime.register_entity(
        "evsc_min_solar_forecast_threshold", "number.solar_threshold", None
    )
    hass.states.async_set("number.solar_threshold", threshold)
    for day in WEEKDAY_KEYS:
        hass.states.async_set(f"time.ready_{day}", ready)
    hass.states.async_set("sensor.status", "Preparing")
    hass.states.async_set("sensor.soc", soc)
    config = {
        CONF_PRICE_SENSOR: "sensor.prices",
        CONF_EV_CHARGER_STATUS: "sensor.status",
        CONF_SOC_CAR: "sensor.soc",
        CONF_BATTERY_CAPACITY: 50,
        CONF_PV_FORECAST: "sensor.fc_today",
        CONF_PV_FORECAST_TOMORROW: "sensor.fc_tomorrow",
    }
    if forecast is not None:
        hass.states.async_set("sensor.fc_today", str(forecast), {"unit_of_measurement": "kWh"})
        hass.states.async_set("sensor.fc_tomorrow", str(forecast), {"unit_of_measurement": "kWh"})
    night = PlannedNightCharge(
        hass, "entry", config, Mock(), _controller(), runtime_data=runtime
    )
    return night


def _capture(night):
    published = {}

    def _publish(key, value, attributes=None):
        if attributes is not None and "prices" in attributes:
            published["attrs"] = attributes

    night._publish = _publish
    return published


def _day_prices(cheap: dict[int, float]) -> dict[int, float]:
    prices = {h: 0.9 for h in range(24)}
    prices.update(cheap)
    return prices


async def test_cheapest_daytime_hours_are_planned(hass):
    base = _base()
    _set_prices(hass, base, _day_prices({12: 0.1, 13: 0.1}))
    night = await _night(hass, base, forecast=None)

    await night._evaluate(_at(base, 3, 0))
    assert night.get_plan().planned == [_at(base, 12), _at(base, 13)]


async def test_night_before_sunny_day_is_skipped(hass):
    base = _base()
    _set_prices(hass, base, _day_prices({3: 0.1, 4: 0.1, 12: 0.5, 13: 0.5}))
    night = await _night(hass, base, forecast=20)
    published = _capture(night)

    await night._evaluate(_at(base, 3, 0))
    assert night.get_plan().planned == [_at(base, 12), _at(base, 13)]
    attrs = published["attrs"]
    assert attrs["solar_skip"] is True
    assert attrs["solar_forecast_kwh"] == 20
    assert _at(base, 4).isoformat() not in attrs["plannable"]
    assert _at(base, 12).isoformat() in attrs["plannable"]
    assert attrs["reserve_used"] is False


async def test_cloudy_day_keeps_the_cheap_night(hass):
    base = _base()
    _set_prices(hass, base, _day_prices({3: 0.1, 4: 0.1, 12: 0.5, 13: 0.5}))
    night = await _night(hass, base, forecast=10)

    await night._evaluate(_at(base, 3, 0))
    assert night.get_plan().planned == [_at(base, 3), _at(base, 4)]


async def test_threshold_zero_turns_the_rule_off(hass):
    base = _base()
    _set_prices(hass, base, _day_prices({3: 0.1, 4: 0.1, 12: 0.5, 13: 0.5}))
    night = await _night(hass, base, forecast=40, threshold="0")

    await night._evaluate(_at(base, 3, 0))
    assert night.get_plan().planned == [_at(base, 3), _at(base, 4)]


async def test_rule_needs_sun_hours_before_the_deadline(hass):
    base = _base()
    _set_prices(hass, base, _day_prices({3: 0.1, 4: 0.1}))
    night = await _night(hass, base, forecast=40)
    rise, _ = night._sun_times(base.date())
    # Deadline one hour after sunrise: the sun cannot help, the night is used.
    ready = (rise + timedelta(hours=1)).strftime("%H:00:00")
    for day in WEEKDAY_KEYS:
        hass.states.async_set(f"time.ready_{day}", ready)

    await night._evaluate(_at(base, 3, 0))
    assert night.get_plan().planned == [_at(base, 3), _at(base, 4)]


async def test_skipped_night_is_a_reserve(hass):
    base = _base()
    _set_prices(hass, base, _day_prices({3: 0.1, 4: 0.1}))
    night = await _night(hass, base, forecast=40, soc="10")
    rise, _ = night._sun_times(base.date())
    ready_hour = (rise + timedelta(hours=3)).hour
    for day in WEEKDAY_KEYS:
        hass.states.async_set(f"time.ready_{day}", f"{ready_hour:02d}:00:00")
    published = _capture(night)

    await night._evaluate(_at(base, 3, 0))
    planned = night.get_plan().planned
    # 50 kWh * 70 % / 0.9 = 38.9 kWh at 11 kW -> 4 hours; fewer day hours exist.
    assert any(hour + timedelta(minutes=30) < rise for hour in planned)
    assert published["attrs"]["reserve_used"] is True


def test_build_plan_reserve_only_fills_the_gap():
    base = _base()
    hours = window_hours(_at(base, 0), _at(base, 6))
    prices = {h: float(i) for i, h in enumerate(hours)}
    plan = build_plan(
        window_start=_at(base, 0),
        deadline=_at(base, 6),
        prices=prices,
        cheapest_count=5,
        needed_kwh=33.0,  # 3 hours at 11 kW
        power_kw=11.0,
        allowed=set(hours[4:]),
        reserve=set(hours[:4]),
    )
    assert plan.planned == [hours[0], hours[4], hours[5]]
    assert plan.reserve_used is True
    assert plan.cheapest == [hours[4], hours[5]]
