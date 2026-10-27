"""Main-fuse protection math (pure Python, no Home Assistant imports).

This module answers one question: *how many amps may the EV charger draw per
phase right now without overloading the main fuse?* It is deliberately free of
Home Assistant imports so every rule can be unit-tested in isolation; the HA
glue (reading sensors, enforcing the result) lives in ``fuse_guard.py``.

Model, per phase ``n``::

    grid_n    = signed grid current (+ import, - export)          [A]
    charger_n = what the EV charger itself draws on that phase    [A]
    house_n   = grid_n - charger_n      (everything except the EV)
    room_n    = fuse - margin - house_n (what the charger may take)

    limit     = min(room_n over all measured phases)

Every uncertainty is resolved towards a *smaller* limit:

* An unsigned meter current is used as-is (import) when the direction is
  unknown. ``|import + x| - x >= real house load``, so the estimate of the
  house load is never too low.
* The charger contribution is the *lower* of the measured phase current and the
  commanded setpoint. Charger meter values often lag (OCPP MeterValues can be
  10-60 s old): right after a step down the stale measurement is still high,
  and subtracting it would hide house load. Taking the minimum keeps the house
  estimate conservative in both directions.
* A missing reading for a required sensor makes the whole calculation invalid;
  the limiter then falls back to the minimum current and finally to a stop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

NOMINAL_VOLTAGE = 230.0
# Voltage readings outside this window are treated as bogus (a meter reporting
# 0 V during a reconnect would otherwise blow the power→current conversion up).
MIN_PLAUSIBLE_VOLTAGE = 180.0
MAX_PLAUSIBLE_VOLTAGE = 260.0

SOURCE_CURRENT_WITH_DIRECTION = "current+direction"
SOURCE_CURRENT = "current"
SOURCE_POWER = "power"
SOURCE_MISSING = "missing"


@dataclass(frozen=True)
class PhaseReading:
    """Raw readings for one phase. ``None`` means "not available"."""

    grid_current_a: float | None = None
    import_w: float | None = None
    export_w: float | None = None
    voltage_v: float | None = None
    charger_current_a: float | None = None


@dataclass(frozen=True)
class PhaseResult:
    """Computed values for one phase."""

    phase: int
    valid: bool
    source: str
    grid_current_a: float | None
    charger_current_a: float
    house_current_a: float | None
    room_a: float | None


@dataclass(frozen=True)
class FuseLimit:
    """Instantaneous charger limit derived from one set of readings."""

    valid: bool
    limit_a: float | None
    bottleneck_phase: int | None
    phases: list[PhaseResult] = field(default_factory=list)
    reason: str = ""
    # v2.19.0: what the meter says right now, independent of the charger
    # estimate. ``overloaded``: some phase is above fuse − margin.
    # ``hard_trip``: some phase is above the hard limit (fuse + X).
    max_grid_a: float | None = None
    overloaded: bool = False
    hard_trip: bool = False


def _plausible_voltage(voltage: float | None) -> float:
    if voltage is None:
        return NOMINAL_VOLTAGE
    if MIN_PLAUSIBLE_VOLTAGE <= voltage <= MAX_PLAUSIBLE_VOLTAGE:
        return voltage
    return NOMINAL_VOLTAGE


def _export_equivalent(magnitude: float, reading: PhaseReading) -> float:
    """Signed equivalent of an exporting phase, safe against reactive current.

    The meter current is an RMS magnitude: it includes reactive current that
    the EV charger (unity power factor) does not cancel. Only the ACTIVE
    export current (export power / voltage) is credited as negative; the
    remaining reactive part still counts as load. By the triangle inequality
    ``|(Ia + Ic, Ir)| <= (Ia + Ic) + Ir``, so ``Ir - Ie`` never overstates the
    room. A pure export (power factor 1) still gives ``-magnitude``; a phase
    carrying ~3 A of reactive current with a few watts of export (seen on a
    real meter) is no longer read as 3 A of export.
    """
    net_export_w = (reading.export_w or 0.0) - (reading.import_w or 0.0)
    active_a = min(magnitude, max(0.0, net_export_w) / _plausible_voltage(
        reading.voltage_v
    ))
    reactive_a = math.sqrt(max(0.0, magnitude * magnitude - active_a * active_a))
    return reactive_a - active_a


def signed_grid_current(reading: PhaseReading) -> tuple[float | None, str]:
    """Return the signed grid current for a phase and how it was obtained.

    Preference order:
    1. Meter current (A) with direction from import/export power.
    2. Meter current (A) without direction -> treated as import (safe side).
    3. (import - export) power divided by the phase voltage.
    """
    if reading.grid_current_a is not None:
        magnitude = abs(reading.grid_current_a)
        if reading.import_w is not None and reading.export_w is not None:
            if reading.export_w > reading.import_w:
                return _export_equivalent(magnitude, reading), (
                    SOURCE_CURRENT_WITH_DIRECTION
                )
            return magnitude, SOURCE_CURRENT_WITH_DIRECTION
        return magnitude, SOURCE_CURRENT

    if reading.import_w is not None:
        net_w = reading.import_w - (reading.export_w or 0.0)
        return net_w / _plausible_voltage(reading.voltage_v), SOURCE_POWER

    return None, SOURCE_MISSING


def effective_charger_current(
    measured_a: float | None,
    commanded_a: float | None,
    charger_on: bool,
) -> float:
    """Return the charger current to subtract from the grid reading.

    Always the conservative (lower) value: a smaller charger contribution means
    a larger house-load estimate and therefore a smaller limit.
    """
    if not charger_on or measured_a is None:
        return 0.0
    measured = max(0.0, measured_a)
    if commanded_a is None:
        return measured
    return max(0.0, min(measured, float(commanded_a)))


def compute_fuse_limit(
    readings: list[PhaseReading],
    fuse_a: float,
    margin_a: float,
    *,
    commanded_a: float | None = None,
    charger_on: bool = False,
    hard_limit_a: float | None = None,
) -> FuseLimit:
    """Compute the per-phase charger limit against the main fuse.

    ``readings`` holds one entry per phase (1 or 3). A phase whose grid current
    cannot be determined invalidates the result: guessing a house load is
    exactly the failure the Node-RED flow had (a missing sensor read as 0 W).
    """
    if not readings:
        return FuseLimit(False, None, None, [], "no phases configured")
    if fuse_a <= 0:
        return FuseLimit(False, None, None, [], "main fuse size not set")

    margin = max(0.0, margin_a)
    results: list[PhaseResult] = []
    missing: list[int] = []

    for index, reading in enumerate(readings, start=1):
        grid_a, source = signed_grid_current(reading)
        charger_a = effective_charger_current(
            reading.charger_current_a, commanded_a, charger_on
        )
        if grid_a is None:
            missing.append(index)
            results.append(
                PhaseResult(index, False, source, None, charger_a, None, None)
            )
            continue
        house_a = grid_a - charger_a
        room_a = fuse_a - margin - house_a
        results.append(
            PhaseResult(index, True, source, grid_a, charger_a, house_a, room_a)
        )

    if missing:
        phases = ", ".join(f"L{n}" for n in missing)
        return FuseLimit(False, None, None, results, f"no reading for {phases}")

    bottleneck = min(results, key=lambda r: r.room_a)
    max_grid = max(r.grid_current_a for r in results)
    overloaded = max_grid > fuse_a - margin + 1e-6
    hard_trip = hard_limit_a is not None and max_grid > hard_limit_a + 1e-6
    reason = f"bottleneck L{bottleneck.phase}"
    if hard_trip:
        reason = f"L{max(results, key=lambda r: r.grid_current_a).phase} above hard limit"
    return FuseLimit(
        True,
        max(0.0, bottleneck.room_a),
        bottleneck.phase,
        results,
        reason,
        max_grid_a=max_grid,
        overloaded=overloaded,
        hard_trip=hard_trip,
    )


def highest_level_at_or_below(value: float, levels: list[int]) -> int:
    """Return the highest charger level <= value, or 0 when below the minimum.

    A tiny epsilon absorbs float noise (15.9999 A must still allow 16 A).
    """
    allowed = [level for level in levels if level <= value + 1e-6]
    return max(allowed) if allowed else 0


@dataclass
class LimiterDecision:
    """Output of :class:`FuseLimiter.update`."""

    allowed_a: int
    target_a: int
    state: str
    reason: str


class FuseLimiter:
    """Turns instantaneous limits into a safe, rate-limited charger ceiling.

    Behaviour (mirrors the proven Node-RED flow, plus a fail-safe):

    * **Decrease immediately when the meter shows an overload** (some phase
      above fuse − margin). While charging without an overload the ceiling is
      left alone (v2.19.0): no pre-emptive cap because the car *could* draw
      more than it does now.
    * **Hard limit** (v2.19.0): a phase above fuse + X stops the charger at
      once, whatever the charger estimate says.
    * **Increase slowly** while charging: at most one step (``ramp_step_a`` or
      one charger level, whichever is larger) every ``ramp_interval_s``.
      Charger and meter data lag, so a fast climb would overshoot.
    * **Below the minimum level -> stop**, and keep the ceiling at 0 for
      ``stop_hold_s`` so the charger is not restarted straight into the same
      overload.
    * **Invalid data** -> ceiling drops to the minimum level at once (never
      raised), and to 0 (stop) if the data stays invalid for
      ``invalid_stop_s``.

    All times are plain floats in seconds supplied by the caller, which keeps
    the class deterministic and testable.
    """

    STATE_OK = "ok"
    STATE_LIMITED = "limited"
    STATE_RAMPING = "ramping"
    STATE_STOPPED = "stopped"
    STATE_HOLD = "hold"
    STATE_INVALID = "invalid_data"
    STATE_INVALID_STOP = "invalid_data_stop"
    STATE_HARD_TRIP = "hard_trip"

    def __init__(
        self,
        levels: list[int],
        max_a: int | None = None,
        *,
        ramp_step_a: float = 2.0,
        ramp_interval_s: float = 20.0,
        stop_hold_s: float = 120.0,
        invalid_stop_s: float = 120.0,
        increase_delay_s: float | None = None,
    ) -> None:
        self._levels = sorted(levels)
        self._max_a = max_a if max_a is not None else self._levels[-1]
        self._ramp_step_a = ramp_step_a
        self._ramp_interval_s = ramp_interval_s
        self._stop_hold_s = stop_hold_s
        self._invalid_stop_s = invalid_stop_s

        self.allowed_a: int | None = None
        self._last_increase_at: float | None = None
        self._hold_until: float = 0.0
        self._invalid_since: float | None = None
        # v2.26.0: with ``increase_delay_s`` an increase waits until the room
        # has been there that long, then goes up in one step to the lowest
        # room seen in that time (instead of ramp_step_a per interval).
        self._increase_delay_s = increase_delay_s
        self._up_since: float | None = None
        self._up_min: int | None = None

    def _reset_increase(self) -> None:
        self._up_since = None
        self._up_min = None

    @property
    def min_level(self) -> int:
        return self._levels[0]

    def _next_step_up(self, current: int) -> int:
        above = [level for level in self._levels if level > current]
        next_level = above[0] if above else current
        by_amps = highest_level_at_or_below(current + self._ramp_step_a, self._levels)
        return max(next_level, by_amps)

    def update(
        self,
        limit: FuseLimit,
        now: float,
        charging: bool,
        *,
        allow_increase: bool = True,
        current_a: float | None = None,
    ) -> LimiterDecision:
        """Return the ceiling for the charger given the latest limit."""
        # ---- invalid data: fail safe -------------------------------------
        if not limit.valid:
            self._reset_increase()
            if self._invalid_since is None:
                self._invalid_since = now
            if now - self._invalid_since >= self._invalid_stop_s:
                self.allowed_a = 0
                return LimiterDecision(0, 0, self.STATE_INVALID_STOP, limit.reason)
            previous = self.allowed_a
            allowed = (
                self.min_level if previous is None else min(previous, self.min_level)
            )
            self.allowed_a = allowed
            return LimiterDecision(allowed, allowed, self.STATE_INVALID, limit.reason)

        self._invalid_since = None
        target = highest_level_at_or_below(
            min(limit.limit_a or 0.0, float(self._max_a)), self._levels
        )

        # ---- hard limit: a phase far above the fuse -> stop at once --------
        if limit.hard_trip:
            self._reset_increase()
            self._hold_until = now + self._stop_hold_s
            self.allowed_a = 0
            return LimiterDecision(0, 0, self.STATE_HARD_TRIP, limit.reason)

        # ---- v2.19.0: no pre-emptive cap -----------------------------------
        # While charging and no phase is above fuse − margin the meter says
        # everything is fine: keep the ceiling where it is instead of lowering
        # it (or stopping) because the car *could* draw more. A real overload
        # shows up on the meter within a second and is handled below.
        # ``current_a`` (the charger's setpoint) only seeds the first value, so
        # a lagging setpoint entity can never undo a reduction later.
        previous = self.allowed_a
        floor = previous if previous else None
        if floor is None and current_a:
            floor = highest_level_at_or_below(
                min(float(current_a), float(self._max_a)), self._levels
            ) or None
        if (
            charging
            and not limit.overloaded
            and floor is not None
            and target < floor
            and now >= self._hold_until
        ):
            self._reset_increase()
            self.allowed_a = floor
            state = self.STATE_LIMITED if floor < self._max_a else self.STATE_OK
            return LimiterDecision(floor, target, state, limit.reason)

        # ---- below minimum: stop and hold ---------------------------------
        if target == 0:
            self._reset_increase()
            self._hold_until = now + self._stop_hold_s
            self.allowed_a = 0
            return LimiterDecision(0, 0, self.STATE_STOPPED, limit.reason)

        if now < self._hold_until:
            self._reset_increase()
            self.allowed_a = 0
            remaining = int(self._hold_until - now)
            return LimiterDecision(
                0, target, self.STATE_HOLD, f"restart blocked for {remaining}s"
            )

        previous = self.allowed_a

        # ---- decrease, first value, or not charging: apply directly ------
        if previous is None or previous == 0 or target <= previous or not charging:
            self._reset_increase()
            self.allowed_a = target
            if previous is not None and target > previous:
                self._last_increase_at = now
            state = self.STATE_LIMITED if target < self._max_a else self.STATE_OK
            return LimiterDecision(target, target, state, limit.reason)

        # ---- increase while charging: ramp --------------------------------
        # v2.19.0: no increase while the charger is still settling after a
        # reduction (the estimate is deliberately generous then).
        if not allow_increase:
            self._reset_increase()
            state = self.STATE_LIMITED if previous < self._max_a else self.STATE_OK
            return LimiterDecision(previous, target, state, limit.reason)
        if self._increase_delay_s is not None:
            if self._up_since is None:
                self._up_since = now
                self._up_min = target
            else:
                self._up_min = min(self._up_min or target, target)
            if now - self._up_since < self._increase_delay_s:
                return LimiterDecision(previous, target, self.STATE_RAMPING, limit.reason)
            allowed = max(previous, self._up_min or target)
            self._reset_increase()
            self.allowed_a = allowed
            self._last_increase_at = now
            state = self.STATE_LIMITED if allowed < self._max_a else self.STATE_OK
            return LimiterDecision(allowed, target, state, limit.reason)
        if (
            self._last_increase_at is not None
            and now - self._last_increase_at < self._ramp_interval_s
        ):
            return LimiterDecision(previous, target, self.STATE_RAMPING, limit.reason)

        allowed = min(target, self._next_step_up(previous))
        self.allowed_a = allowed
        self._last_increase_at = now
        state = self.STATE_RAMPING if allowed < target else (
            self.STATE_LIMITED if target < self._max_a else self.STATE_OK
        )
        return LimiterDecision(allowed, target, state, limit.reason)
