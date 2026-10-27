"""Centralized Charger Controller for EV Smart Charger."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Optional

import async_timeout
from homeassistant.util import dt as dt_util
from homeassistant.core import HomeAssistant

from .const import (
    CHARGER_AMPERAGE_STABILIZATION_DELAY,
    CHARGER_BUTTON_OFF_GRACE_SECONDS,
    CHARGER_BUTTON_STATE_SETTLE_SECONDS,
    CHARGER_COMMAND_DELAY,
    CHARGER_CURRENT_ENTITY_WAIT_SECONDS,
    CHARGER_MIN_OPERATION_INTERVAL,
    CHARGER_MODEL_TUYA,
    CHARGER_START_SEQUENCE_DELAY,
    CHARGER_STOP_SEQUENCE_DELAY,
    CHARGING_POWER_DRAWING_FLOOR_W,
    CONF_EV_CHARGER_CURRENT,
    CONF_EV_CHARGER_START_BUTTON,
    CONF_EV_CHARGER_STATUS,
    CONF_EV_CHARGER_STOP_BUTTON,
    CONF_EV_CHARGER_SWITCH,
    SERVICE_CALL_TIMEOUT,
    CURRENT_SET_TIMEOUT_SECONDS,
    get_amp_levels,
    get_charger_model,
    uses_charger_buttons,
)
from .fuse_math import highest_level_at_or_below
from .runtime import EVSCRuntimeData
from .utils.amperage_helper import AmperageCalculator
from .utils.logging_helper import EVSCLogger


class FuseLimitError(RuntimeError):
    """Raised when the main-fuse guard forbids starting the charger."""


class CurrentControlAdapter:
    """Adapter for charger current entities across HA domains."""

    _SERVICE_MAP = {
        "number": ("number", "set_value", "value"),
        "input_number": ("input_number", "set_value", "value"),
        "select": ("select", "select_option", "option"),
        "input_select": ("input_select", "select_option", "option"),
    }

    def __init__(self, hass: HomeAssistant, entity_id: str | None) -> None:
        """Initialize the adapter."""
        self.hass = hass
        self.entity_id = entity_id
        self.domain = entity_id.split(".", 1)[0] if entity_id else None

    async def async_validate(self) -> None:
        """Validate entity domain and service availability."""
        if not self.entity_id:
            raise ValueError("Missing charger current control entity")

        if self.domain not in self._SERVICE_MAP:
            raise ValueError(
                f"Unsupported charger current control domain: {self.domain}"
            )

        service_domain, service_name, _ = self._SERVICE_MAP[self.domain]
        if not self.hass.services.has_service(service_domain, service_name):
            raise ValueError(
                f"Missing Home Assistant service: {service_domain}.{service_name}"
            )

    def is_available(self) -> bool:
        """Return True when the entity exists and accepts values right now.

        Some integrations (OCPP's session current limit) only expose the
        current-control entity while a charging session is active.
        """
        if not self.entity_id:
            return False
        state = self.hass.states.get(self.entity_id)
        return state is not None and state.state not in (
            None,
            "unknown",
            "unavailable",
        )

    def get_numeric_state(self) -> int | None:
        """Read a numeric value from the current control entity."""
        if not self.entity_id:
            return None

        state = self.hass.states.get(self.entity_id)
        if state is None or state.state in (None, "unknown", "unavailable", "none"):
            return None

        raw_value = state.state
        try:
            return int(float(raw_value))
        except (TypeError, ValueError):
            match = re.search(r"-?\d+(?:\.\d+)?", str(raw_value))
            if match:
                return int(float(match.group(0)))
        return None

    def build_service_call(
        self,
        value: int | float | str,
    ) -> tuple[str, str, dict]:
        """Build the HA service payload for the configured domain."""
        if self.domain not in self._SERVICE_MAP or not self.entity_id:
            raise ValueError("Current control adapter not configured")

        service_domain, service_name, field_name = self._SERVICE_MAP[self.domain]
        payload_value: int | float | str

        if self.domain in ("select", "input_select"):
            if isinstance(value, (int, float)):
                payload_value = str(int(value))
            else:
                payload_value = str(value)
        else:
            payload_value = value

        return service_domain, service_name, {
            "entity_id": self.entity_id,
            field_name: payload_value,
        }


@dataclass
class OperationResult:
    """Result of a charger operation with detailed feedback."""

    success: bool
    operation: str
    reason: str
    amperage: Optional[int] = None
    queued: bool = False
    error_message: Optional[str] = None
    timestamp: datetime | None = None

    def __post_init__(self):
        """Set timestamp if not provided."""
        if self.timestamp is None:
            self.timestamp = dt_util.now()

    def __str__(self) -> str:
        """Human-readable representation for logging."""
        status = "✅ SUCCESS" if self.success else "❌ FAILED"
        details = []
        if self.amperage is not None:
            details.append(f"{self.amperage}A")
        if self.error_message:
            details.append(f"Error: {self.error_message}")
        details_str = f" ({', '.join(details)})" if details else ""
        return f"{status}: {self.operation}{details_str} - {self.reason}"


class ChargerController:
    """Centralized controller for all charger operations."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict,
        runtime_data: EVSCRuntimeData | None = None,
    ):
        """Initialize ChargerController."""
        self.hass = hass
        self.entry_id = entry_id
        self.config = config
        self._runtime_data = runtime_data
        self.logger = EVSCLogger("CHARGER CONTROLLER")

        self._charger_switch = config.get(CONF_EV_CHARGER_SWITCH)
        # v2.14.0 (issue #65): start/stop buttons instead of a switch. The
        # buttons carry no state, so the controller remembers what it commanded.
        self._start_button = config.get(CONF_EV_CHARGER_START_BUTTON)
        self._stop_button = config.get(CONF_EV_CHARGER_STOP_BUTTON)
        self._uses_buttons = uses_charger_buttons(config)
        self._commanded_on: Optional[bool] = None
        self._last_command_time: Optional[datetime] = None
        self._last_seen_drawing: Optional[datetime] = None
        self._charger_current = config.get(CONF_EV_CHARGER_CURRENT)
        self._current_control = CurrentControlAdapter(hass, self._charger_current)

        # Charger model (v2.0.0): governs the amperage level set and whether the
        # safe stop/set/start decrease sequence is used (tuya) or a live set (generic).
        self._amp_levels = get_amp_levels(config)
        self._charger_model = get_charger_model(config)

        self._last_operation_time: Optional[datetime] = None
        self._current_amperage: Optional[int] = None
        self._is_on: Optional[bool] = None
        # v2.2.0 — measured charging power (W) cached on each _refresh_state.
        # None when no charging-power sensor is mapped (→ drawing-now falls back
        # to the commanded switch echo, byte-for-byte legacy behaviour).
        self._measured_power_w: Optional[float] = None
        self._lock = asyncio.Lock()
        # v2.14.0 main-fuse protection: what the automations last asked for,
        # and whether the fuse guard has reduced / stopped the charger since.
        # Lets the guard restore the request once the fuse has room again.
        self._requested_amps: Optional[int] = None
        self._fuse_reduced = False
        self._fuse_stopped = False

        self.logger.info(
            "Initialized ChargerController - Switch: %s, Current: %s, Model: %s",
            (
                f"buttons {self._start_button} / {self._stop_button}"
                if self._uses_buttons
                else self._charger_switch
            ),
            self._charger_current,
            self._charger_model,
        )

    async def _emit_operation_diagnostic(
        self,
        operation: str,
        result: str,
        *,
        reason_code: str,
        reason_detail: str,
        target_amps: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Publish a structured diagnostic event for controller operations."""
        if self._runtime_data is None or self._runtime_data.diagnostic_manager is None:
            return

        await self._runtime_data.diagnostic_manager.async_emit_event(
            component="Charger Controller",
            event=operation,
            result=result,
            reason_code=reason_code,
            reason_detail=reason_detail,
            raw_values={
                "target_amps": target_amps,
                "current_amps": self._current_amperage,
                "charger_on": self._is_on,
                # v2.2.0: measured charging power (diagnostic only — control uses
                # the commanded switch echo). drawing_now is the model's stateless
                # verdict; None when no power sensor is mapped.
                "measured_power_w": self._measured_power_w,
                "drawing_now": (
                    self._runtime_data.power_model.is_charging(self.hass)
                    if self._runtime_data and self._runtime_data.power_model
                    else None
                ),
                "charger_switch": self._charger_switch,
                "charger_buttons": (
                    [self._start_button, self._stop_button]
                    if self._uses_buttons
                    else None
                ),
                "current_entity": self._charger_current,
                "error_message": error_message,
            },
            severity="warning" if result == "failed" else "info",
        )

    async def async_setup(self):
        """Setup controller and validate capabilities."""
        self.logger.info("Setting up ChargerController")
        await self._current_control.async_validate()
        await self._refresh_state()
        self.logger.success(
            "Setup complete - Initial state: On=%s, Amperage=%sA",
            self._is_on,
            self._current_amperage,
        )

    async def _refresh_state(self):
        """Refresh cached state from Home Assistant.

        ``_is_on`` / ``_current_amperage`` stay strictly COMMANDED (switch echo,
        setpoint) and drive ALL control decisions (byte-for-byte v2.1.x). v2.2.0
        also caches the measured charging power for the operation DIAGNOSTIC only
        (it does not gate any control path); None when no power sensor is mapped.
        """
        if not self._uses_buttons:
            charger_state = self.hass.states.get(self._charger_switch)
            if charger_state:
                self._is_on = charger_state.state == "on"
        self._current_amperage = self._current_control.get_numeric_state()
        if self._runtime_data is not None and self._runtime_data.power_model is not None:
            self._measured_power_w = self._runtime_data.power_model.read_charging_power(
                self.hass
            )
        if self._uses_buttons:
            self._is_on = self._button_mode_is_on()

    def _button_mode_is_on(self) -> Optional[bool]:
        """Resolve the on/off state of a button-controlled charger (issue #65).

        Right after a press the charger needs a few seconds to start or stop
        drawing, so the commanded state is trusted for
        ``CHARGER_BUTTON_STATE_SETTLE_SECONDS``. After that the measured state
        wins, which also picks up sessions started or stopped outside the
        integration (wallbox app, auto-start on plug-in). With neither a power
        nor a status sensor mapped, the commanded state is the only signal.
        """
        if self._commanded_on is not None and self._last_command_time is not None:
            elapsed = (dt_util.now() - self._last_command_time).total_seconds()
            if elapsed < CHARGER_BUTTON_STATE_SETTLE_SECONDS:
                return self._commanded_on

        power_model = (
            self._runtime_data.power_model if self._runtime_data is not None else None
        )
        has_signal = self._measured_power_w is not None or bool(
            self.config.get(CONF_EV_CHARGER_STATUS)
        )
        if power_model is not None and has_signal:
            drawing = power_model.is_charging(self.hass)
            now = dt_util.now()
            if drawing:
                self._last_seen_drawing = now
                return True
            # v2.25.2: ride out a one-update dropout of the power/status
            # sensor while no stop was pressed.
            if (
                self._commanded_on is not False
                and self._last_seen_drawing is not None
                and (now - self._last_seen_drawing).total_seconds()
                < CHARGER_BUTTON_OFF_GRACE_SECONDS
            ):
                return True
            return False
        return self._commanded_on

    def button_charger_on(self) -> bool:
        """Synchronous on/off answer for a button-controlled charger.

        Used by the main fuse guard, which has no switch to read when the
        charger is driven by start/stop buttons.
        """
        if self._runtime_data is not None and self._runtime_data.power_model is not None:
            self._measured_power_w = self._runtime_data.power_model.read_charging_power(
                self.hass
            )
        return bool(self._button_mode_is_on())

    def _record_command(self, on: bool) -> None:
        """Remember the last on/off command sent to a button-controlled charger."""
        self._commanded_on = on
        self._last_command_time = dt_util.now()
        if not on:
            self._last_seen_drawing = None

    async def start_charger(
        self,
        target_amps: Optional[int] = None,
        reason: str = "",
    ) -> OperationResult:
        """Start the charger with optional target amperage."""
        async with self._lock:
            try:
                self.logger.separator()
                self.logger.start(f"{self.logger.CHARGER} Start Charger")
                self.logger.info("Reason: %s", reason or "No reason provided")
                await self._wait_for_rate_limit()
                await self._refresh_state()

                capped_target = None
                requested_target = None
                if target_amps is not None:
                    requested_target = self._normalize_target_amps(target_amps)
                    capped_target = self._cap_to_fuse(requested_target)
                elif self._fuse_ceiling() is not None:
                    # No explicit target: keep the existing setpoint, but never
                    # above what the main fuse allows right now.
                    capped_target = self._cap_to_fuse(
                        self._current_amperage or self._amp_levels[0]
                    )
                if capped_target == 0:
                    raise FuseLimitError(
                        "Main fuse limit: no room to start the charger"
                    )

                rejected_while_running = await self._start_sequence(capped_target)
                self._remember_request(requested_target, capped_target)

                self._record_operation_time()
                await self._refresh_state()
                await self._emit_operation_diagnostic(
                    "charger_start",
                    "succeeded",
                    reason_code=(
                        "already_charging"
                        if rejected_while_running
                        else "command_executed"
                    ),
                    reason_detail=reason or "No reason provided",
                    target_amps=self._current_amperage,
                )
                return OperationResult(
                    success=True,
                    operation="start",
                    reason=reason,
                    amperage=self._current_amperage,
                )
            except Exception as ex:
                self.logger.error("Failed to start charger: %s", ex)
                await self._emit_operation_diagnostic(
                    "charger_start",
                    "failed",
                    reason_code="command_failed",
                    reason_detail=reason or "No reason provided",
                    target_amps=self._normalize_target_amps(target_amps)
                    if target_amps is not None
                    else None,
                    error_message=str(ex),
                )
                return OperationResult(
                    success=False,
                    operation="start",
                    reason=reason,
                    amperage=self._normalize_target_amps(target_amps)
                    if target_amps is not None
                    else None,
                    error_message=str(ex),
                )
            finally:
                self.logger.separator()

    async def stop_charger(self, reason: str = "") -> OperationResult:
        """Stop the charger."""
        async with self._lock:
            try:
                self.logger.separator()
                self.logger.start(f"{self.logger.CHARGER} Stop Charger")
                self.logger.info("Reason: %s", reason or "No reason provided")
                await self._wait_for_rate_limit()
                await self._stop_charger_unlocked()
                await self._emit_operation_diagnostic(
                    "charger_stop",
                    "succeeded",
                    reason_code="command_executed",
                    reason_detail=reason or "No reason provided",
                )
                return OperationResult(
                    success=True,
                    operation="stop",
                    reason=reason,
                )
            except Exception as ex:
                self.logger.error("Failed to stop charger: %s", ex)
                await self._emit_operation_diagnostic(
                    "charger_stop",
                    "failed",
                    reason_code="command_failed",
                    reason_detail=reason or "No reason provided",
                    error_message=str(ex),
                )
                return OperationResult(
                    success=False,
                    operation="stop",
                    reason=reason,
                    error_message=str(ex),
                )
            finally:
                self.logger.separator()

    async def set_amperage(self, target_amps: int, reason: str = "") -> OperationResult:
        """Set charger amperage with safe decrease handling."""
        async with self._lock:
            requested_target = self._normalize_target_amps(target_amps)
            normalized_target = self._cap_to_fuse(requested_target)

            try:
                self.logger.separator()
                self.logger.start(f"{self.logger.CHARGER} Set Amperage")
                self.logger.info(
                    "Target: %sA (Current: %sA)",
                    normalized_target,
                    self._current_amperage,
                )
                self.logger.info("Reason: %s", reason or "No reason provided")

                await self._refresh_state()
                if normalized_target == 0:
                    # Main fuse leaves no room for the minimum current.
                    if self._is_on:
                        await self._stop_charger_unlocked()
                    await self._emit_operation_diagnostic(
                        "charger_set_amperage",
                        "succeeded",
                        reason_code="fuse_limit_stop",
                        reason_detail=reason or "Main fuse limit",
                    )
                    return OperationResult(
                        success=True,
                        operation="stop",
                        reason="Main fuse limit: stopped charger",
                        amperage=0,
                    )
                if self._current_amperage == normalized_target:
                    self._remember_request(requested_target, normalized_target)
                    await self._emit_operation_diagnostic(
                        "charger_set_amperage",
                        "succeeded",
                        reason_code="already_at_target",
                        reason_detail=f"Already at target ({normalized_target}A)",
                        target_amps=normalized_target,
                    )
                    return OperationResult(
                        success=True,
                        operation="set_amperage",
                        reason=f"Already at target ({normalized_target}A)",
                        amperage=normalized_target,
                    )

                await self._wait_for_rate_limit()
                operation = await self._set_amperage_unlocked(normalized_target)
                self._remember_request(requested_target, normalized_target)
                await self._emit_operation_diagnostic(
                    "charger_set_amperage",
                    "succeeded",
                    reason_code="command_executed",
                    reason_detail=reason or "No reason provided",
                    target_amps=normalized_target,
                )
                return OperationResult(
                    success=True,
                    operation=operation,
                    reason=reason,
                    amperage=normalized_target,
                )
            except Exception as ex:
                self.logger.error("Failed to set amperage: %s", ex)
                await self._emit_operation_diagnostic(
                    "charger_set_amperage",
                    "failed",
                    reason_code="command_failed",
                    reason_detail=reason or "No reason provided",
                    target_amps=normalized_target,
                    error_message=str(ex),
                )
                return OperationResult(
                    success=False,
                    operation="set_amperage",
                    reason=reason,
                    amperage=normalized_target,
                    error_message=str(ex),
                )
            finally:
                self.logger.separator()

    async def adjust_for_grid_import(
        self,
        reason: str = "Grid import detected",
    ) -> OperationResult:
        """Reduce charging amperage by one level for grid import protection."""
        async with self._lock:
            try:
                self.logger.separator()
                self.logger.start(f"{self.logger.CHARGER} Grid Import Protection")
                self.logger.info("Reason: %s", reason)
                await self._refresh_state()

                current_amps = self._current_amperage or 0
                next_amps = AmperageCalculator.get_next_level_down(
                    current_amps, self._amp_levels
                )
                await self._wait_for_rate_limit()

                if next_amps == 0:
                    await self._stop_charger_unlocked()
                    return OperationResult(
                        success=True,
                        operation="stop",
                        reason=reason,
                    )

                operation = await self._set_amperage_unlocked(next_amps)
                return OperationResult(
                    success=True,
                    operation=operation,
                    reason=reason,
                    amperage=next_amps,
                )
            except Exception as ex:
                self.logger.error("Failed to adjust for grid import: %s", ex)
                return OperationResult(
                    success=False,
                    operation="adjust_for_grid_import",
                    reason=reason,
                    error_message=str(ex),
                )
            finally:
                self.logger.separator()

    async def recover_to_target(
        self,
        target_amps: int,
        reason: str = "Conditions improved",
    ) -> OperationResult:
        """Gradually recover charging amperage toward target by one level."""
        requested_target = self._normalize_target_amps(target_amps)
        normalized_target = self._cap_to_fuse(requested_target)

        async with self._lock:
            try:
                self.logger.separator()
                self.logger.start(f"{self.logger.CHARGER} Amperage Recovery")
                self.logger.info("Target: %sA", normalized_target)
                self.logger.info("Reason: %s", reason)
                await self._refresh_state()

                current_amps = self._current_amperage or 0
                if normalized_target == 0:
                    return OperationResult(
                        success=False,
                        operation="recover_to_target",
                        reason=reason,
                        error_message="Main fuse limit: no room to charge",
                    )
                if current_amps >= normalized_target:
                    return OperationResult(
                        success=True,
                        operation="recover_to_target",
                        reason=f"Already at target ({current_amps}A)",
                        amperage=current_amps,
                    )

                await self._wait_for_rate_limit()

                if not self._is_on or current_amps == 0:
                    await self._start_charger_unlocked(normalized_target)
                    self._remember_request(requested_target, normalized_target)
                    return OperationResult(
                        success=True,
                        operation="start",
                        reason=reason,
                        amperage=self._current_amperage,
                    )

                next_amps = AmperageCalculator.get_next_level_up(
                    current_amps,
                    normalized_target,
                    self._amp_levels,
                )
                operation = await self._set_amperage_unlocked(next_amps)
                return OperationResult(
                    success=True,
                    operation=operation,
                    reason=reason,
                    amperage=next_amps,
                )
            except Exception as ex:
                self.logger.error("Failed to recover amperage: %s", ex)
                return OperationResult(
                    success=False,
                    operation="recover_to_target",
                    reason=reason,
                    amperage=normalized_target,
                    error_message=str(ex),
                )
            finally:
                self.logger.separator()

    async def _start_charger_unlocked(self, target_amps: Optional[int]) -> None:
        """Start the charger without reacquiring the controller lock."""
        await self._start_sequence(target_amps)
        self._record_operation_time()
        await self._refresh_state()

    async def _start_sequence(self, target_amps: Optional[int]) -> bool:
        """Set the current (when possible) and turn the charger on.

        Normally the current is set BEFORE ``switch.turn_on`` so the session
        starts at the right level. Some integrations (OCPP's session current
        limit) only make the current entity available once a session runs; in
        that case the charger is turned on first and the current is applied as
        soon as the entity appears. The main-fuse watchdog covers the short
        window in between.

        Returns True when a redundant ``turn_on`` rejection was tolerated.
        """
        deferred = False
        if target_amps is not None:
            if self._current_control.is_available():
                await self._set_amperage_internal(target_amps)
                await asyncio.sleep(CHARGER_AMPERAGE_STABILIZATION_DELAY)
            else:
                deferred = True
                self.logger.info(
                    "Current entity %s unavailable before start - will set %sA "
                    "once the session is active",
                    self._charger_current,
                    target_amps,
                )

        rejected_while_running = await self._turn_on_charger_switch()
        await asyncio.sleep(CHARGER_START_SEQUENCE_DELAY)

        if deferred:
            if await self._wait_for_current_entity():
                await self._set_amperage_internal(target_amps)
                await asyncio.sleep(CHARGER_AMPERAGE_STABILIZATION_DELAY)
            else:
                self.logger.warning(
                    "Current entity %s still unavailable %ss after start - "
                    "could not apply %sA",
                    self._charger_current,
                    CHARGER_CURRENT_ENTITY_WAIT_SECONDS,
                    target_amps,
                )
        return rejected_while_running

    async def _wait_for_current_entity(self) -> bool:
        """Wait (bounded) for the current-control entity to become available."""
        for _ in range(int(CHARGER_CURRENT_ENTITY_WAIT_SECONDS)):
            if self._current_control.is_available():
                return True
            await asyncio.sleep(1)
        return self._current_control.is_available()

    async def _turn_on_charger_switch(self) -> bool:
        """Turn the charger switch on, tolerating a redundant-command rejection.

        Some charger integrations reject ``switch.turn_on`` when the vehicle is
        already charging (e.g. Tesla Fleet raises "Command was unsuccessful:
        is_charging"). The desired outcome is already achieved, so a rejection
        is only treated as fatal when the charger is not actually running
        afterwards (issue #60).

        Returns True when a rejection was tolerated, False on a clean call.
        Raises FuseLimitError when the main-fuse guard forbids charging.
        """
        if self._fuse_ceiling() == 0:
            raise FuseLimitError("Main fuse limit: charger must stay off")
        try:
            if self._uses_buttons:
                await self._call_service(
                    "button",
                    "press",
                    {"entity_id": self._start_button},
                    log_failure=False,
                )
                self._record_command(True)
            else:
                await self._call_service(
                    "switch",
                    "turn_on",
                    {"entity_id": self._charger_switch},
                    log_failure=False,
                )
            return False
        except Exception as ex:
            await self._refresh_state()
            if self._charger_already_running():
                self.logger.warning(
                    "Charger rejected switch.turn_on (%s) but is already "
                    "charging - treating the start as successful",
                    ex,
                )
                return True
            self.logger.error(
                "Service call failed: %s - %s",
                "button.press" if self._uses_buttons else "switch.turn_on",
                ex,
            )
            raise

    async def _turn_off_charger_switch(self) -> None:
        """Turn the charger off through its switch or its stop button."""
        if self._uses_buttons:
            try:
                await self._call_service(
                    "button",
                    "press",
                    {"entity_id": self._stop_button},
                    log_failure=False,
                )
            except Exception as err:
                # v2.25.1: a stop button rejects the press when there is no
                # session ("no active transaction"). With the measured draw
                # below the floor the charger is already stopped: done.
                await self._refresh_state()
                # v2.26.0: "The charger is not connected" means there is no
                # session to stop either.
                if "not connected" in str(err).lower() or (
                    self._measured_power_w is not None
                    and self._measured_power_w <= CHARGING_POWER_DRAWING_FLOOR_W
                ):
                    self.logger.warning(
                        f"Stop button rejected ({err}) - charger is not drawing, "
                        "treating it as stopped"
                    )
                    self._record_command(False)
                    return
                self.logger.error(f"Service call failed: button.press - {err}")
                raise
            self._record_command(False)
            return
        await self._call_service(
            "switch",
            "turn_off",
            {"entity_id": self._charger_switch},
        )

    def _charger_already_running(self) -> bool:
        """Return True when the charger is on, by switch echo or measured draw.

        Only strong evidence counts: the switch reporting ON, or a mapped power
        sensor above the drawing floor. The tolerant status fallback is not
        used here, so an unknown brand status can never mask a real failure.
        """
        if self._is_on:
            return True
        return (
            self._measured_power_w is not None
            and self._measured_power_w > CHARGING_POWER_DRAWING_FLOOR_W
        )

    async def _stop_charger_unlocked(self) -> None:
        """Stop the charger without reacquiring the controller lock."""
        # A deliberate stop: the fuse guard must not restart the charger later.
        self._fuse_stopped = False
        self._fuse_reduced = False
        await self._turn_off_charger_switch()
        await asyncio.sleep(CHARGER_COMMAND_DELAY)
        self._record_operation_time()
        await self._refresh_state()

    async def _set_amperage_unlocked(self, target_amps: int) -> str:
        """Set charger amperage without reacquiring the controller lock.

        On DECREASE, Tuya chargers need the safe stop/set/start sequence
        (changing current while charging misbehaves). Generic (non-Tuya)
        wallboxes accept a live current reduction, so we set the lower value
        directly — no stop, no ~6-8s interruption per step (v2.0.0).
        """
        await self._refresh_state()
        current_amps = self._current_amperage or 0
        is_on = bool(self._is_on)

        if (
            is_on
            and target_amps < current_amps
            and self._charger_model == CHARGER_MODEL_TUYA
        ):
            await self._decrease_amperage_unlocked(target_amps)
            return "adjust_down"

        await self._set_amperage_internal(target_amps)
        self._record_operation_time()
        await self._refresh_state()
        return "set_amperage"

    async def _decrease_amperage_unlocked(
        self, target_amps: int, *, fuse_action: bool = False
    ) -> None:
        """Decrease amperage using the safe stop/set/start sequence."""
        await self._turn_off_charger_switch()
        await asyncio.sleep(CHARGER_STOP_SEQUENCE_DELAY)
        await self._set_amperage_internal(target_amps, fuse_action=fuse_action)
        await asyncio.sleep(CHARGER_AMPERAGE_STABILIZATION_DELAY)
        await self._turn_on_charger_switch()
        await asyncio.sleep(CHARGER_START_SEQUENCE_DELAY)
        self._record_operation_time()
        await self._refresh_state()

    async def _set_amperage_internal(self, amps: int, *, fuse_action: bool = False) -> None:
        """Set the configured charger amperage entity.

        Last line of defence for the main fuse: whatever path got here, the
        value sent to the charger never exceeds the current fuse ceiling.
        ``fuse_action`` marks writes made by the fuse guard itself; every other
        write records the automation's request so it can be restored later.
        """
        if not fuse_action:
            self._requested_amps = amps
            self._fuse_reduced = False
        ceiling = self._fuse_ceiling()
        if ceiling is not None:
            if ceiling == 0:
                raise FuseLimitError("Main fuse limit: no room for the minimum current")
            capped = min(amps, highest_level_at_or_below(ceiling, self._amp_levels))
            if capped < amps and not fuse_action:
                self._fuse_reduced = True
            amps = capped
        service_domain, service_name, data = self._current_control.build_service_call(amps)
        try:
            await self._call_service(
                service_domain,
                service_name,
                data,
                log_failure=False,
                timeout=CURRENT_SET_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # v2.26.0: cloud chargers (Growatt THOR) confirm a new current
            # slowly; the command has been sent, so carry on.
            self.logger.warning(
                "Charger did not confirm %sA within %ss - continuing",
                amps,
                CURRENT_SET_TIMEOUT_SECONDS,
            )
        except Exception as ex:
            self.logger.error(
                "Service call failed: %s.%s - %s", service_domain, service_name, ex
            )
            raise

    async def _wait_for_rate_limit(self) -> None:
        """Wait until the minimum operation interval has elapsed."""
        if self._last_operation_time is None:
            return

        elapsed = (dt_util.now() - self._last_operation_time).total_seconds()
        if elapsed >= CHARGER_MIN_OPERATION_INTERVAL:
            return

        wait_time = CHARGER_MIN_OPERATION_INTERVAL - elapsed
        self.logger.info("Rate limit active, waiting %.1fs", wait_time)
        await asyncio.sleep(wait_time)

    def _record_operation_time(self) -> None:
        """Record the completion time of the last operation."""
        self._last_operation_time = dt_util.now()

    def _normalize_target_amps(self, target_amps: int | float | None) -> int:
        """Normalize a target amperage to the nearest supported level."""
        if target_amps is None:
            return self._amp_levels[0]
        return min(self._amp_levels, key=lambda value: abs(value - int(target_amps)))

    # ------------------------------------------------------------------
    # Main fuse protection (v2.14.0)
    # ------------------------------------------------------------------
    def fuse_ceiling(self) -> int | None:
        """Public main-fuse ceiling in A (None = no fuse protection)."""
        return self._fuse_ceiling()

    def _fuse_ceiling(self) -> int | None:
        """Return the main-fuse ceiling in A, or None when not configured."""
        if self._runtime_data is None:
            return None
        guard = getattr(self._runtime_data, "fuse_guard", None)
        if guard is None or not getattr(guard, "enabled", False):
            return None
        return guard.max_allowed_amps()

    def _cap_to_fuse(self, amps: int) -> int:
        """Clamp a target to the fuse ceiling (0 = must not charge)."""
        ceiling = self._fuse_ceiling()
        if ceiling is None:
            return amps
        if ceiling == 0:
            return 0
        return min(amps, highest_level_at_or_below(ceiling, self._amp_levels))

    async def async_enforce_fuse_ceiling(self, ceiling: int, reason: str) -> None:
        """Bring a running charger down to the fuse ceiling immediately.

        Called by the FuseGuard watchdog. Deliberately does NOT take the
        controller lock or honour the operation rate limit: protecting the
        fuse must not wait behind a 30 s rate-limit sleep or a slow sequence.
        Any operation already in flight clamps itself in
        ``_set_amperage_internal`` / ``_turn_on_charger_switch``, so it cannot
        undo this reduction.
        """
        await self._refresh_state()
        if not self._is_on:
            return

        if ceiling <= 0:
            self.logger.warning("%s - stopping charger", reason)
            if self._requested_amps is None and self._current_amperage:
                self._requested_amps = self._current_amperage
            await self._turn_off_charger_switch()
            self._fuse_stopped = True
            self._record_operation_time()
            await self._refresh_state()
            await self._emit_operation_diagnostic(
                "charger_stop",
                "succeeded",
                reason_code="fuse_limit_stop",
                reason_detail=reason,
            )
            return

        target = highest_level_at_or_below(ceiling, self._amp_levels)
        current = self._current_amperage
        if current is not None and current <= target:
            return
        if not self._current_control.is_available():
            self.logger.warning(
                "%s - current entity unavailable, cannot reduce; stopping charger",
                reason,
            )
            await self.async_enforce_fuse_ceiling(0, reason)
            return

        self.logger.warning("%s - reducing %sA -> %sA", reason, current, target)
        if self._requested_amps is None and current is not None:
            self._requested_amps = current
        self._fuse_reduced = True
        if self._charger_model == CHARGER_MODEL_TUYA:
            await self._decrease_amperage_unlocked(target, fuse_action=True)
        else:
            await self._set_amperage_internal(target, fuse_action=True)
            self._record_operation_time()
            await self._refresh_state()
        await self._emit_operation_diagnostic(
            "charger_set_amperage",
            "succeeded",
            reason_code="fuse_limit_reduce",
            reason_detail=reason,
            target_amps=target,
        )

    def _remember_request(self, requested: int | None, applied: int | None) -> None:
        """Record what an automation asked for, before the fuse cap.

        ``_set_amperage_internal`` only sees the already-capped value; this
        keeps the original request so the guard can restore it later.
        """
        if requested is None or applied is None:
            return
        self._requested_amps = requested
        self._fuse_reduced = applied < requested

    def is_fuse_paused(self) -> bool:
        """True while the fuse guard has stopped the charger and may restart it.

        Automations use this to tell a fuse pause apart from a finished or
        unplugged session.
        """
        return self._fuse_stopped

    def fuse_restore_pending(self) -> bool:
        """True when the fuse guard reduced or stopped charging it may undo."""
        return self._fuse_reduced or self._fuse_stopped

    async def async_restore_after_fuse(self, ceiling: int, reason: str) -> None:
        """Undo a fuse reduction/stop once the fuse has room again.

        Only ever restores towards what an automation last requested, never
        beyond it, and only for reductions the fuse guard itself made. A stop
        by an automation or the user clears the pending restart.
        """
        if ceiling <= 0 or not self.fuse_restore_pending():
            return
        await self._refresh_state()
        requested = self._requested_amps or self._amp_levels[0]
        target = min(requested, highest_level_at_or_below(ceiling, self._amp_levels))
        if target <= 0:
            return

        if self._fuse_stopped:
            if self._is_on:
                # Someone else restarted it; nothing left for us to do.
                self._fuse_stopped = False
            else:
                power_model = (
                    self._runtime_data.power_model if self._runtime_data else None
                )
                if (
                    power_model is not None
                    and getattr(power_model, "_charger_status_entity", None)
                    and not power_model.is_plugged_in(self.hass)
                ):
                    self.logger.info("Fuse restart skipped - vehicle not plugged in")
                    self._fuse_stopped = False
                    self._fuse_reduced = False
                    return
                self.logger.info("%s - restarting charger at %sA", reason, target)
                self._fuse_stopped = False
                await self._start_sequence_for_fuse(target)
                self._fuse_reduced = target < requested
                await self._emit_operation_diagnostic(
                    "charger_start",
                    "succeeded",
                    reason_code="fuse_limit_restart",
                    reason_detail=reason,
                    target_amps=target,
                )
                return

        if not self._is_on:
            return
        current = self._current_amperage
        if current is not None and target <= current:
            if target >= requested:
                self._fuse_reduced = False
            return
        if not self._current_control.is_available():
            return
        self.logger.info("%s - restoring %sA -> %sA", reason, current, target)
        await self._set_amperage_internal(target, fuse_action=True)
        self._record_operation_time()
        await self._refresh_state()
        self._fuse_reduced = target < requested
        await self._emit_operation_diagnostic(
            "charger_set_amperage",
            "succeeded",
            reason_code="fuse_limit_restore",
            reason_detail=reason,
            target_amps=target,
        )

    async def _start_sequence_for_fuse(self, target: int) -> None:
        """Restart after a fuse stop without overwriting the stored request."""
        deferred = not self._current_control.is_available()
        if not deferred:
            await self._set_amperage_internal(target, fuse_action=True)
            await asyncio.sleep(CHARGER_AMPERAGE_STABILIZATION_DELAY)
        await self._turn_on_charger_switch()
        await asyncio.sleep(CHARGER_START_SEQUENCE_DELAY)
        if deferred and await self._wait_for_current_entity():
            await self._set_amperage_internal(target, fuse_action=True)
        self._record_operation_time()
        await self._refresh_state()

    async def _call_service(
        self,
        domain: str,
        service: str,
        data: dict,
        *,
        log_failure: bool = True,
        timeout: float = SERVICE_CALL_TIMEOUT,
    ):
        """Call a Home Assistant service with timeout and error handling."""
        try:
            async with async_timeout.timeout(timeout):
                await self.hass.services.async_call(
                    domain,
                    service,
                    data,
                    blocking=True,
                )
        except asyncio.TimeoutError:
            if log_failure:
                self.logger.error("Service call timeout: %s.%s", domain, service)
            raise
        except Exception as ex:
            if log_failure:
                self.logger.error(
                    "Service call failed: %s.%s - %s", domain, service, ex
                )
            raise

    async def is_charging(self) -> bool:
        """Return whether the charger is currently on."""
        await self._refresh_state()
        return self._is_on or False

    async def get_current_amperage(self) -> Optional[int]:
        """Return the current configured amperage."""
        await self._refresh_state()
        return self._current_amperage

