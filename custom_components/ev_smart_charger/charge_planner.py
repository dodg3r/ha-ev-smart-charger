"""Pure planning helpers for planned night charging (v2.20.0).

No Home Assistant imports, so the logic can be tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math


@dataclass
class NightPlan:
    """Result of one planning run."""

    window_start: datetime
    deadline: datetime
    hours: list[datetime]                     # every hour start in the window
    prices: dict[datetime, float]             # known prices in the window
    cheapest: list[datetime] = field(default_factory=list)
    planned: list[datetime] = field(default_factory=list)
    needed_kwh: float | None = None
    needed_hours: int | None = None


def window_hours(start: datetime, end: datetime) -> list[datetime]:
    """Return the hour starts of all hours that overlap [start, end)."""
    if end <= start:
        return []
    first = start.replace(minute=0, second=0, microsecond=0)
    hours = []
    current = first
    while current < end:
        hours.append(current)
        current = current + timedelta(hours=1)
    return hours


def pick_cheapest(
    hours: list[datetime],
    prices: dict[datetime, float],
    count: int,
    *,
    required: list[datetime] | None = None,
) -> list[datetime]:
    """Pick ``count`` hours with the lowest price, returned in time order.

    Hours with an unknown price are only used when there are not enough
    priced hours. Ties are broken by the earlier hour. ``required`` hours
    (e.g. the hour that is already charging) are always included and count
    towards ``count``.
    """
    if count <= 0 and not required:
        return []
    chosen = [hour for hour in (required or []) if hour in hours]
    remaining = max(0, count - len(chosen))
    candidates = [hour for hour in hours if hour not in chosen]
    candidates.sort(
        key=lambda hour: (
            hour not in prices,
            prices.get(hour, 0.0),
            hour,
        )
    )
    chosen.extend(candidates[:remaining])
    return sorted(chosen)


def energy_needed_kwh(
    soc: float | None,
    target: float,
    capacity_kwh: float,
    efficiency: float,
) -> float | None:
    """Return the energy taken from the grid to reach the target."""
    if soc is None:
        return None
    missing = max(0.0, float(target) - float(soc))
    if missing <= 0:
        return 0.0
    return missing / 100.0 * capacity_kwh / max(efficiency, 0.1)


def hours_needed(kwh: float | None, power_kw: float) -> int | None:
    """Return the number of whole hours needed to charge ``kwh``."""
    if kwh is None:
        return None
    if kwh <= 0:
        return 0
    if power_kw <= 0:
        return None
    return math.ceil(round(kwh / power_kw, 6))


def build_plan(
    *,
    window_start: datetime,
    deadline: datetime,
    prices: dict[datetime, float],
    cheapest_count: int,
    needed_kwh: float | None,
    power_kw: float,
    committed: list[datetime] | None = None,
    earliest: datetime | None = None,
    allowed: set[datetime] | None = None,
) -> NightPlan:
    """Build the plan for one night window.

    ``cheapest`` is always the ``cheapest_count`` cheapest hours in the
    window. ``planned`` is filled only when ``needed_kwh`` is known: the
    cheapest hours that cover the need. ``committed`` hours (already started)
    are kept in the plan. Only hours that end after ``earliest`` (usually
    now) can be planned. When ``allowed`` is given, only those hours are
    candidates for both the cheapest hours and the plan (v2.21.0: night
    hours only, so the plan never competes with free solar).
    """
    hours = window_hours(window_start, deadline)
    window_prices = {hour: prices[hour] for hour in hours if hour in prices}
    plan = NightPlan(
        window_start=window_start,
        deadline=deadline,
        hours=hours,
        prices=window_prices,
    )
    candidates = [hour for hour in hours if allowed is None or hour in allowed]
    plan.cheapest = pick_cheapest(candidates, window_prices, cheapest_count)
    plan.needed_kwh = None if needed_kwh is None else round(needed_kwh, 2)
    plan.needed_hours = hours_needed(needed_kwh, power_kw)
    if plan.needed_hours is not None:
        open_hours = [
            hour
            for hour in candidates
            if earliest is None or hour + timedelta(hours=1) > earliest
        ]
        plan.planned = pick_cheapest(
            open_hours,
            window_prices,
            min(plan.needed_hours, len(open_hours)),
            required=committed if plan.needed_hours > 0 else None,
        )
    return plan
