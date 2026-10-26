"""Electricity price reader for planned night charging (v2.20.0).

Reads hourly prices for today and tomorrow from a mapped price sensor.

Supported sources:
- Nord Pool (HACS) and similar sensors with ``raw_today`` / ``raw_tomorrow``
  attributes: lists of ``{start, end, value}`` (or ``hour`` / ``price``).
- Sensors with plain ``today`` / ``tomorrow`` attributes: lists of 24 (hourly)
  or 96 (15-minute) numbers starting at local midnight.
- The core Nord Pool integration: prices are fetched with the
  ``nordpool.get_prices_for_date`` service (currency per MWh, converted to
  currency per kWh).

15-minute prices are averaged into hourly prices. The parsing helpers are
pure so they can be tested without Home Assistant.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any, Iterable

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

_START_KEYS = ("start", "hour", "startsAt", "start_time", "time")
_VALUE_KEYS = ("value", "price", "total", "price_per_kwh")


def _to_datetime(value: Any) -> datetime | None:
    """Parse a datetime or an ISO string; naive values are local time."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return parsed


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def hour_start(moment: datetime) -> datetime:
    """Return the start of the local hour containing ``moment``."""
    local = dt_util.as_local(moment)
    return local.replace(minute=0, second=0, microsecond=0)


def aggregate_hourly(points: Iterable[tuple[datetime, float]]) -> dict[datetime, float]:
    """Average price points (any resolution) into local hourly prices."""
    buckets: dict[datetime, list[float]] = {}
    for start, price in points:
        buckets.setdefault(hour_start(start), []).append(price)
    return {
        start: round(sum(values) / len(values), 5)
        for start, values in buckets.items()
        if values
    }


def parse_price_entries(entries: Any, *, scale: float = 1.0) -> list[tuple[datetime, float]]:
    """Parse a list of price dicts into ``(start, price)`` points."""
    points: list[tuple[datetime, float]] = []
    if not isinstance(entries, (list, tuple)):
        return points
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        start = None
        for key in _START_KEYS:
            if key in entry:
                start = _to_datetime(entry[key])
                break
        price = None
        for key in _VALUE_KEYS:
            if key in entry:
                price = _to_float(entry[key])
                break
        if start is None or price is None:
            continue
        points.append((start, price * scale))
    return points


def parse_price_list(values: Any, day_start: datetime) -> list[tuple[datetime, float]]:
    """Parse a plain list of numbers starting at local midnight of a day."""
    if not isinstance(values, (list, tuple)) or not values:
        return []
    count = len(values)
    if count >= 92:  # 15-minute resolution (92-100 around DST changes)
        step = timedelta(minutes=15)
    elif count >= 23:
        step = timedelta(hours=1)
    else:
        return []
    points = []
    for index, value in enumerate(values):
        price = _to_float(value)
        if price is None:
            continue
        points.append((day_start + step * index, price))
    return points


def prices_from_attributes(
    attributes: dict[str, Any], today: date
) -> dict[datetime, float]:
    """Return hourly prices for today and tomorrow from sensor attributes."""
    points: list[tuple[datetime, float]] = []
    tomorrow_valid = attributes.get("tomorrow_valid")
    for raw_key, plain_key, day in (
        ("raw_today", "today", today),
        ("raw_tomorrow", "tomorrow", today + timedelta(days=1)),
    ):
        if raw_key == "raw_tomorrow" and tomorrow_valid is False:
            continue
        if raw_key in attributes:
            points.extend(parse_price_entries(attributes.get(raw_key)))
            continue
        day_start = datetime.combine(day, datetime.min.time()).replace(
            tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
        points.extend(parse_price_list(attributes.get(plain_key), day_start))
    return aggregate_hourly(points)


def has_attribute_prices(attributes: dict[str, Any]) -> bool:
    """Return True when the sensor carries price lists in its attributes."""
    return any(key in attributes for key in ("raw_today", "today"))


def parse_core_nordpool_response(
    response: Any, entity_id: str | None = None
) -> list[tuple[datetime, float]]:
    """Parse a core Nord Pool service response (currency per MWh)."""
    if not isinstance(response, dict) or not response:
        return []
    areas = [key for key, value in response.items() if isinstance(value, list)]
    if not areas:
        return []
    area = areas[0]
    if entity_id and len(areas) > 1:
        lowered = entity_id.lower()
        for candidate in areas:
            if candidate.lower() in lowered:
                area = candidate
                break
    return parse_price_entries(response[area], scale=0.001)


async def _async_core_nordpool_prices(
    hass: HomeAssistant, entity_id: str, today: date
) -> dict[datetime, float]:
    """Fetch today's and tomorrow's prices from the core Nord Pool service."""
    if not hass.services.has_service("nordpool", "get_prices_for_date"):
        return {}
    try:
        from homeassistant.helpers import entity_registry as er

        registry_entry = er.async_get(hass).async_get(entity_id)
    except Exception:  # noqa: BLE001 - registry lookup is best effort
        registry_entry = None
    if registry_entry is None or not registry_entry.config_entry_id:
        return {}

    points: list[tuple[datetime, float]] = []
    for day in (today, today + timedelta(days=1)):
        try:
            response = await hass.services.async_call(
                "nordpool",
                "get_prices_for_date",
                {
                    "config_entry": registry_entry.config_entry_id,
                    "date": day.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - tomorrow is often not released yet
            _LOGGER.debug("Nord Pool prices for %s not available: %s", day, err)
            continue
        points.extend(parse_core_nordpool_response(response, entity_id))
    return aggregate_hourly(points)


async def async_read_hourly_prices(
    hass: HomeAssistant, entity_id: str | None
) -> dict[datetime, float]:
    """Return hourly prices (local hour start -> price per kWh)."""
    if not entity_id:
        return {}
    state = hass.states.get(entity_id)
    today = dt_util.now().date()
    if state is not None and has_attribute_prices(state.attributes):
        return prices_from_attributes(dict(state.attributes), today)
    return await _async_core_nordpool_prices(hass, entity_id, today)
