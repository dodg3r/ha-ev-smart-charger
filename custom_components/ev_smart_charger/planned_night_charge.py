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

from .charge_planner import NightPlan, build_plan, energy_needed_kwh, window_hours
from .const import (
    NIGHT_DISCONNECT_GRACE_SECONDS,
    NIGHT_START_RETRY_SECONDS,
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
    NIGHT_PLAN_STARTUP_GRACE_SECONDS,
    NIGHT_PLAN_CHARGE_EFFICIENCY,
    NIGHT_PLAN_CHEAPEST_HOURS,
    NIGHT_PLAN_PRICE_REFRESH_SECONDS,
    PRIORITY_NIGHT_CHARGE,
    WEEKDAY_KEYS,
)
from .power_model import ChargingModel, is_disconnected_status
from .price_source import (
    PRICE_SOURCE_NOT_MAPPED,
    PRICE_SOURCE_UNSUPPORTED,
    async_read_prices,
    hour_start,
)
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
STATUS_WAITING_FOR_PRICES = "waiting_for_prices"


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
        self._price_source: str | None = None
        self._prices_fetched_at: datetime | None = None
        self._prices_dirty = True
        self._active = False
        self._active_mode_value = NIGHT_CHARGE_MODE_IDLE
        # v2.25.1: set in async_setup; no planning on missing prices right
        # after a restart (the price integration may not be ready yet).
        self._setup_at: datetime | None = None
        self._committed: set[datetime] = set()
        self._applied_amps: int | None = None
        self._plan_deadline: datetime | None = None
        self._completed_deadline: datetime | None = None
        # Target the latch was set for: raising the target re-opens the plan.
        self._completed_target: float | None = None
        self._was_connected = False
        self._disconnected_since: datetime | None = None
        self._start_retry_at: datetime | None = None
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
        self._setup_at = dt_util.now()
        # v2.24.1: start the session sensor from a known state.
        self._publish(HELPER_NIGHT_SESSION_STATE_SUFFIX, self._active_mode_value)
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

    def _next_deadline(self, now: datetime) -> tuple[datetime | None, str | None]:
        """Return the next car-ready deadline of a day with car ready ON.

        Days with car ready OFF are skipped (v2.21.0), so with only Thursday
        ticked a Tuesday evening plans straight towards Thursday morning.
        Returns (None, None) when no day in the coming week is ticked.
        """
        local_now = dt_util.as_local(now)
        for offset in range(0, 8):
            day = local_now.date() + timedelta(days=offset)
            key = WEEKDAY_KEYS[day.weekday()]
            deadline = datetime.combine(
                day, self._ready_time_for(key), tzinfo=dt_util.DEFAULT_TIME_ZONE
            )
            if deadline > local_now and self._car_ready_for(key):
                return deadline, key
        return None, None

    def _night_hours(self, hours: list[datetime], deadline: datetime | None) -> set[datetime]:
        """Hours that may be planned: between sunset and sunrise, plus the
        morning of the deadline day up to the deadline."""
        cache: dict = {}

        def sun(day):
            if day not in cache:
                ref = datetime.combine(day, time(12), tzinfo=dt_util.DEFAULT_TIME_ZONE)
                rise = self._astral.get_sunrise(ref)
                sset = self._astral.get_sunset(ref)
                cache[day] = (
                    dt_util.as_local(rise) if rise else None,
                    dt_util.as_local(sset) if sset else None,
                )
            return cache[day]

        allowed: set[datetime] = set()
        for hour in hours:
            mid = hour + timedelta(minutes=30)
            rise, sset = sun(mid.date())
            if rise is None or sset is None:
                allowed.add(hour)
                continue
            if mid < rise or mid >= sset:
                allowed.add(hour)
            elif deadline is not None and mid.date() == deadline.date() and hour < deadline:
                allowed.add(hour)
        return allowed

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
            self._price_source = PRICE_SOURCE_NOT_MAPPED
            return
        # v2.23.0: without any prices (e.g. the price integration was not
        # ready at startup) retry every minute instead of every 10 minutes.
        refresh = 60 if not self._prices else NIGHT_PLAN_PRICE_REFRESH_SECONDS
        stale = (
            self._prices_fetched_at is None
            or (now - self._prices_fetched_at).total_seconds() >= refresh
        )
        if not (self._prices_dirty or stale):
            return
        try:
            self._prices, source = await async_read_prices(
                self.hass, self._price_entity
            )
            if source == PRICE_SOURCE_UNSUPPORTED and self._price_source != source:
                state = self.hass.states.get(self._price_entity)
                keys = sorted((state.attributes or {}).keys()) if state else []
                self.logger.warning(
                    f"Price sensor {self._price_entity} has no price list "
                    f"(raw_today/today) and is not a core Nord Pool sensor. "
                    f"Attributes: {keys}"
                )
            self._price_source = source
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
        connected = self._connected()
        # v2.26.0: a running session survives a short "not connected" blink
        # of the charger status (Growatt); stop only when it lasts.
        if connected:
            self._disconnected_since = None
        else:
            if self._disconnected_since is None:
                self._disconnected_since = now
            if (
                self._active
                and (now - self._disconnected_since).total_seconds()
                < NIGHT_DISCONNECT_GRACE_SECONDS
            ):
                connected = True
        soc = self._soc()
        current_hour = hour_start(now)

        if self._plan_deadline != deadline:
            self._plan_deadline = deadline
            self._committed.clear()
            self._completed_deadline = None
            self._completed_target = None
        if not connected:
            self._committed.clear()
            self._completed_deadline = None
        self._was_connected = connected

        if deadline is None:
            # Overview: no car-ready day ahead. Show every hour with a known
            # price from now on and the cheapest ones; never charge.
            last = max((h for h in self._prices if h >= current_hour), default=None)
            end = last + timedelta(hours=1) if last else current_hour + timedelta(hours=1)
            plan = build_plan(
                window_start=current_hour,
                deadline=end,
                prices=self._prices,
                cheapest_count=NIGHT_PLAN_CHEAPEST_HOURS,
                needed_kwh=None,
                power_kw=self._power_kw(),
            )
            plan.planned = []
            self._last_plan = plan
            if self._active:
                await self._stop("No car-ready day")
            self._publish_plan(
                now, plan, mode="overview", enabled=enabled, connected=connected,
                completed=False, plan_active=False, soc=soc, target=None,
                day_key=None, allowed=None,
            )
            return

        target = self._target_for(day_key)
        window_start = current_hour
        sunset_before = self._window_start(deadline)
        hours = window_hours(window_start, deadline)
        allowed = self._night_hours(hours, deadline)

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

        # The plan becomes live once the prices reach the deadline, or at the
        # sunset before the deadline at the latest (prices missing: plan with
        # what is known, unknown hours last).
        covered = all(hour in self._prices for hour in hours)
        plan_active = covered or now >= sunset_before or bool(self._committed)
        # v2.25.1: right after a restart the price integration is often not
        # ready; do not fall back to a plan without any prices then, or the
        # current hour is picked and charging starts at once.
        if (
            not self._prices
            and not self._committed
            and self._setup_at is not None
            and now - self._setup_at < timedelta(seconds=NIGHT_PLAN_STARTUP_GRACE_SECONDS)
        ):
            plan_active = False

        wants_charge = connected and not completed
        needed_kwh = None
        if wants_charge:
            needed_kwh = energy_needed_kwh(
                soc, target, self._capacity_kwh(), NIGHT_PLAN_CHARGE_EFFICIENCY
            )
        plan = build_plan(
            window_start=window_start,
            deadline=deadline,
            prices=self._prices,
            cheapest_count=NIGHT_PLAN_CHEAPEST_HOURS,
            needed_kwh=needed_kwh if wants_charge else 0.0,
            power_kw=self._power_kw(),
            committed=sorted(self._committed),
            earliest=now,
            allowed=allowed,
        )
        if wants_charge and needed_kwh is None:
            # SOC unknown: fall back to the cheapest hours that are left.
            plan.planned = [
                hour for hour in plan.cheapest if hour + timedelta(hours=1) > now
            ]
            plan.needed_hours = None
            plan.needed_kwh = None
        if not wants_charge:
            plan.planned = []
            plan.needed_hours = 0 if completed else None
            plan.needed_kwh = 0.0 if completed else None
        if not plan_active:
            plan.planned = []
        self._last_plan = plan

        should_charge = (
            enabled
            and wants_charge
            and plan_active
            and now < deadline
            and current_hour in plan.planned
        )

        if should_charge and not self._active:
            if self._may_try_start(now):
                await self._start(now, current_hour, plan)
        elif self._active and not should_charge:
            if not enabled:
                reason = "Night Smart Charge disabled"
            elif not connected:
                reason = "Car disconnected"
            elif completed:
                reason = "EV target reached"
            elif now >= deadline:
                reason = "Car-ready deadline reached"
            else:
                reason = "Planned hour ended"
            await self._stop(reason)
        elif self._active and not self._has_control():
            self.logger.warning("Night Smart Charge lost control of the charger")
            self._set_inactive()
        elif self._active and should_charge:
            await self._apply_amps()

        self._publish_plan(
            now, plan, mode="plan", enabled=enabled, connected=connected,
            completed=completed, plan_active=plan_active, soc=soc,
            target=target, day_key=day_key, allowed=allowed,
        )

    def _may_try_start(self, now: datetime) -> bool:
        """v2.26.0: no start while the main fuse leaves no room, and wait
        after a failed start instead of retrying on every tick."""
        if self._start_retry_at is not None and now < self._start_retry_at:
            return False
        ceiling = getattr(self.charger_controller, "fuse_ceiling", None)
        if callable(ceiling):
            try:
                value = ceiling()
            except Exception:  # never block on a diagnostic read
                value = None
            if value == 0:
                self.logger.debug("Planned night charge waits: main fuse has no room")
                return False
        return True

    async def _start(
        self, now: datetime, current_hour: datetime, plan: NightPlan
    ) -> None:
        amps = self._night_amps()
        reason = (
            f"Planned night charge {current_hour.strftime('%H:%M')} "
            f"({len(plan.planned)} h planned)"
        )
        if not await self._acquire_control("turn_on", reason):
            return
        result = await self.charger_controller.start_charger(amps, reason)
        if not self._succeeded(result):
            self.logger.warning(
                "Planned night charge could not start the charger - "
                f"retrying in {NIGHT_START_RETRY_SECONDS} s"
            )
            self._start_retry_at = now + timedelta(seconds=NIGHT_START_RETRY_SECONDS)
            self._release_control("Start failed")
            return
        self._start_retry_at = None
        self._active = True
        self._applied_amps = amps
        self._committed.add(current_hour)
        self._active_mode = NIGHT_CHARGE_MODE_GRID
        self.logger.info(f"⚡ {reason} at {amps} A")

    async def _apply_amps(self) -> None:
        """v2.25.3: follow a changed night amperage during a running session.

        The controller caps the value to the main fuse and remembers it as the
        request, so the fuse guard ramps up towards it when there is room.
        """
        amps = self._night_amps()
        if amps == self._applied_amps:
            return
        previous = self._applied_amps
        self._applied_amps = amps
        result = await self.charger_controller.set_amperage(
            amps, f"Night charge amperage changed {previous} A -> {amps} A"
        )
        if not self._succeeded(result):
            self.logger.warning("Could not apply the new night charge amperage")
            self._applied_amps = previous
            return
        self.logger.info(f"⚡ Night charge amperage {previous} A -> {amps} A")

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
        self._applied_amps = None
        self._active_mode = NIGHT_CHARGE_MODE_IDLE

    def _publish_plan(
        self,
        now: datetime,
        plan: NightPlan,
        *,
        mode: str,
        enabled: bool,
        connected: bool,
        completed: bool,
        plan_active: bool,
        soc: float | None,
        target: float | None,
        day_key: str | None,
        allowed: set[datetime] | None,
    ) -> None:
        if not enabled:
            status = STATUS_DISABLED
        elif self._active:
            status = STATUS_CHARGING
        elif mode == "overview":
            status = STATUS_CAR_READY_OFF
        elif completed:
            status = STATUS_TARGET_REACHED
        elif not connected:
            status = STATUS_WAITING_FOR_CAR
        elif not plan.prices:
            status = STATUS_NO_PRICES
        elif not plan_active:
            status = STATUS_WAITING_FOR_PRICES
        else:
            status = STATUS_PLANNED

        current_hour = hour_start(now)
        upcoming = [hour for hour in plan.planned if hour >= current_hour]
        unit = None
        if self._price_entity:
            state = self.hass.states.get(self._price_entity)
            if state is not None:
                unit = state.attributes.get("unit_of_measurement")
        known = [hour for hour in plan.prices]
        # v2.21.1: the chart shows the known prices (at least 12 hours and
        # every planned hour), not a week of empty bars when the deadline is
        # days away.
        display_end = current_hour + timedelta(hours=12)
        if known:
            display_end = max(display_end, max(known) + timedelta(hours=1))
        if plan.planned:
            display_end = max(display_end, max(plan.planned) + timedelta(hours=1))
        display_end = min(display_end, plan.deadline)
        shown = [hour for hour in plan.hours if hour < display_end]
        prices = [
            {"start": hour.isoformat(), "price": plan.prices.get(hour)}
            for hour in shown
        ]
        cheapest_prices = [plan.prices[hour] for hour in plan.cheapest if hour in plan.prices]
        attributes = {
            "mode": mode,
            "plan_active": plan_active,
            "window_start": plan.window_start.isoformat(),
            "window_end": plan.deadline.isoformat(),
            "deadline": plan.deadline.isoformat() if mode == "plan" else None,
            "deadline_day": day_key,
            "car_ready": mode == "plan",
            "connected": connected,
            "soc": soc,
            "target_soc": target,
            "needed_kwh": plan.needed_kwh,
            "needed_hours": plan.needed_hours,
            "power_kw": round(self._power_kw(), 2),
            "prices": prices,
            "prices_truncated": len(shown) < len(plan.hours),
            "price_source": self._price_source,
            "prices_until": (
                (max(known) + timedelta(hours=1)).isoformat() if known else None
            ),
            "plannable": (
                [hour.isoformat() for hour in shown if hour in allowed]
                if allowed is not None
                else None
            ),
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
