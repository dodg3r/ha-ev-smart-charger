"""Charging power model: single source of truth for phase mode + charger model.

Built once from the config entry data and stored on ``EVSCRuntimeData.power_model``
(see ``__init__.py``). Encapsulates the only two things three-phase / charger-model
support changes in the charging logic:

- the watt→amp conversion voltage (``effective_voltage`` = phase_count × 230 V), and
- the amperage level set (``amp_levels``: discrete Tuya levels vs 1 A generic steps).

Single-phase + Tuya (the defaults) reproduce the pre-v2.0.0 behaviour exactly:
``effective_voltage`` = 230, ``amp_levels`` = CHARGER_AMP_LEVELS, and the power
readers return the single configured sensor value.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant

from .const import (
    CHARGER_STATUS_END,
    CHARGER_STATUS_FREE,
    CHARGER_STATUS_WAIT,
    CHARGING_POWER_DRAWING_FLOOR_W,
    CONF_BATTERY_POWER,
    CONF_CHARGING_POWER,
    CONF_CHARGING_POWER_L2,
    CONF_CHARGING_POWER_L3,
    CONF_EV_CHARGER_STATUS,
    CONF_EV_SOC_LIMIT,
    CONF_FV_PRODUCTION,
    CONF_FV_PRODUCTION_L2,
    CONF_FV_PRODUCTION_L3,
    CONF_GRID_AVAILABLE,
    CONF_GRID_IMPORT,
    CONF_GRID_IMPORT_L2,
    CONF_GRID_IMPORT_L3,
    CONF_HOME_CONSUMPTION,
    CONF_HOME_CONSUMPTION_L2,
    CONF_HOME_CONSUMPTION_L3,
    CONF_FUSE_CHARGER_CURRENT,
    FUSE_CHARGER_CURRENT_KEYS,
    FUSE_CHARGER_PHASE_ATTRIBUTES,
    FUSE_GRID_EXPORT_KEYS,
    FUSE_GRID_IMPORT_KEYS,
    FUSE_GRID_VOLTAGE_KEYS,
    VOLTAGE_EU,
    get_amp_levels,
    get_charger_model,
    get_effective_voltage,
    get_phase_count,
    has_meter_import_power,
    is_three_phase,
)
from .utils.state_helper import get_float, get_state

# States that mean "no usable numeric reading" for an otherwise-mapped sensor.
_UNAVAILABLE_STATES = (None, "unknown", "unavailable")
# Statuses that mean "not actively drawing" for the textual fallback. Anything
# NOT in this set (or the unavailable set) is treated as charging — a TOLERANT
# blocklist, matching the frontend `_isDrawingNow`, so non-Tuya wallboxes that
# report a brand-specific charging string (e.g. "Charging") still register.
_IDLE_OR_DONE_STATUSES = (CHARGER_STATUS_FREE, CHARGER_STATUS_END, CHARGER_STATUS_WAIT)

# ---------------------------------------------------------------------------
# v2.9.1 — brand-vocabulary status classifiers (SSOT)
#
# Non-Tuya wallboxes report brand-specific status strings. Two lifecycle
# questions keep coming up across components and were previously answered with
# exact Tuya-string comparisons (the root cause of the 2026-07-19 incident,
# where OCPP-style 'available' — which means NO EV connected — passed a
# "connected" gate that only rejected 'charger_free'):
#
#   1. "Is the cable disconnected?"  -> is_disconnected_status()
#   2. "Has the charge completed?"   -> is_charge_complete_status()
#
# Both are deliberately CONSERVATIVE allowlists of unambiguous synonyms
# (case-insensitive): an unknown brand string classifies as "connected / not
# complete", which is the safe failure mode — a wrong 'disconnected' verdict
# would block night charging entirely, while a wrong 'connected' verdict at
# worst runs a session with no draw (caught by the measured-power checks).
# OCPP reference: 'Available' = connector not occupied; 'Finishing' = session
# ended.
# ---------------------------------------------------------------------------
_DISCONNECTED_STATUSES = frozenset(
    {
        CHARGER_STATUS_FREE,  # Tuya
        "available",  # OCPP: connector not occupied
        "disconnected",
        "unplugged",
        "not_connected",
        "not connected",
        "no_car",
        "no car",
        "no_vehicle",
        "car_not_connected",
    }
)

_CHARGE_COMPLETE_STATUSES = frozenset(
    {
        CHARGER_STATUS_END,  # Tuya
        "charged",
        "complete",
        "completed",
        "finished",
        "finishing",  # OCPP: session wrap-up
        "full",
        "charge_complete",
        "charging_complete",
    }
)


# v2.29.1: statuses that mean a charging session (OCPP transaction) is open,
# whether or not the car draws right now. 'suspended_ev' = the car paused the
# charge itself (full, or its own charge limit). A start command is pointless
# then: OCPP chargers reject it ("a transaction is already active").
_SESSION_OPEN_STATUSES = frozenset(
    {
        "charger_charging",  # Tuya
        "charging",
        "suspended_ev",
        "suspendedev",
        "suspended_evse",
        "suspendedevse",
    }
)

_EV_PAUSED_STATUSES = frozenset({"suspended_ev", "suspendedev"})


def _status_key(status: str | None) -> str:
    return str(status).strip().lower().replace(" ", "_").replace("-", "_")


def is_session_open_status(status: str | None) -> bool:
    """True when the status says a charging session is open (drawing or paused)."""
    if not status:
        return False
    key = _status_key(status)
    return key in _SESSION_OPEN_STATUSES or key.replace("_", "") in _SESSION_OPEN_STATUSES


def is_ev_paused_status(status: str | None) -> bool:
    """True when the car itself paused the session (full or its own limit)."""
    if not status:
        return False
    key = _status_key(status)
    return key in _EV_PAUSED_STATUSES or key.replace("_", "") in _EV_PAUSED_STATUSES


# v2.30.0: statuses that mean "no session is open" (OCPP vocabulary). Used to
# skip a stop press that the charger would only reject.
_SESSION_IDLE_STATUSES = frozenset({"preparing", "idle", "finishing", "available"})


def is_session_closed_status(status: str | None) -> bool:
    """True when the status clearly says no charging session is open.

    Unknown / unavailable statuses return False (we cannot tell).
    """
    if not status:
        return False
    key = _status_key(status)
    if key in ("unknown", "unavailable", "none"):
        return False
    return (
        key in _SESSION_IDLE_STATUSES
        or is_disconnected_status(status)
        or is_charge_complete_status(status)
    )


def is_disconnected_status(status: str | None) -> bool:
    """True when the textual charger status unambiguously means 'no EV plugged'.

    Tolerant of brand vocabularies (case/whitespace-insensitive). Unknown
    strings return False (treated as connected — the safe default).
    """
    if not status:
        return False
    return str(status).strip().lower() in _DISCONNECTED_STATUSES


def is_charge_complete_status(status: str | None) -> bool:
    """True when the textual charger status unambiguously means 'charge done'.

    Tolerant of brand vocabularies (case/whitespace-insensitive). Unknown
    strings return False.
    """
    if not status:
        return False
    return str(status).strip().lower() in _CHARGE_COMPLETE_STATUSES


# ---------------------------------------------------------------------------
# v2.30.0 — the car's own charge limit counts as full
# ---------------------------------------------------------------------------
def read_ev_soc_limit(hass: HomeAssistant, config) -> float | None:
    """The car's charge limit in %, or None when unmapped / unreadable.

    Values outside 1..100 are ignored (None), so a broken reading never
    blocks charging.
    """
    entity = (config or {}).get(CONF_EV_SOC_LIMIT)
    if not entity:
        return None
    # Quiet read: a car integration that is offline must not log every tick.
    value = _parse_number(get_state(hass, entity))
    if value is None or value <= 0 or value > 100:
        return None
    return float(value)


def cap_ev_target(target, limit: float | None):
    """Cap an EV target to the car's limit (unchanged when there is none)."""
    if target is None or limit is None:
        return target
    if target > limit:
        return type(target)(limit) if isinstance(target, int) else float(limit)
    return target


def is_ev_full(soc: float | None, limit: float | None) -> bool:
    """True when the EV SOC is at or above the car's limit (or 100 %)."""
    if soc is None:
        return False
    return soc >= (limit if limit is not None else 100.0)

# Per-quantity config keys, ordered L1, L2, L3. L1 reuses the existing single-phase
# key so single-phase installs read exactly the same entity as before.
_PRODUCTION_KEYS = (CONF_FV_PRODUCTION, CONF_FV_PRODUCTION_L2, CONF_FV_PRODUCTION_L3)
_CONSUMPTION_KEYS = (CONF_HOME_CONSUMPTION, CONF_HOME_CONSUMPTION_L2, CONF_HOME_CONSUMPTION_L3)
_GRID_IMPORT_KEYS = (CONF_GRID_IMPORT, CONF_GRID_IMPORT_L2, CONF_GRID_IMPORT_L3)
# v2.2.0 — measured charging power. L1 is a NEW key (no single-phase predecessor),
# so _entities_for returns [] in single-phase when CONF_CHARGING_POWER is unmapped.
_CHARGING_POWER_KEYS = (CONF_CHARGING_POWER, CONF_CHARGING_POWER_L2, CONF_CHARGING_POWER_L3)


# ---------------------------------------------------------------------------
# v2.18.0 — raw readers shared with the fuse guard
# ---------------------------------------------------------------------------
def _parse_number(value) -> float | None:
    if value in (None, "", "unknown", "unavailable", "none"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_power_w(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A power sensor in watts (kW normalised), or None when unreadable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    value = _parse_number(state.state)
    if value is None:
        return None
    unit = str(state.attributes.get("unit_of_measurement") or "").lower()
    if unit == "kw":
        value *= 1000.0
    return value


def read_current_a(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """A current sensor in amperes (mA normalised), or None when unreadable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    value = _parse_number(state.state)
    if value is None:
        return None
    if str(state.attributes.get("unit_of_measurement") or "") == "mA":
        value /= 1000.0
    return value


def read_number(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Any numeric sensor, or None when unreadable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return None if state is None else _parse_number(state.state)


def read_charger_phase_currents(
    hass: HomeAssistant, config: dict, phase_count: int
) -> list[float | None]:
    """Per-phase charger current (A) from the meter-step mapping.

    Dedicated per-phase sensors win; otherwise one sensor whose state is the
    total and whose L1/L2/L3 attributes (if any) hold the phases. Used by the
    fuse guard and by the derived charging power.
    """
    dedicated = [config.get(key) for key in FUSE_CHARGER_CURRENT_KEYS[:phase_count]]
    if any(dedicated):
        return [read_current_a(hass, entity) for entity in dedicated]

    total_entity = config.get(CONF_FUSE_CHARGER_CURRENT)
    if not total_entity:
        return [None] * phase_count
    state = hass.states.get(total_entity)
    if state is None:
        return [None] * phase_count
    total = _parse_number(state.state)

    # Charger reports no current at all: per-phase attributes are often
    # left at their last value, so trust the total and report zero.
    if total is not None and abs(total) < 0.1:
        return [0.0] * phase_count

    values: list[float | None] = []
    for candidates in FUSE_CHARGER_PHASE_ATTRIBUTES[:phase_count]:
        value = None
        for name in candidates:
            if name in state.attributes:
                value = _parse_number(state.attributes.get(name))
                break
        values.append(value)

    if phase_count == 1 and values[0] is None:
        return [total]
    if all(v is None for v in values) and total is not None:
        # No per-phase attributes: assume the load is spread evenly.
        return [total / phase_count] * phase_count
    return values


def _entities_for(config: dict, keys: tuple[str, ...]) -> list[str]:
    """Return the configured entity ids for a quantity.

    Single-phase → only L1 (``keys[0]``). Three-phase → L1/L2/L3, skipping any
    that are not mapped (defensive; the config flow makes L2/L3 required in
    three-phase, but a hand-edited entry might omit one).
    """
    if not is_three_phase(config):
        entity = config.get(keys[0])
        return [entity] if entity else []
    return [config[k] for k in keys if config.get(k)]


@dataclass
class ChargingModel:
    """Immutable view of phase mode + charger model derived from config."""

    phase_count: int
    effective_voltage: float
    amp_levels: list[int]
    charger_model: str
    _production_entities: list[str]
    _consumption_entities: list[str]
    _grid_import_entities: list[str]
    # v2.1.0 (issue #29) — optional signed battery-power sensor. Single (never
    # phase-summed, unlike the readers above): battery power is one inverter-level
    # aggregate, like SOC. None when the user did not map a sensor.
    _battery_power_entity: str | None = None
    # v2.2.0 — measured EV charging-power sensors ([] / [L1] / [L1,L2,L3]) and the
    # textual charger-status sensor used as the drawing-now FALLBACK. Both live on
    # the model so is_charging() is a genuine single source of truth.
    _charging_power_entities: list[str] = field(default_factory=list)
    _charger_status_entity: str | None = None
    # v2.6.0 (issue #36) — optional binary_sensor for grid availability
    # (on = grid present, off = grid lost). None when unmapped.
    _grid_available_entity: str | None = None
    # v2.18.0 — meter-step data used to DERIVE quantities the user left
    # unmapped: per-phase import / export power and voltage from the main
    # meter, and the charger current. ``_meter_import_entities`` is empty
    # unless every phase is mapped (a partial sum would be wrong).
    _meter_import_entities: list[str] = field(default_factory=list)
    _meter_export_entities: list[str | None] = field(default_factory=list)
    _meter_voltage_entities: list[str | None] = field(default_factory=list)
    _config: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_config(cls, config: dict) -> "ChargingModel":
        """Build the model from config entry data."""
        phases = get_phase_count(config)
        return cls(
            phase_count=get_phase_count(config),
            effective_voltage=get_effective_voltage(config),
            amp_levels=get_amp_levels(config),
            charger_model=get_charger_model(config),
            _production_entities=_entities_for(config, _PRODUCTION_KEYS),
            _consumption_entities=_entities_for(config, _CONSUMPTION_KEYS),
            _grid_import_entities=_entities_for(config, _GRID_IMPORT_KEYS),
            _battery_power_entity=config.get(CONF_BATTERY_POWER),
            _charging_power_entities=_entities_for(config, _CHARGING_POWER_KEYS),
            _charger_status_entity=config.get(CONF_EV_CHARGER_STATUS),
            _grid_available_entity=config.get(CONF_GRID_AVAILABLE),
            _meter_import_entities=(
                [config[k] for k in FUSE_GRID_IMPORT_KEYS[:phases]]
                if has_meter_import_power(config)
                else []
            ),
            _meter_export_entities=[config.get(k) for k in FUSE_GRID_EXPORT_KEYS[:phases]],
            _meter_voltage_entities=[config.get(k) for k in FUSE_GRID_VOLTAGE_KEYS[:phases]],
            _config=dict(config),
        )

    def is_grid_available(self, hass) -> bool | None:
        """Return grid availability from the optional binary_sensor (issue #36).

        Fail-safe: returns ``None`` when the sensor is unmapped OR its state is
        unknown/unavailable/None — callers must treat ``None`` as "don't act".
        Only an explicit ``on``/``off`` yields ``True``/``False``. This prevents
        a boot-time or inverter-integration-restart ``unavailable`` from being
        read as "grid lost" and spuriously stopping a night session.
        """
        if not self._grid_available_entity:
            return None
        state = get_state(hass, self._grid_available_entity)
        if state in _UNAVAILABLE_STATES:
            return None
        return str(state).lower() in ("on", "true", "1", "yes", "home", "present")

    # ----- entity lists (for validation / dashboard) -----
    def production_entities(self) -> list[str]:
        """Return mapped production sensors ([L1] or [L1, L2, L3])."""
        return list(self._production_entities)

    def consumption_entities(self) -> list[str]:
        """Return mapped home-consumption sensors."""
        return list(self._consumption_entities)

    def grid_import_entities(self) -> list[str]:
        """Return mapped grid-import sensors."""
        return list(self._grid_import_entities)

    # ----- v2.18.0: what is derived instead of mapped -----
    @property
    def grid_import_derived(self) -> bool:
        """Grid import is computed from the meter's import − export power."""
        return not self._grid_import_entities and bool(self._meter_import_entities)

    @property
    def charging_power_derived(self) -> bool:
        """Charging power is computed from the charger current × voltage."""
        if self._charging_power_entities:
            return False
        config = self._config
        return bool(config.get(CONF_FUSE_CHARGER_CURRENT)) or any(
            config.get(k) for k in FUSE_CHARGER_CURRENT_KEYS[: self.phase_count]
        )

    @property
    def consumption_derived(self) -> bool:
        """Home consumption is computed from production, grid and charger."""
        return (
            not self._consumption_entities
            and bool(self._grid_import_entities or self._meter_import_entities)
            and bool(self._charging_power_entities or self.charging_power_derived)
        )

    def grid_import_source_entities(self) -> list[str]:
        """Entities whose changes move the grid-import reading."""
        if self._grid_import_entities:
            return list(self._grid_import_entities)
        if self._meter_import_entities:
            return self._meter_import_entities + [
                e for e in self._meter_export_entities if e
            ]
        return []

    def labelled_power_entities(self) -> list[tuple[str, str]]:
        """Return (entity_id, label) pairs for all power sensors, for validation.

        Labels gain an "Ln" suffix only in three-phase so single-phase log
        messages are unchanged. v2.18.0: a derived grid import validates the
        meter's import / export sensors instead.
        """
        out: list[tuple[str, str]] = []
        groups = [
            ("Solar Production", self._production_entities),
            ("Home Consumption", self._consumption_entities),
            ("Grid Import", self._grid_import_entities),
        ]
        if self.grid_import_derived:
            groups.append(("Meter Import", self._meter_import_entities))
            groups.append(
                ("Meter Export", [e for e in self._meter_export_entities if e])
            )
        for base, entities in groups:
            for idx, entity in enumerate(entities):
                label = base if self.phase_count == 1 else f"{base} L{idx + 1}"
                out.append((entity, label))
        return out

    def validate_grid_import(self, hass: HomeAssistant) -> tuple[bool, str | None]:
        """Is grid import readable right now? (mapped or derived)."""
        if self._grid_import_entities:
            for entity in self._grid_import_entities:
                if read_number(hass, entity) is None:
                    return False, f"Grid Import ({entity}) unavailable"
            return True, None
        if self._meter_import_entities:
            if self._derived_grid_import(hass) is None:
                return False, "Grid Import (meter import/export) unavailable"
            return True, None
        return False, "Grid Import not configured"

    def derived_power_errors(self, hass: HomeAssistant) -> list[str]:
        """Errors for derived quantities that cannot be computed right now.

        Mapped sensors are checked through ``labelled_power_entities``; this
        covers what is derived from the charger current.
        """
        if self.consumption_derived and self._ev_power_for_balance(hass) is None:
            return [
                "Home Consumption (derived): charger current unavailable "
                "while the car may be charging"
            ]
        return []

    # ----- power readers (sum across phases) -----
    def read_production(self, hass: HomeAssistant) -> float:
        """Total PV production (W), summed across phases in three-phase mode."""
        return sum(get_float(hass, e) for e in self._production_entities)

    def read_consumption(self, hass: HomeAssistant) -> float:
        """Total home consumption (W), summed across phases.

        v2.18.0: when unmapped it is derived as
        production + grid import − EV charging power − battery charging
        (battery power signed: positive = charging), i.e. what the house
        itself uses. Surplus = production − consumption then equals what the
        EV draws now plus any export, minus any import.
        """
        if self._consumption_entities or not self.consumption_derived:
            return sum(get_float(hass, e) for e in self._consumption_entities)
        ev = self._ev_power_for_balance(hass) or 0.0
        battery = (
            get_float(hass, self._battery_power_entity, default=0.0)
            if self._battery_power_entity
            else 0.0
        )
        house = self.read_production(hass) + self.read_grid_import(hass) - ev - battery
        return max(0.0, house)

    def read_grid_import(self, hass: HomeAssistant) -> float:
        """Total grid import (W, positive = importing), summed across phases.

        v2.18.0: when unmapped, the meter's import − export power per phase.
        """
        if self._grid_import_entities or not self._meter_import_entities:
            return sum(get_float(hass, e) for e in self._grid_import_entities)
        value = self._derived_grid_import(hass)
        return 0.0 if value is None else value

    def _derived_grid_import(self, hass: HomeAssistant) -> float | None:
        total = 0.0
        for index, entity in enumerate(self._meter_import_entities):
            imported = read_power_w(hass, entity)
            if imported is None:
                return None
            export_entity = self._meter_export_entities[index]
            exported = 0.0
            if export_entity:
                exported = read_power_w(hass, export_entity)
                if exported is None:
                    return None
            total += imported - exported
        return total

    def _derived_charging_power(self, hass: HomeAssistant) -> float | None:
        currents = read_charger_phase_currents(hass, self._config, self.phase_count)
        total = 0.0
        for index, current in enumerate(currents):
            if current is None:
                return None
            voltage = read_number(hass, self._meter_voltage_entities[index]) if index < len(
                self._meter_voltage_entities
            ) else None
            if voltage is None or voltage < 100:
                voltage = VOLTAGE_EU
            total += abs(current) * voltage
        return total

    def _ev_power_for_balance(self, hass: HomeAssistant) -> float | None:
        """EV power to remove from the house balance, or None when unknown.

        Measured / derived charging power when readable. Otherwise 0 W when
        the status says the car is not drawing (an OCPP current sensor often
        goes unavailable between sessions), else None.
        """
        power = self.read_charging_power(hass)
        if power is not None:
            return power
        status = get_state(hass, self._charger_status_entity)
        if status in _UNAVAILABLE_STATES:
            return None
        if (
            status in _IDLE_OR_DONE_STATUSES
            or is_disconnected_status(status)
            or is_charge_complete_status(status)
        ):
            return 0.0
        return None

    def read_battery_discharge(self, hass: HomeAssistant) -> float | None:
        """Home-battery discharge in watts (>=0 = discharging), or None.

        Single sensor (not phase-summed). Convention: the sensor reports
        negative = discharging, positive = charging, so discharge is the
        clamped negation. Returns None when no sensor is configured — the
        explicit guard is what distinguishes "unconfigured" from a genuine 0 W
        reading (``get_float`` defaults to 0.0 for missing/unknown states).
        """
        if not self._battery_power_entity:
            return None
        return max(0.0, -get_float(hass, self._battery_power_entity, default=0.0))

    # ----- charging-state SSOT (v2.2.0) -----
    def charging_power_entities(self) -> list[str]:
        """Return mapped charging-power sensors ([] / [L1] / [L1, L2, L3])."""
        return list(self._charging_power_entities)

    def read_charging_power(self, hass: HomeAssistant) -> float | None:
        """Total measured EV charging power in watts, or None.

        Three-way, all-or-nothing (so a partial/lagging three-phase read can never
        produce a misleadingly-low sum that flips the drawing-now verdict):

        - no charging-power sensor mapped              -> None (unconfigured)
        - any *mapped* phase missing / unavailable     -> None (cannot measure)
        - every mapped phase readable                  -> float, clamped >= 0.0

        The ``max(0.0, ...)`` clamp means a reversed-sign sensor reads a flat 0 W
        and the caller falls through to the status string (the diagnostic surfaces
        the flat 0 as the user's cue to apply a ``| abs`` template fix). Returns
        None — not 0.0 — when unconfigured, so callers distinguish "no sensor"
        (use status fallback) from "genuinely 0 W" (not drawing).
        """
        if not self._charging_power_entities:
            if self.charging_power_derived:
                # v2.18.0: charger current × phase voltage (230 V fallback).
                return self._derived_charging_power(hass)
            return None
        total = 0.0
        for entity in self._charging_power_entities:
            state_obj = hass.states.get(entity)
            if state_obj is None or state_obj.state in _UNAVAILABLE_STATES:
                return None
            try:
                value = float(state_obj.state)
            except (ValueError, TypeError):
                return None
            # Normalize to watts by unit. Many HA wallbox integrations (Tuya,
            # Easee, Wallbox, go-e, …) expose charging power in kW; without this
            # the floor comparison (200 W) and the frontend (which already
            # normalizes in _powerW) would disagree, and a kW sensor would read
            # ~3.7 < floor → falsely "not charging". Unknown/missing unit →
            # assume watts (the config-flow field asks for watts).
            unit = (state_obj.attributes.get("unit_of_measurement") or "").lower()
            if unit == "kw":
                value *= 1000.0
            total += value
        return max(0.0, total)

    def is_charging(self, hass: HomeAssistant) -> bool:
        """Drawing-now SSOT: is the EV actually drawing current right now?

        STATELESS / instantaneous truth (no grace, no debounce — those belong to
        the consumers that need them). Resolution order:

        1. measured charging power available -> power > CHARGING_POWER_DRAWING_FLOOR_W
        2. else TOLERANT status fallback -> charging unless the status is an
           explicitly idle/done/unavailable value (matches the frontend
           `_isDrawingNow` so backend and dashboard agree on brand strings).
        3. else (no status sensor) -> False
        """
        power = self.read_charging_power(hass)
        if power is not None:
            return power > CHARGING_POWER_DRAWING_FLOOR_W
        status = get_state(hass, self._charger_status_entity)
        if status in _UNAVAILABLE_STATES or status in _IDLE_OR_DONE_STATUSES:
            return False
        # v2.9.1: brand synonyms (e.g. OCPP 'available', 'charged') are just as
        # explicitly not-drawing as the Tuya statuses above.
        if is_disconnected_status(status) or is_charge_complete_status(status):
            return False
        return True

    def is_plugged_in(self, hass: HomeAssistant) -> bool:
        """Lifecycle, NOT draw: is a cable plugged in?

        Read from the textual status (measured 0 W cannot tell "paused" from
        "unplugged"). Status present and not disconnected/unknown/unavailable.
        v2.9.1: uses the tolerant is_disconnected_status classifier so brand
        vocabularies (OCPP 'available', …) answer correctly.
        """
        state = get_state(hass, self._charger_status_entity)
        return state not in _UNAVAILABLE_STATES and not is_disconnected_status(state)

    # ----- conversion -----
    def watts_to_amps(self, watts: float) -> float:
        """Convert total watts to per-phase amperage using the effective voltage."""
        return watts / self.effective_voltage
