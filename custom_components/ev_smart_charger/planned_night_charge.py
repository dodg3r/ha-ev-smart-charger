"""Night Smart Charge driven by electricity prices (v2.20.0).

Replaces the start-time based Night Smart Charge:

- The night window runs from the sunset before the car-ready deadline to the
  deadline. The deadline is the car-ready time of the day the car is needed
  (one time per weekday, ``evsc_car_ready_time_<day>``).
- When prices are known, the 5 cheapest hours of the window are published.
- When the car is plugged in, the energy needed to reach that day's EV target
  is turned into a number of hours at the night charge amperage, and the
  cheapest hours before the deadline are planned.
- The charger runs only in planned hours. When car ready is OFF for that day
  nothing is charged at night (Solar Surplus charges during the day).

The public interface matches the old ``NightSmartCharge`` so Solar Surplus,
Boost, the Smart Blocker and the coordinator keep working unchanged.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .charge_planner import NightPlan, build_plan, energy_needed_kwh
from .const import (
    CAR_READY_TIME_DAY_SUFFIX_PREFIX,
    CONF_BATTERY_CAPACITY,
    CONF_EV_CHARGER_STATUS,
    CONF_PRICE_SENSOR,
    CONF_SOC_CAR,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CAR_READY_TIME,
    DEFAULT_CAR_READY_WEEKDAY,
    DEFAULT_CAR_READY_WEEKEND,
    DEFAULT_EV_MIN_SOC_WEEKDAY,
    DEFAULT_EV_MIN_SOC_WEEKEND,
    DEFAULT_NIGHT_CHARGE_AMPERAGE,
    HELPER_CAR_READY_TIME_SUFFIX,
    HELPER_NIGHT_CHARGE_AMPERAGE_SUFFIX,
    HELPER_NIGHT_CHARGE_ENABLED_SUFFIX,
    HELPER_NIGHT_CHARGE_PLAN_SUFFIX,
    HELPER_NIGHT_SESSION_STATE_SUFFIX,
    NIGHT_CHARGE_MODE_GRID,
    NIGHT_CHARGE_MODE_IDLE,
    NIGHT_PLAN_CHARGE_EFFICIENCY,
    NIGHT_PLAN_CHEAPEST_HOURS,
    NIGHT_PLAN_PRICE_REFRESH_SECONDS,
    PRIORITY_NIGHT_CHARGE,
    WEEKDAY_KEYS,
)
from .power_model import ChargingModel, is_disconnected_status
from .price_source import async_read_hourly_prices, hour_start
from .runtime import EVSCRuntimeData
from .utils import state_helper
from .utils.astral_time_service import AstralTimeService
from .utils.logging_helper import EVSCLogger

_UNAVAILABLE = (None, "unknown", "unavailable")

STATUS_DISABLED = "disabled"
STATUS_CAR_READY_OFF = "car_ready_off"
STATUS_WAITING_FOR_CAR = "waiting_for_car"
STATUS_TARGET_REACHED = "target_reached"
STATUS_PLANNED = "planned"
STATUS_CHARGING = "charging"
STATUS_NO_PRICES = "no_prices"


def _parse_time(value: str | None) -> time | None:
    if not value or value in _UNAVAILABLE:
        return None
    parts = str(value).split(":")
    try:
        return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    except (ValueError, IndexError):
        return None


class PlannedNightCharge:
    """Charge in the cheapest hours before the car-ready deadline."""

    uses_price_plan = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: dict,
        priority_balancer,
        charger_controller,
        runtime_data: EVSCRuntimeData | None = None,
        coordinator=None,
        boost_charge=None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.config = config
        self.priority_balancer = priority_balancer
        self.charger_controller = charger_controller
        self._runtime_data = runtime_data
        self._coordinator = coordinator
        self._boost_charge = boost_charge
        self.logger = EVSCLogger("NIGHT SMART CHARGE")
        self._astral = AstralTimeService(hass)

        pm = runtime_data.power_model if runtime_data is not None else None
        self._power_model = (
            pm if isinstance(pm, ChargingModel) else ChargingModel.from_config(config)
        )
        self._price_entity = config.get(CONF_PRICE_SENSOR)
        self._status_entity = config.get(CONF_EV_CHARGER_STATUS)
        self._soc_entity = config.get(CONF_SOC_CAR)

        self._unsubs: list = []
        self._prices: dict[datetime, float] = {}
        self._prices_fetched_at: datetime | None = None
        self._prices_dirty = True
        self._active = False
        self._active_mode_value = NIGHT_CHARGE_MODE_IDLE
        self._committed: set[datetime] = set()
        self._plan_deadline: datetime | None = None
        self._completed_deadline: datetime | None = None
        # Target the latch was set for: raising the target re-opens the plan.
        self._completed_target: float | None = None
        self._was_connected = False
        self._last_plan: NightPlan | None = None
        self._tick_running = False
        # Read by Solar Surplus (1 h cooldown after a night session). Planned
        # night charging never blocks the solar day, so this stays None.
        self._last_completion_time = None

    # ------------------------------------------------------------------
    # Coordinator ownership
    # ------------------------------------------------------------------

    @property
    def _automation_name(self) -> str:
        return "Night Smart Charge"

    @property
    def _active_mode(self) -> str:
        return self._active_mode_value

    @_active_mode.setter
    def _active_mode(self, value: str) -> None:
        if value == self._active_mode_value:
            return
        self._active_mode_value = value
        self._publish(HELPER_NIGHT_SESSION_STATE_SUFFIX, value)

    def _has_control(self) -> bool:
        if self._coordinator is None:
            return True
        return self._coordinator.is_automation_active(self._automation_name)

    async def _acquire_control(self, action: str, reason: str) -> bool:
        if self._coordinator is None or self._has_control():
            return True
        allowed, denial = await self._coordinator.request_charger_action(
            automation_name=self._automation_name,
            action=action,
            reason=reason,
            priority=PRIORITY_NIGHT_CHARGE,
        )
        if not allowed:
            self.logger.info(f"Coordinator denied Night Smart Charge: {denial}")
        return bool(allowed)

    def _release_control(self, reason: str) -> None:
        if self._coordinator is not None:
            self._coordinator.release_control(self._automation_name, reason)

    @staticmethod
    def _succeeded(result) -> bool:
        if hasattr(result, "success"):
            return bool(result.success)
        return bool(result)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _entity(self, key: str) -> str | None:
        if self._runtime_data is None:
            return None
        return self._runtime_data.get_entity_id(key)

    def _publish(self, key: str, value, attributes: dict | None = None) -> None:
        if self._runtime_data is None:
            return
        entity = self._runtime_data.get_entity(key)
        if entity is None or not hasattr(entity, "async_publish"):
            return
        if getattr(entity, "hass", None) is None:
            return
        try:
            self.hass.async_create_task(entity.async_publish(value, attributes))
        except RuntimeError:
            return

    async def async_setup(self) -> None:
        self.logger.info(
            "Planned Night Smart Charge: price sensor "
            f"{self._price_entity or 'not configured'}"
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_interval, timedelta(minutes=1)
            )
        )
        # Start right at the hour boundary instead of up to a minute late.
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._async_interval, minute=0, second=2
            )
        )

        watched = [
            entity
            for entity in (self._status_entity, self._soc_entity)
            if entity
        ]
        if self._price_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self._price_entity], self._async_prices_changed
                )
            )
        for key in (
            HELPER_NIGHT_CHARGE_ENABLED_SUFFIX,
            HELPER_NIGHT_CHARGE_AMPERAGE_SUFFIX,
            HELPER_CAR_READY_TIME_SUFFIX,
        ):
            entity = self._entity(key)
            if entity:
                watched.append(entity)
        for day in WEEKDAY_KEYS:
            for key in (
                f"{CAR_READY_TIME_DAY_SUFFIX_PREFIX}{day}",
                f"evsc_car_ready_{day}",
                f"evsc_ev_min_soc_{day}",
            ):
                entity = self._entity(key)
                if entity:
                    watched.append(entity)
        if watched:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, watched, self._async_input_changed
                )
            )
        await self._async_tick(dt_util.now())

    async def async_remove(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ------------------------------------------------------------------
    # Public interface (same as the old NightSmartCharge)
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        entity = self._entity(HELPER_NIGHT_CHARGE_ENABLED_SUFFIX)
        if not entity:
            return False
        return state_helper.get_bool(self.hass, entity)

    def is_active(self) -> bool:
        return self._active

    def get_active_mode(self) -> str:
        return self._active_mode

    def get_plan(self) -> NightPlan | None:
        return self._last_plan

    async def async_request_immediate_check(self, reason: str = "") -> None:
        if reason:
            self.logger.info(f"Night Smart Charge check requested: {reason}")
        await self._async_tick(dt_util.now())

    async def async_pause_for_external_override(self, reason: str = "") -> None:
        """Another override (Boost) took the charger: stand down."""
        if not self._active:
            return
        self.logger.info(f"Night Smart Charge paused: {reason or 'external override'}")
        self._release_control(reason or "External override")
        self._set_inactive()

    async def async_try_handover_from_solar_surplus(self, reason: str = "") -> bool:
        """Sunset handover: accept only when the current hour is planned."""
        if self._boost_charge is not None and self._boost_charge.is_active():
            return False
        await self._async_tick(dt_util.now())
        return self._active

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def _night_amps(self) -> int:
        entity = self._entity(HELPER_NIGHT_CHARGE_AMPERAGE_SUFFIX)
        value = state_helper.get_int(self.hass, entity, None) if entity else None
        return int(value) if value else DEFAULT_NIGHT_CHARGE_AMPERAGE

    def _power_kw(self) -> float:
        return self._night_amps() * self._power_model.effective_voltage / 1000.0

    def _capacity_kwh(self) -> float:
        try:
            return float(self.config.get(CONF_BATTERY_CAPACITY) or DEFAULT_BATTERY_CAPACITY)
        except (TypeError, ValueError):
            return DEFAULT_BATTERY_CAPACITY

    def _ready_time_for(self, day_key: str) -> time:
        for key in (f"{CAR_READY_TIME_DAY_SUFFIX_PREFIX}{day_key}", HELPER_CAR_READY_TIME_SUFFIX):
            entity = self._entity(key)
            if entity:
                parsed = _parse_time(state_helper.get_state(self.hass, entity))
                if parsed is not None:
                    return parsed
        return _parse_time(DEFAULT_CAR_READY_TIME)

    def _car_ready_for(self, day_key: str) -> bool:
        weekend = day_key in ("saturday", "sunday")
        default = DEFAULT_CAR_READY_WEEKEND if weekend else DEFAULT_CAR_READY_WEEKDAY
        entity = self._entity(f"evsc_car_ready_{day_key}")
        if not entity:
            return default
        state = state_helper.get_state(self.hass, entity)
        if state in _UNAVAILABLE:
            return default
        return state == "on"

    def _target_for(self, day_key: str) -> float:
        weekend = day_key in ("saturday", "sunday")
        default = DEFAULT_EV_MIN_SOC_WEEKEND if weekend else DEFAULT_EV_MIN_SOC_WEEKDAY
        entity = self._entity(f"evsc_ev_min_soc_{day_key}")
        if not entity:
            return float(default)
        value = state_helper.get_float(self.hass, entity, None)
        return float(default) if value is None else float(value)

    def _soc(self) -> float | None:
        if not self._soc_entity:
            return None
        value = state_helper.get_float(self.hass, self._soc_entity, None)
        if value is None:
            return None
        return max(0.0, min(100.0, float(value)))

    def _connected(self) -> bool:
        if not self._status_entity:
            return True
        state = state_helper.get_state(self.hass, self._status_entity)
        if state in _UNAVAILABLE:
            return False
        return not is_disconnected_status(state)

    def _next_deadline(self, now: datetime) -> tuple[datetime, str]:
        local_now = dt_util.as_local(now)
        for offset in range(0, 8):
            day = local_now.date() + timedelta(days=offset)
            key = WEEKDAY_KEYS[day.weekday()]
            deadline = datetime.combine(
                day, self._ready_time_for(key), tzinfo=dt_util.DEFAULT_TIME_ZONE
            )
            if deadline > local_now:
                return deadline, key
        fallback = local_now + timedelta(hours=12)
        return fallback, WEEKDAY_KEYS[fallback.weekday()]

    def _window_start(self, deadline: datetime) -> datetime:
        """Return the sunset before the deadline."""
        sunset = self._astral.get_sunset(deadline)
        if sunset is not None and sunset < deadline:
            return dt_util.as_local(sunset)
        sunset = self._astral.get_sunset(deadline - timedelta(days=1))
        if sunset is not None and sunset < deadline:
            return dt_util.as_local(sunset)
        return deadline - timedelta(hours=12)

    async def _refresh_prices(self, now: datetime) -> None:
        if not self._price_entity:
            self._prices = {}
            return
        stale = (
            self._prices_fetched_at is None
            or (now - self._prices_fetched_at).total_seconds()
            >= NIGHT_PLAN_PRICE_REFRESH_SECONDS
        )
        if not (self._prices_dirty or stale):
            return
        try:
            self._prices = await async_read_hourly_prices(self.hass, self._price_entity)
        except Exception as err:  # noqa: BLE001 - never break the tick on prices
            self.logger.warning(f"Could not read prices: {err}")
        self._prices_fetched_at = now
        self._prices_dirty = False

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @callback
    async def _async_interval(self, now) -> None:
        await self._async_tick(dt_util.now())

    @callback
    async def _async_prices_changed(self, event) -> None:
        self._prices_dirty = True
        await self._async_tick(dt_util.now())

    @callback
    async def _async_input_changed(self, event) -> None:
        await self._async_tick(dt_util.now())

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _async_tick(self, now: datetime) -> None:
        if self._tick_running:
            return
        self._tick_running = True
        try:
            await self._evaluate(now)
        except Exception as err:  # noqa: BLE001 - keep the loop alive
            self.logger.error(f"Night Smart Charge evaluation failed: {err}")
        finally:
            self._tick_running = False

    async def _evaluate(self, now: datetime) -> None:
        await self._refresh_prices(now)

        enabled = self.is_enabled()
        deadline, day_key = self._next_deadline(now)
        window_start = self._window_start(deadline)
        car_ready = self._car_ready_for(day_key)
        connected = self._connected()
        soc = self._soc()
        target = self._target_for(day_key)

        if self._plan_deadline != deadline:
            self._plan_deadline = deadline
            self._committed.clear()
            self._completed_deadline = None
        if not connected:
            self._committed.clear()
            self._completed_deadline = None
        if connected and not self._was_connected:
            self.logger.info(
                f"Car connected: planning to {target:.0f}% by "
                f"{deadline.strftime('%a %H:%M')}"
            )
        self._was_connected = connected

        current_hour = hour_start(now)
        # The latch only holds for the target it was set for: raising the
        # target re-opens the plan, a 1 % drop after a finished charge does not.
        if self._completed_deadline == deadline and (
            self._completed_target is None or target > self._completed_target
        ):
            self._completed_deadline = None
            self._completed_target = None
        if soc is not None and soc >= target:
            if self._completed_deadline != deadline and connected:
                self.logger.info(f"EV target reached ({soc:.0f}% >= {target:.0f}%)")
            self._completed_deadline = deadline
            self._completed_target = target
        completed = self._completed_deadline == deadline

        needed_kwh = None
        if connected and car_ready and not completed:
            needed_kwh = energy_needed_kwh(
                soc, target, self._capacity_kwh(), NIGHT_PLAN_CHARGE_EFFICIENCY
            )
        committed = sorted(self._committed)
        plan = build_plan(
            window_start=window_start,
            deadline=deadline,
            prices=self._prices,
            cheapest_count=NIGHT_PLAN_CHEAPEST_HOURS,
            needed_kwh=needed_kwh if connected and car_ready and not completed else 0.0,
            power_kw=self._power_kw(),
            committed=committed,
            earliest=now,
        )
        if connected and car_ready and not completed and needed_kwh is None:
            # SOC unknown: fall back to the cheapest hours that are left.
            plan.planned = [
                hour for hour in plan.cheapest if hour + timedelta(hours=1) > now
            ]
            plan.needed_hours = None
            plan.needed_kwh = None
        if not (connected and car_ready and not completed):
            plan.planned = []
            plan.needed_hours = 0 if completed else None
            plan.needed_kwh = 0.0 if completed else None
        self._last_plan = plan

        in_window = window_start - timedelta(hours=1) < now < deadline
        should_charge = (
            enabled
            and connected
            and car_ready
            and not completed
            and in_window
            and current_hour in plan.planned
        )

        if should_charge and not self._active:
            await self._start(current_hour, plan)
        elif self._active and not should_charge:
            if not enabled:
                reason = "Night Smart Charge disabled"
            elif not connected:
                reason = "Car disconnected"
            elif completed:
                reason = "EV target reached"
            elif not in_window:
                reason = "Car-ready deadline reached"
            else:
                reason = "Planned hour ended"
            await self._stop(reason)
        elif self._active and not self._has_control():
            self.logger.warning("Night Smart Charge lost control of the charger")
            self._set_inactive()

        self._publish_plan(
            now,
            plan,
            enabled=enabled,
            car_ready=car_ready,
            connected=connected,
            completed=completed,
            soc=soc,
            target=target,
            day_key=day_key,
        )

    async def _start(self, current_hour: datetime, plan: NightPlan) -> None:
        amps = self._night_amps()
        reason = (
            f"Planned night charge {current_hour.strftime('%H:%M')} "
            f"({len(plan.planned)} h planned)"
        )
        if not await self._acquire_control("turn_on", reason):
            return
        result = await self.charger_controller.start_charger(amps, reason)
        if not self._succeeded(result):
            self.logger.warning("Planned night charge could not start the charger")
            self._release_control("Start failed")
            return
        self._active = True
        self._committed.add(current_hour)
        self._active_mode = NIGHT_CHARGE_MODE_GRID
        self.logger.info(f"⚡ {reason} at {amps} A")

    async def _stop(self, reason: str) -> None:
        if self._has_control():
            result = await self.charger_controller.stop_charger(reason)
            if not self._succeeded(result):
                self.logger.warning(f"Stop failed: {reason}")
        self._release_control(reason)
        self._set_inactive()
        self.logger.info(f"Night Smart Charge stopped: {reason}")

    def _set_inactive(self) -> None:
        self._active = False
        self._active_mode = NIGHT_CHARGE_MODE_IDLE

    def _publish_plan(
        self,
        now: datetime,
        plan: NightPlan,
        *,
        enabled: bool,
        car_ready: bool,
        connected: bool,
        completed: bool,
        soc: float | None,
        target: float,
        day_key: str,
    ) -> None:
        if not enabled:
            status = STATUS_DISABLED
        elif self._active:
            status = STATUS_CHARGING
        elif not car_ready:
            status = STATUS_CAR_READY_OFF
        elif completed:
            status = STATUS_TARGET_REACHED
        elif not connected:
            status = STATUS_WAITING_FOR_CAR
        elif not plan.prices:
            status = STATUS_NO_PRICES
        else:
            status = STATUS_PLANNED

        current_hour = hour_start(now)
        upcoming = [hour for hour in plan.planned if hour >= current_hour]
        unit = None
        if self._price_entity:
            state = self.hass.states.get(self._price_entity)
            if state is not None:
                unit = state.attributes.get("unit_of_measurement")
        prices = [
            {"start": hour.isoformat(), "price": plan.prices.get(hour)}
            for hour in plan.hours
        ]
        cheapest_prices = [plan.prices[hour] for hour in plan.cheapest if hour in plan.prices]
        attributes = {
            "window_start": plan.window_start.isoformat(),
            "deadline": plan.deadline.isoformat(),
            "deadline_day": day_key,
            "car_ready": car_ready,
            "connected": connected,
            "soc": soc,
            "target_soc": target,
            "needed_kwh": plan.needed_kwh,
            "needed_hours": plan.needed_hours,
            "power_kw": round(self._power_kw(), 2),
            "prices": prices,
            "cheapest": [hour.isoformat() for hour in plan.cheapest],
            "cheapest_average": (
                round(sum(cheapest_prices) / len(cheapest_prices), 4)
                if cheapest_prices
                else None
            ),
            "planned": [hour.isoformat() for hour in plan.planned],
            "next_charge": upcoming[0].isoformat() if upcoming else None,
            "price_unit": unit,
            "price_sensor": self._price_entity,
            "active": self._active,
        }
        self._publish(HELPER_NIGHT_CHARGE_PLAN_SUFFIX, status, attributes)
