"""v2.55.0: total electricity price = (price + addition) × (1 + VAT)."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.util import dt as dt_util

from custom_components.ev_smart_charger.const import (
    CONF_PRICE_SENSOR,
    HELPER_PRICE_ADDITION_SUFFIX,
    HELPER_PRICE_VAT_SUFFIX,
)
from custom_components.ev_smart_charger.planned_night_charge import (
    PlannedNightCharge,
)

IDS = {
    HELPER_PRICE_ADDITION_SUFFIX: "number.price_addition",
    HELPER_PRICE_VAT_SUFFIX: "number.price_vat",
}


def _plan(hass) -> PlannedNightCharge:
    runtime = SimpleNamespace(
        power_model=None, get_entity_id=IDS.get, get_entity=lambda _key: None
    )
    return PlannedNightCharge(
        hass, "entry", {CONF_PRICE_SENSOR: "sensor.nordpool"}, None, None, runtime
    )


def _hour(h: int) -> datetime:
    return datetime(2026, 11, 23, h, tzinfo=dt_util.DEFAULT_TIME_ZONE)


async def test_total_price_matches_the_supplier_formula(hass):
    plan = _plan(hass)
    hass.states.async_set("number.price_addition", "0.48")
    hass.states.async_set("number.price_vat", "25")

    totals = plan._total_prices({_hour(19): 1.89973})

    # (1.89973 + 0.48) × 1.25 = 2.97466, the supplier sensor's own price.
    assert totals[_hour(19)] == 2.97466


async def test_zero_settings_keep_the_sensor_price(hass):
    plan = _plan(hass)
    hass.states.async_set("number.price_addition", "0")
    hass.states.async_set("number.price_vat", "0")
    raw = {_hour(1): 1.2345}

    assert plan._total_prices(raw) == raw


async def test_missing_settings_keep_the_sensor_price(hass):
    plan = _plan(hass)
    assert plan._total_prices({_hour(1): 1.0}) == {_hour(1): 1.0}


async def test_refresh_applies_the_settings_without_refetching(hass):
    plan = _plan(hass)
    hass.states.async_set("sensor.nordpool", "1.0")
    hass.states.async_set("number.price_addition", "0")
    hass.states.async_set("number.price_vat", "0")
    now = _hour(12)
    read = AsyncMock(return_value=({_hour(13): 1.0}, "attributes"))

    with patch(
        "custom_components.ev_smart_charger.planned_night_charge.async_read_prices",
        read,
    ):
        await plan._refresh_prices(now)
        assert plan._prices == {_hour(13): 1.0}
        hass.states.async_set("number.price_vat", "25")
        await plan._refresh_prices(now)

    assert plan._prices == {_hour(13): 1.25}
    assert read.await_count == 1
