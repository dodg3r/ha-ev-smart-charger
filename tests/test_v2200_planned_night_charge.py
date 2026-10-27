"""v2.20.0: Night Smart Charge plans the cheapest hours before car-ready time."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

from homeassistant.util import dt as dt_util

from custom_components.ev_smart_charger.charge_planner import (
    build_plan,
    energy_needed_kwh,
    hours_needed,
    pick_cheapest,
    window_hours,
)
from custom_components.ev_smart_charger.charger_controller import OperationResult
from custom_components.ev_smart_charger.const import (
    CONF_BATTERY_CAPACITY,
    CONF_EV_CHARGER_STATUS,
    CONF_PRICE_SENSOR,
    CONF_SOC_CAR,
    WEEKDAY_KEYS,
)
from custom_components.ev_smart_charger.planned_night_charge import (
    PlannedNightCharge,
)
from custom_components.ev_smart_charger.price_source import (
    parse_core_nordpool_response,
    parse_price_list,
    prices_from_attributes,
)
from custom_components.ev_smart_charger.runtime import EVSCRuntimeData

TZ = dt_util.DEFAULT_TIME_ZONE


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def _base() -> datetime:
    return datetime(2026, 11, 3, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)


def test_window_hours_includes_partial_first_hour():
    base = _base()
    hours = window_hours(_at(base, 17, 20), _at(base, 20, 0))
    assert hours == [_at(base, 17), _at(base, 18), _at(base, 19)]


def test_pick_cheapest_orders_by_price_then_time():
    base = _base()
    hours = [_at(base, h) for h in range(6)]
    prices = {hours[0]: 3, hours[1]: 1, hours[2]: 1, hours[3]: 2}
    assert pick_cheapest(hours, prices, 3) == [hours[1], hours[2], hours[3]]
    # Unknown prices are used last.
    assert pick_cheapest(hours, prices, 5)[-1] == hours[4]


def test_energy_and_hours_needed():
    kwh = energy_needed_kwh(60, 80, 50, 0.9)
    assert round(kwh, 2) == 11.11
    assert hours_needed(kwh, 11.04) == 2
    assert hours_needed(0.0, 3.68) == 0
    assert energy_needed_kwh(85, 80, 50, 0.9) == 0.0
    assert energy_needed_kwh(None, 80, 50, 0.9) is None


def test_build_plan_keeps_committed_hour_and_skips_past_hours():
    base = _base()
    hours = [_at(base, h) for h in range(1, 7)]
    prices = dict(zip(hours, [1.0, 0.5, 0.2, 0.3, 0.9, 0.8]))
    plan = build_plan(
        window_start=_at(base, 1),
        deadline=_at(base, 7),
        prices=prices,
        cheapest_count=5,
        needed_kwh=3.0,
        power_kw=3.68,
        committed=[_at(base, 2)],
        earliest=_at(base, 2, 30),
    )
    assert plan.cheapest == [_at(base, h) for h in (2, 3, 4, 5, 6)]
    # One hour needed, but the hour already charging is kept.
    assert plan.planned == [_at(base, 2)]

    plan = build_plan(
        window_start=_at(base, 1),
        deadline=_at(base, 7),
        prices=prices,
        cheapest_count=5,
        needed_kwh=3.0,
        power_kw=3.68,
        earliest=_at(base, 3, 30),
    )
    assert plan.planned == [_at(base, 3)]


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------


def test_raw_today_quarter_hours_are_averaged():
    base = _base()
    raw = [
        {"start": (_at(base, 1) + timedelta(minutes=15 * i)).isoformat(), "value": v}
        for i, v in enumerate([1.0, 2.0, 3.0, 4.0])
    ]
    prices = prices_from_attributes(
        {"raw_today": raw, "raw_tomorrow": [], "tomorrow_valid": False}, base.date()
    )
    assert prices == {_at(base, 1): 2.5}


def test_plain_hourly_list():
    base = _base()
    points = parse_price_list([float(i) for i in range(24)], base)
    assert points[5] == (_at(base, 5), 5.0)


def test_core_nordpool_response_is_per_mwh():
    base = _base()
    response = {
        "SE3": [{"start": _at(base, 2).isoformat(), "end": _at(base, 3).isoformat(), "price": 420.0}],
        "SE4": [{"start": _at(base, 2).isoformat(), "end": _at(base, 3).isoformat(), "price": 999.0}],
    }
    points = parse_core_nordpool_response(response, "sensor.nord_pool_se3_current_price")
    assert points == [(_at(base, 2), 0.42)]


# ---------------------------------------------------------------------------
# Component against real HA states
# ---------------------------------------------------------------------------


def _runtime(hass) -> EVSCRuntimeData:
    runtime = EVSCRuntimeData(config={}, expected_entity_count=0)
    runtime.register_entity("evsc_night_smart_charge_enabled", "switch.night_enabled", None)
    runtime.register_entity("evsc_night_charge_amperage", "number.night_amps", None)
    hass.states.async_set("switch.night_enabled", "on")
    hass.states.async_set("number.night_amps", "16")
    for day in WEEKDAY_KEYS:
        runtime.register_entity(f"evsc_car_ready_{day}", f"switch.ready_{day}", None)
        runtime.register_entity(f"evsc_car_ready_time_{day}", f"time.ready_{day}", None)
        runtime.register_entity(f"evsc_ev_min_soc_{day}", f"number.target_{day}", None)
        hass.states.async_set(f"switch.ready_{day}", "on")
        hass.states.async_set(f"time.ready_{day}", "07:00:00")
        hass.states.async_set(f"number.target_{day}", "80")
    return runtime


def _controller():
    controller = Mock()
    controller.start_charger = AsyncMock(
        return_value=OperationResult(success=True, operation="start", reason="t")
    )
    controller.stop_charger = AsyncMock(
        return_value=OperationResult(success=True, operation="stop", reason="t")
    )
    return controller


async def _component(hass, base):
    hours = [_at(base, h) for h in range(1, 7)]
    raw = [
        {"start": hour.isoformat(), "end": (hour + timedelta(hours=1)).isoformat(), "value": v}
        for hour, v in zip(hours, [1.0, 0.5, 0.2, 0.3, 0.9, 0.8])
    ]
    hass.states.async_set(
        "sensor.prices",
        "0.5",
        {"raw_today": raw, "raw_tomorrow": [], "unit_of_measurement": "SEK/kWh"},
    )
    hass.states.async_set("sensor.status", "Preparing")
    hass.states.async_set("sensor.soc", "60")
    config = {
        CONF_PRICE_SENSOR: "sensor.prices",
        CONF_EV_CHARGER_STATUS: "sensor.status",
        CONF_SOC_CAR: "sensor.soc",
        # 20 kWh * 20 % / 0.9 = 4.4 kWh at 3.68 kW -> 2 hours
        CONF_BATTERY_CAPACITY: 20,
    }
    controller = _controller()
    night = PlannedNightCharge(
        hass, "entry", config, Mock(), controller, runtime_data=_runtime(hass)
    )
    return night, controller


async def test_charges_only_in_the_cheapest_needed_hours(hass):
    base = _base()
    night, controller = await _component(hass, base)

    await night._evaluate(_at(base, 1, 30))
    plan = night.get_plan()
    assert plan.planned == [_at(base, 3), _at(base, 4)]
    controller.start_charger.assert_not_awaited()

    await night._evaluate(_at(base, 3, 0))
    controller.start_charger.assert_awaited_once()
    assert controller.start_charger.await_args.args[0] == 16
    assert night.is_active()

    # SOC rises: one hour would now be enough, but the running hour is kept.
    hass.states.async_set("sensor.soc", "75")
    await night._evaluate(_at(base, 3, 40))
    controller.stop_charger.assert_not_awaited()

    await night._evaluate(_at(base, 5, 0))
    controller.stop_charger.assert_awaited_once()
    assert not night.is_active()


async def test_no_car_ready_day_shows_an_overview(hass):
    """v2.21.0: no car-ready day ahead -> all known hours, no charging."""
    base = _base()
    night, controller = await _component(hass, base)
    for day in WEEKDAY_KEYS:
        hass.states.async_set(f"switch.ready_{day}", "off")

    await night._evaluate(_at(base, 3, 0))
    controller.start_charger.assert_not_awaited()
    plan = night.get_plan()
    assert plan.planned == []
    assert plan.hours == [_at(base, h) for h in range(3, 7)]
    assert plan.cheapest == [_at(base, h) for h in range(3, 7)]


async def test_skips_days_without_car_ready(hass):
    """Only the day after tomorrow is ticked: plan towards that morning."""
    base = _base()
    night, controller = await _component(hass, base)
    keys = [WEEKDAY_KEYS[(base.weekday() + i) % 7] for i in range(7)]
    for key in keys:
        hass.states.async_set(f"switch.ready_{key}", "off")
    hass.states.async_set(f"switch.ready_{keys[2]}", "on")

    await night._evaluate(_at(base, 3, 0))
    plan = night.get_plan()
    assert plan.deadline == _at(base + timedelta(days=2), 7)
    # Prices only reach today 07:00 and it is long before the sunset before
    # the deadline: the plan waits for prices and nothing is charged.
    assert plan.planned == []
    controller.start_charger.assert_not_awaited()


async def test_missing_prices_plan_goes_live_at_sunset(hass):
    """Fallback: at the sunset before the deadline plan with known prices."""
    base = _base()
    night, controller = await _component(hass, base)
    keys = [WEEKDAY_KEYS[(base.weekday() + i) % 7] for i in range(7)]
    for key in keys:
        hass.states.async_set(f"switch.ready_{key}", "off")
    hass.states.async_set(f"switch.ready_{keys[1]}", "on")  # tomorrow 07:00

    await night._evaluate(_at(base, 15, 0))
    assert night.get_plan().planned == []  # waiting for tomorrow's prices

    await night._evaluate(_at(base, 22, 0))  # after sunset, still no prices
    assert night.get_plan().planned  # planned with unknown prices


async def test_night_hours_exclude_daytime(hass):
    base = _base()
    night = PlannedNightCharge(hass, "entry", {}, Mock(), _controller())
    hours = [_at(base, h) for h in (2, 12, 23)]
    allowed = night._night_hours(hours, _at(base + timedelta(days=1), 7))
    assert allowed == {_at(base, 2), _at(base, 23)}
    # On the deadline day the morning up to the deadline may be used.
    allowed = night._night_hours([_at(base, 12)], _at(base, 16))
    assert allowed == {_at(base, 12)}


async def test_disconnected_car_is_not_planned(hass):
    base = _base()
    night, controller = await _component(hass, base)
    hass.states.async_set("sensor.status", "Available")

    await night._evaluate(_at(base, 3, 0))
    controller.start_charger.assert_not_awaited()
    assert night.get_plan().planned == []


async def test_target_reached_stops_and_stays_stopped(hass):
    base = _base()
    night, controller = await _component(hass, base)
    await night._evaluate(_at(base, 3, 0))
    assert night.is_active()

    hass.states.async_set("sensor.soc", "80")
    await night._evaluate(_at(base, 3, 20))
    controller.stop_charger.assert_awaited_once()

    # A small SOC drop does not restart charging for the same deadline.
    hass.states.async_set("sensor.soc", "79")
    await night._evaluate(_at(base, 4, 0))
    controller.start_charger.assert_awaited_once()


async def test_raising_the_target_reopens_the_plan(hass):
    """v2.20.1: a latch set at a low target must not survive a higher target."""
    base = _base()
    night, controller = await _component(hass, base)
    day = WEEKDAY_KEYS[base.weekday()]
    hass.states.async_set(f"number.target_{day}", "50")

    await night._evaluate(_at(base, 1, 30))
    assert night.get_plan().planned == []  # 60 % >= 50 %: target reached

    hass.states.async_set(f"number.target_{day}", "80")
    await night._evaluate(_at(base, 1, 40))
    assert night.get_plan().planned == [_at(base, 3), _at(base, 4)]

    await night._evaluate(_at(base, 3, 0))
    controller.start_charger.assert_awaited_once()


# ---------------------------------------------------------------------------
# v2.21.1: unknown prices, price source, chart window
# ---------------------------------------------------------------------------


def test_cheapest_only_uses_priced_hours():
    base = _base()
    plan = build_plan(
        window_start=_at(base, 1),
        deadline=_at(base, 9),
        prices={_at(base, 2): 0.4, _at(base, 3): 0.1},
        cheapest_count=5,
        needed_kwh=None,
        power_kw=3.68,
    )
    assert plan.cheapest == [_at(base, 2), _at(base, 3)]


def _capture(night):
    published = {}

    def _publish(key, value, attributes=None):
        published["state"] = value
        published["attrs"] = attributes or {}

    night._publish = _publish
    return published


async def test_unsupported_price_sensor_is_reported(hass):
    base = _base()
    night, _controller_mock = await _component(hass, base)
    hass.states.async_set("sensor.prices", "0.5", {"unit_of_measurement": "SEK/kWh"})
    published = _capture(night)

    await night._evaluate(_at(base, 3, 0))
    assert published["attrs"]["price_source"] == "unsupported"
    assert published["attrs"]["cheapest"] == []
    assert all(p["price"] is None for p in published["attrs"]["prices"])


async def test_far_deadline_chart_is_capped(hass):
    """A deadline a week away does not publish a week of empty bars."""
    base = _base()
    night, _controller_mock = await _component(hass, base)
    keys = [WEEKDAY_KEYS[(base.weekday() + i) % 7] for i in range(7)]
    for key in keys:
        hass.states.async_set(f"switch.ready_{key}", "off")
    hass.states.async_set(f"switch.ready_{keys[6]}", "on")
    published = _capture(night)

    await night._evaluate(_at(base, 3, 0))
    attrs = published["attrs"]
    assert attrs["prices_truncated"] is True
    assert len(attrs["prices"]) == 12  # at least 12 hours, prices end at 07:00
    assert attrs["price_source"] == "attributes"


async def test_missing_prices_are_retried_every_minute(hass):
    """v2.23.0: empty prices are re-read after a minute, not ten."""
    base = _base()
    night, _controller_mock = await _component(hass, base)
    raw = hass.states.get("sensor.prices").attributes["raw_today"]
    hass.states.async_set("sensor.prices", "0.5", {"unit_of_measurement": "SEK/kWh"})
    await night._evaluate(_at(base, 3, 0))
    assert night.get_plan().prices == {}

    hass.states.async_set("sensor.prices", "0.5", {"raw_today": raw, "raw_tomorrow": []})
    night._prices_dirty = False  # no state-change event in this test
    await night._evaluate(_at(base, 3, 1, ) + timedelta(seconds=5))
    assert night.get_plan().prices
