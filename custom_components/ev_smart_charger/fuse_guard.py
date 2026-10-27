"""Main-fuse protection for EV Smart Charger (v2.14.0).

Keeps the EV charger from overloading the house main fuse, on every phase.

Two roles:

1. **Ceiling for every charger command.** ``ChargerController`` asks
   :meth:`FuseGuard.max_allowed_amps` before starting the charger or changing
   its current, and clamps the request. No automation — Solar Surplus, Night
   Smart Charge, Boost, Force Charge — can ask for more than the fuse allows.
2. **Watchdog.** The guard re-evaluates on every change of a meter or charger
   sensor (and every few seconds as a backstop). When the charger is running
   above the ceiling it lowers the current at once, bypassing the controller's
   normal rate limit, and stops the charger when not even the minimum current
   fits. Charging started outside the integration is covered too.

The arithmetic lives in ``fuse_math.py`` (pure, unit-tested). This module only
reads Home Assistant state, feeds the math and acts on the result. It is a
no-op unless a main fuse size is configured.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import time
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    CONF_EV_CHARGER_CURRENT,
    CONF_CHARGING_POWER,
    CONF_CHARGING_POWER_L2,
    CONF_CHARGING_POWER_L3,
    CONF_EV_CHARGER_STATUS,
    CONF_EV_CHARGER_SWITCH,
    DEFAULT_FUSE_HARD_TRIP,
    DEFAULT_FUSE_MARGIN,
    FUSE_CHARGER_DRAWING_A,
    FUSE_ENTITY_KEYS,
    FUSE_GRID_CURRENT_KEYS,
    FUSE_GRID_EXPORT_KEYS,
    FUSE_GRID_IMPORT_KEYS,
    FUSE_GRID_VOLTAGE_KEYS,
    FUSE_GUARD_INTERVAL_SECONDS,
    FUSE_INVALID_STOP_SECONDS,
    FUSE_MEASURED_OVERDRAW_TOLERANCE_A,
    FUSE_INCREASE_DELAY_SECONDS,
    FUSE_RAMP_INTERVAL_SECONDS,
    FUSE_RAMP_STEP_A,
    FUSE_SETTLE_SECONDS,
    FUSE_STOP_HOLD_SECONDS,
    HELPER_FUSE_HARD_TRIP_SUFFIX,
    HELPER_FUSE_HEADROOM_SENSOR_SUFFIX,
    HELPER_FUSE_MARGIN_SUFFIX,
    get_amp_levels,
    get_main_fuse_size,
    get_phase_count,
    uses_charger_buttons,
)
from .fuse_math import (
    FuseLimit,
    FuseLimiter,
    LimiterDecision,
    PhaseReading,
    compute_fuse_limit,
)
from .power_model import (
    read_charger_phase_currents,
    read_current_a,
    read_number,
    read_power_w,
)
from .utils.logging_helper import EVSCLogger

class FuseGuard:
    """Main-fuse ceiling + watchdog for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict,
        runtime_data: Any,
        charger_controller: Any,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.config = config
        self._runtime_data = runtime_data
        self._controller = charger_controller
        self.logger = EVSCLogger("MAIN FUSE GUARD")

        self.fuse_a = get_main_fuse_size(config)
        self.enabled = self.fuse_a > 0
        self.phase_count = get_phase_count(config)
        self._levels = get_amp_levels(config)
        self._limiter = FuseLimiter(
            self._levels,
            max_a=self._levels[-1],
            ramp_step_a=FUSE_RAMP_STEP_A,
            ramp_interval_s=FUSE_RAMP_INTERVAL_SECONDS,
            stop_hold_s=FUSE_STOP_HOLD_SECONDS,
            increase_delay_s=FUSE_INCREASE_DELAY_SECONDS,
            invalid_stop_s=FUSE_INVALID_STOP_SECONDS,
        )

        self._switch_entity = config.get(CONF_EV_CHARGER_SWITCH)
        self._current_entity = config.get(CONF_EV_CHARGER_CURRENT)

        self._last_limit: FuseLimit | None = None
        self._last_decision: LimiterDecision | None = None
        self._last_state: str | None = None
        self._last_published: tuple | None = None
        self._enforce_task: asyncio.Task | None = None
        self._unsubs: list = []
        # v2.19.0: settle window after a reduction (see FUSE_SETTLE_SECONDS).
        self._settle_until: float = 0.0
        self._settle_commanded: float | None = None
        self._settle_ceiling: int | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def async_setup(self) -> None:
        """Start listening. No-op when no main fuse size is configured."""
        if not self.enabled:
            return

        tracked = self._tracked_entities()
        if tracked:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, tracked, self._async_state_changed
                )
            )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_interval,
                timedelta(seconds=FUSE_GUARD_INTERVAL_SECONDS),
            )
        )
        self.logger.info(
            "Main fuse protection active: %sA fuse, %s phase(s), %s tracked entities",
            self.fuse_a,
            self.phase_count,
            len(tracked),
        )
        self._evaluate(enforce=True)

    async def async_remove(self) -> None:
        """Stop listening and cancel any running enforcement."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._enforce_task is not None and not self._enforce_task.done():
            self._enforce_task.cancel()
        self._enforce_task = None

    # ------------------------------------------------------------------
    # public API (used by ChargerController)
    # ------------------------------------------------------------------
    def max_allowed_amps(self) -> int | None:
        """Return the current ceiling in A (0 = must not charge).

        ``None`` means protection is not configured. Re-evaluates from live
        state so a command never relies on a stale ceiling.
        """
        if not self.enabled:
            return None
        decision = self._evaluate(enforce=False)
        return decision.allowed_a

    @property
    def last_decision(self) -> LimiterDecision | None:
        return self._last_decision

    @property
    def last_limit(self) -> FuseLimit | None:
        return self._last_limit

    # ------------------------------------------------------------------
    # event handlers
    # ------------------------------------------------------------------
    @callback
    def _async_state_changed(self, _event: Event) -> None:
        self._evaluate(enforce=True)

    @callback
    def _async_interval(self, _now=None) -> None:
        self._evaluate(enforce=True)

    # ------------------------------------------------------------------
    # reading Home Assistant state
    # ------------------------------------------------------------------
    def _tracked_entities(self) -> list[str]:
        entities = [self.config.get(key) for key in FUSE_ENTITY_KEYS]
        entities += [self._switch_entity, self._current_entity]
        if uses_charger_buttons(self.config):
            entities += [
                self.config.get(key)
                for key in (
                    CONF_CHARGING_POWER,
                    CONF_CHARGING_POWER_L2,
                    CONF_CHARGING_POWER_L3,
                    CONF_EV_CHARGER_STATUS,
                )
            ]
        return sorted({e for e in entities if e})

    def _read_entity(self, entity_id: str | None) -> float | None:
        return read_number(self.hass, entity_id)

    def _read_power_w(self, entity_id: str | None) -> float | None:
        return read_power_w(self.hass, entity_id)

    def _read_current_a(self, entity_id: str | None) -> float | None:
        return read_current_a(self.hass, entity_id)

    def _read_charger_phase_currents(self) -> list[float | None]:
        """Per-phase charger current, from dedicated sensors or attributes."""
        return read_charger_phase_currents(self.hass, self.config, self.phase_count)

    def _build_readings(self) -> list[PhaseReading]:
        charger = self._read_charger_phase_currents()
        readings: list[PhaseReading] = []
        for index in range(self.phase_count):
            readings.append(
                PhaseReading(
                    grid_current_a=self._read_current_a(
                        self.config.get(FUSE_GRID_CURRENT_KEYS[index])
                    ),
                    import_w=self._read_power_w(
                        self.config.get(FUSE_GRID_IMPORT_KEYS[index])
                    ),
                    export_w=self._read_power_w(
                        self.config.get(FUSE_GRID_EXPORT_KEYS[index])
                    ),
                    voltage_v=self._read_entity(
                        self.config.get(FUSE_GRID_VOLTAGE_KEYS[index])
                    ),
                    charger_current_a=charger[index],
                )
            )
        return readings

    def _margin_a(self) -> float:
        entity_id = None
        if self._runtime_data is not None:
            entity_id = self._runtime_data.get_entity_id(HELPER_FUSE_MARGIN_SUFFIX)
        value = self._read_entity(entity_id)
        return DEFAULT_FUSE_MARGIN if value is None else max(0.0, value)

    def _hard_limit_a(self) -> float:
        entity_id = None
        if self._runtime_data is not None:
            entity_id = self._runtime_data.get_entity_id(HELPER_FUSE_HARD_TRIP_SUFFIX)
        value = self._read_entity(entity_id)
        extra = DEFAULT_FUSE_HARD_TRIP if value is None else max(0.0, value)
        return self.fuse_a + extra

    def _charger_on(self) -> bool:
        if not self._switch_entity:
            # Start/stop button chargers (Growatt THOR): no switch to read,
            # ask the controller (commanded state, then measured draw).
            check = getattr(self._controller, "button_charger_on", None)
            return bool(check()) if callable(check) and uses_charger_buttons(self.config) else False
        state = self.hass.states.get(self._switch_entity)
        return state is not None and state.state == "on"

    def _setpoint_a(self) -> float | None:
        return self._read_entity(self._current_entity)

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------
    def _evaluate(self, *, enforce: bool) -> LimiterDecision:
        readings = self._build_readings()
        # v2.25.2: a charger phase current above ~1 A means the charger draws
        # and the meter includes that draw, whatever the on/off state says.
        # A one-update 'off' (button charger power sensor dropping out) used
        # to count the charger's own current as house load -> stop + hold.
        charger_on = self._charger_on() or any(
            r.charger_current_a is not None and r.charger_current_a > FUSE_CHARGER_DRAWING_A
            for r in readings
        )
        setpoint = self._setpoint_a()
        margin = self._margin_a()
        now = time.monotonic()

        # v2.19.0: right after a reduction the car still draws the old current
        # for a few seconds while the meter already reflects it. Count the
        # charger at its previous setpoint until it has had time to follow, so
        # the same overload does not trigger a second reduction or a stop.
        settling = charger_on and now < self._settle_until
        commanded = setpoint
        if settling and self._settle_commanded is not None:
            commanded = self._settle_commanded
        elif not settling:
            self._clear_settle()

        limit = compute_fuse_limit(
            readings,
            self.fuse_a,
            margin,
            commanded_a=commanded,
            charger_on=charger_on,
            hard_limit_a=self._hard_limit_a(),
        )
        decision = self._limiter.update(
            limit,
            now,
            charging=charger_on,
            allow_increase=not settling,
            current_a=setpoint,
        )
        self._last_limit = limit
        self._last_decision = decision

        self._log_transition(decision, limit)
        self._publish(decision, limit, margin, readings)

        if enforce:
            if charger_on and self._needs_enforcement(decision, setpoint, readings):
                if self._settled_enough(decision, settling):
                    self._start_enforcement(decision, setpoint, now)
            elif decision.allowed_a > 0 and self._restore_pending():
                self._start_restore(decision)
        return decision

    def _clear_settle(self) -> None:
        self._settle_until = 0.0
        self._settle_commanded = None
        self._settle_ceiling = None

    def _settled_enough(self, decision: LimiterDecision, settling: bool) -> bool:
        """During the settle window only act on a lower ceiling or a stop.

        The setpoint entity of some chargers (OCPP) lags behind the command,
        so it can still read the old value; that alone must not repeat the
        same reduction.
        """
        if not settling or decision.allowed_a == 0 or self._settle_ceiling is None:
            return True
        return decision.allowed_a < self._settle_ceiling

    def _restore_pending(self) -> bool:
        check = getattr(self._controller, "fuse_restore_pending", None)
        return bool(check()) if callable(check) else False

    def _start_restore(self, decision: LimiterDecision) -> None:
        if self._enforce_task is not None and not self._enforce_task.done():
            return
        reason = f"Main fuse protection: room again (ceiling {decision.allowed_a}A)"
        self._enforce_task = self.hass.async_create_task(
            self._async_restore(decision.allowed_a, reason)
        )

    async def _async_restore(self, ceiling: int, reason: str) -> None:
        try:
            await self._controller.async_restore_after_fuse(ceiling, reason)
        except Exception as ex:  # never let the watchdog die
            self.logger.error("Fuse restore failed: %s", ex)

    def _needs_enforcement(
        self,
        decision: LimiterDecision,
        setpoint: float | None,
        readings: list[PhaseReading],
    ) -> bool:
        allowed = decision.allowed_a
        if allowed == 0:
            return True
        if setpoint is not None:
            return setpoint > allowed + 1e-6
        measured = [r.charger_current_a for r in readings if r.charger_current_a is not None]
        if not measured:
            return False
        return max(measured) > allowed + FUSE_MEASURED_OVERDRAW_TOLERANCE_A

    def _start_enforcement(
        self,
        decision: LimiterDecision,
        setpoint: float | None = None,
        now: float | None = None,
    ) -> None:
        if self._enforce_task is not None and not self._enforce_task.done():
            return
        if decision.allowed_a > 0 and setpoint is not None and setpoint > decision.allowed_a:
            # A reduction: count the charger at the higher of the old setpoint
            # and any setpoint already being settled from.
            previous = self._settle_commanded or 0.0
            self._settle_commanded = max(float(setpoint), previous)
            self._settle_ceiling = decision.allowed_a
            self._settle_until = (now if now is not None else time.monotonic()) + (
                FUSE_SETTLE_SECONDS
            )
        elif decision.allowed_a == 0:
            self._clear_settle()
        reason = (
            f"Main fuse protection: {decision.state} "
            f"(ceiling {decision.allowed_a}A, {decision.reason})"
        )
        self._enforce_task = self.hass.async_create_task(
            self._async_enforce(decision.allowed_a, reason)
        )

    async def _async_enforce(self, ceiling: int, reason: str) -> None:
        try:
            await self._controller.async_enforce_fuse_ceiling(ceiling, reason)
        except Exception as ex:  # never let the watchdog die
            self.logger.error("Fuse enforcement failed: %s", ex)

    def _log_transition(self, decision: LimiterDecision, limit: FuseLimit) -> None:
        if decision.state == self._last_state:
            return
        previous = self._last_state
        self._last_state = decision.state
        if decision.state in (
            FuseLimiter.STATE_STOPPED,
            FuseLimiter.STATE_INVALID,
            FuseLimiter.STATE_INVALID_STOP,
            FuseLimiter.STATE_HARD_TRIP,
        ):
            self.logger.warning(
                "Fuse protection %s -> %s: ceiling %sA (%s)",
                previous,
                decision.state,
                decision.allowed_a,
                limit.reason,
            )
        elif decision.state == FuseLimiter.STATE_HOLD:
            self.logger.info(
                "Fuse protection %s -> %s: ceiling %sA (%s)",
                previous,
                decision.state,
                decision.allowed_a,
                limit.reason,
            )
        else:
            # v2.26.0: ok / limited / ramping are logged by the controller
            # when it actually changes the current.
            self.logger.debug(
                "Fuse protection %s -> %s: ceiling %sA (%s)",
                previous,
                decision.state,
                decision.allowed_a,
                limit.reason,
            )

    def _publish(
        self,
        decision: LimiterDecision,
        limit: FuseLimit,
        margin: float,
        readings: list[PhaseReading],
    ) -> None:
        if self._runtime_data is None:
            return
        sensor = self._runtime_data.get_entity(HELPER_FUSE_HEADROOM_SENSOR_SUFFIX)
        if sensor is None or not hasattr(sensor, "publish"):
            return

        attributes: dict[str, Any] = {
            "state": decision.state,
            "reason": decision.reason,
            "data_valid": limit.valid,
            "main_fuse_a": self.fuse_a,
            "margin_a": margin,
            "target_a": decision.target_a,
            "raw_limit_a": None if limit.limit_a is None else round(limit.limit_a, 1),
            "bottleneck_phase": (
                f"L{limit.bottleneck_phase}" if limit.bottleneck_phase else None
            ),
            # v2.19.0
            "overloaded": limit.overloaded,
            "hard_limit_a": round(self._hard_limit_a(), 1),
            "settling": time.monotonic() < self._settle_until,
            # v2.24.1: True only while the guard itself stopped the charger
            # (a fuse pause); "stopped" alone can also mean "no room to start".
            "charger_paused": bool(
                getattr(self._controller, "is_fuse_paused", lambda: False)()
            ),
        }
        for result in limit.phases:
            suffix = f"l{result.phase}"
            attributes[f"grid_current_{suffix}"] = (
                None if result.grid_current_a is None else round(result.grid_current_a, 1)
            )
            attributes[f"charger_current_{suffix}"] = round(result.charger_current_a, 1)
            attributes[f"house_current_{suffix}"] = (
                None if result.house_current_a is None else round(result.house_current_a, 1)
            )
            attributes[f"room_{suffix}"] = (
                None if result.room_a is None else round(result.room_a, 1)
            )
            attributes[f"source_{suffix}"] = result.source

        # Write immediately when the ceiling or state changes; otherwise refresh
        # the (fast-moving) per-phase attributes at most every 30 s so the
        # recorder is not flooded at the meter's update rate.
        key = (decision.allowed_a, decision.state, limit.valid, limit.bottleneck_phase)
        now = time.monotonic()
        if self._last_published is not None:
            last_key, last_at = self._last_published
            if key == last_key and now - last_at < 30:
                return
        self._last_published = (key, now)
        sensor.publish(decision.allowed_a, attributes)
