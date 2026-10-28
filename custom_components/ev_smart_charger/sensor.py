"""Sensor platform for EV Smart Charger diagnostics."""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_SOC_CAR,
    HELPER_HYBRID_DIAGNOSTIC_SUFFIX,
    HELPER_NIGHT_SESSION_STATE_SUFFIX,
    HELPER_NIGHT_CHARGE_PLAN_SUFFIX,
    HYBRID_STATE_IDLE,
    NIGHT_CHARGE_MODE_IDLE,
    HELPER_FUSE_HEADROOM_SENSOR_SUFFIX,
    HELPER_MAIN_FUSE_SENSOR_SUFFIX,
    get_main_fuse_size,
    has_home_battery,
    is_fuse_protection_enabled,
)
from .entity_base import EVSCEntityMixin
from .runtime import EVSCRuntimeData, get_runtime_data
from .utils.logging_helper import EVSCLogger

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EVSC diagnostic sensor entities."""
    runtime_data = get_runtime_data(entry)
    battery_configured = has_home_battery(entry.data)

    entities = [
        EVSCDiagnosticSensor(
            runtime_data,
            entry.entry_id,
            "evsc_diagnostic",
            "EVSC Diagnostic Status",
            "mdi:information-outline",
        ),
        EVSCPriorityStateSensor(
            runtime_data,
            entry.entry_id,
            "evsc_priority_daily_state",
            "EVSC Priority Daily State",
            "mdi:priority-high",
        ),
        EVSCSolarSurplusDiagnosticSensor(
            runtime_data,
            entry.entry_id,
            "evsc_solar_surplus_diagnostic",
            "EVSC Solar Surplus Diagnostic",
            "mdi:solar-power",
        ),
        EVSCLogFilePathSensor(
            hass,
            runtime_data,
            entry.entry_id,
            "evsc_log_file_path",
            "EVSC Log File Path",
            "mdi:file-document-outline",
        ),
        EVSCTodayTargetSensor(
            runtime_data,
            entry.entry_id,
            "evsc_today_ev_target",
            "EVSC Today EV Target",
            "mdi:battery-charging-80",
            label="EV",
        ),
        EVSCCachedEVSOCSensor(
            runtime_data,
            entry.entry_id,
            entry.data.get(CONF_SOC_CAR),
            "evsc_cached_ev_soc",
            "EVSC Cached EV SOC",
            "mdi:car-battery",
        ),
        EVSCHybridInverterDiagnosticSensor(
            runtime_data,
            entry.entry_id,
            HELPER_HYBRID_DIAGNOSTIC_SUFFIX,
            "EVSC Hybrid Inverter Diagnostic",
            "mdi:solar-power-variant-outline",
        ),
        EVSCNightSessionStateSensor(
            runtime_data,
            entry.entry_id,
            HELPER_NIGHT_SESSION_STATE_SUFFIX,
            "EVSC Night Session State",
            "mdi:weather-night",
        ),
        EVSCNightChargePlanSensor(
            runtime_data,
            entry.entry_id,
            HELPER_NIGHT_CHARGE_PLAN_SUFFIX,
            "EVSC Night Charge Plan",
            "mdi:calendar-clock",
        ),
    ]

    # v1.7.0: skip Today Home Target sensor in PV-only mode
    if battery_configured:
        entities.append(
            EVSCTodayTargetSensor(
                runtime_data,
                entry.entry_id,
                "evsc_today_home_target",
                "EVSC Today Home Target",
                "mdi:home-battery",
                label="Home",
            )
        )

    # v2.14.0: main-fuse protection sensors (only when a fuse is configured)
    if is_fuse_protection_enabled(entry.data):
        entities.append(
            EVSCMainFuseSensor(
                runtime_data,
                entry.entry_id,
                HELPER_MAIN_FUSE_SENSOR_SUFFIX,
                "EVSC Main Fuse",
                "mdi:fuse",
                fuse_a=get_main_fuse_size(entry.data),
            )
        )
        entities.append(
            EVSCFuseHeadroomSensor(
                runtime_data,
                entry.entry_id,
                HELPER_FUSE_HEADROOM_SENSOR_SUFFIX,
                "EVSC Fuse Charging Limit",
                "mdi:current-ac",
            )
        )

    async_add_entities(entities)
    _LOGGER.info("✅ Created %s EVSC sensors", len(entities))


class EVSCBaseSensor(EVSCEntityMixin, SensorEntity, RestoreEntity):
    """Shared base for EVSC restoreable sensors."""

    _attr_should_poll = False

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
        *,
        native_value: Any = None,
        native_unit_of_measurement: str | None = None,
        entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC,
    ) -> None:
        """Initialize the shared sensor state."""
        self._init_evsc_entity(
            runtime_data,
            entry_id,
            suffix,
            "sensor",
            name,
            icon,
            entity_category=entity_category,
        )
        self._attr_native_value = native_value
        if native_unit_of_measurement is not None:
            self._attr_native_unit_of_measurement = native_unit_of_measurement
        self._attr_extra_state_attributes: dict[str, Any] = {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return current state attributes."""
        return self._attr_extra_state_attributes

    async def async_publish(
        self,
        value: Any,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Publish state updates through the entity object."""
        self._attr_native_value = value
        self._attr_extra_state_attributes = dict(attributes or {})
        self.async_write_ha_state()


class EVSCDiagnosticSensor(EVSCBaseSensor):
    """EVSC Diagnostic Sensor showing real-time automation status."""

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the diagnostic sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value="Initializing",
        )

    async def async_added_to_hass(self) -> None:
        """Restore last state."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "✅ Diagnostic sensor registered: %s (unique_id: %s)",
            self.entity_id,
            self.unique_id,
        )
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_native_value = last_state.state
            self._attr_extra_state_attributes = dict(last_state.attributes)


class EVSCPriorityStateSensor(EVSCBaseSensor):
    """EVSC Priority State Sensor showing current charging priority."""

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the priority sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value="EV_Free",
        )

    async def async_added_to_hass(self) -> None:
        """Restore last state."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "✅ Priority sensor registered: %s (unique_id: %s)",
            self.entity_id,
            self.unique_id,
        )
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_native_value = last_state.state
            self._attr_extra_state_attributes = dict(last_state.attributes)


class EVSCNightSessionStateSensor(EVSCBaseSensor):
    """Runtime state of the Night Smart Charge session (v1.11.9).

    Published by night_smart_charge.py whenever its `_active_mode` transitions
    between `idle`, `battery`, and `grid`. Consumed by the bundled Lovelace
    card so the hero banner can show a blue-violet "Night Smart Charge Active"
    state — analogous to the existing red Force / orange Boost banners.
    """

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the night session sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value=NIGHT_CHARGE_MODE_IDLE,
        )

    async def async_added_to_hass(self) -> None:
        """Restore last state across HA restarts."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "✅ Night session sensor registered: %s (unique_id: %s)",
            self.entity_id,
            self.unique_id,
        )
        # v2.24.1: runtime state, never restored. A restored "grid" from a
        # session that was running at restart kept the blue banner lit, since
        # Night Smart Charge starts idle and only publishes on a change.
        self._attr_native_value = NIGHT_CHARGE_MODE_IDLE
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()


class EVSCNightChargePlanSensor(EVSCBaseSensor):
    """Cheapest hours and charge plan of Night Smart Charge (v2.20.0).

    Published by planned_night_charge.py. State is the plan status
    (planned / charging / waiting_for_car / car_ready_off / ...); the
    attributes carry the hourly prices, the cheapest hours and the plan.
    """

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the plan sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value="waiting",
            entity_category=None,
        )


class EVSCSolarSurplusDiagnosticSensor(EVSCBaseSensor):
    """EVSC Solar Surplus Diagnostic Sensor with detailed check information."""

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the solar surplus diagnostic sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value="Waiting for first check",
        )

    async def async_added_to_hass(self) -> None:
        """Restore last state."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "✅ Solar Surplus Diagnostic sensor registered: %s (unique_id: %s)",
            self.entity_id,
            self.unique_id,
        )
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_native_value = last_state.state
            self._attr_extra_state_attributes = dict(last_state.attributes)


class EVSCLogFilePathSensor(EVSCEntityMixin, SensorEntity):
    """EVSC Log File Path Sensor."""

    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the sensor."""
        self._hass = hass
        self._init_evsc_entity(
            runtime_data,
            entry_id,
            suffix,
            "sensor",
            name,
            icon,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        self._attr_native_value = self._get_log_file_path()

    def _get_log_manager(self):
        """Return the runtime log manager when available."""
        return self._runtime_data.log_manager if self._runtime_data is not None else None

    def _get_log_file_path(self) -> str:
        """Get today's log file path from log manager."""
        log_manager = self._get_log_manager()
        if log_manager:
            return log_manager.get_log_file_path()

        now = dt_util.now()
        return self._hass.config.path(
            "custom_components",
            "ev_smart_charger",
            "logs",
            str(now.year),
            f"{now.month:02d}",
            f"{now.day:02d}.log",
        )

    def _get_logs_directory(self) -> str:
        """Get base logs directory from log manager."""
        log_manager = self._get_log_manager()
        if log_manager:
            return log_manager.get_logs_directory()

        return self._hass.config.path(
            "custom_components",
            "ev_smart_charger",
            "logs",
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return the state attributes."""
        return {
            "description": "Today's log file path (format: logs/<year>/<month>/<day>.log)",
            "friendly_name": "Log File Path",
            "logs_directory": self._get_logs_directory(),
            "structure": "logs/<year>/<month>/<day>.log",
            # v2.13.0 (issue #59): the daily file only ever receives records the
            # package logger lets through. Surfacing both makes an empty log file
            # self-diagnosable instead of looking like a broken toggle.
            "file_logging_active": EVSCLogger.is_global_file_logging_enabled(),
            "effective_log_level": EVSCLogger.get_effective_level_name(),
        }

    async def async_update(self) -> None:
        """Update the sensor value."""
        self._attr_native_value = self._get_log_file_path()

    async def async_added_to_hass(self) -> None:
        """Entity added to hass."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "✅ Log File Path sensor registered: %s (unique_id: %s)",
            self.entity_id,
            self.unique_id,
        )
        _LOGGER.info("  📄 Today's log file: %s", self._attr_native_value)


class EVSCTodayTargetSensor(EVSCBaseSensor):
    """EVSC Today Target Sensor (unified for EV and Home)."""

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
        label: str = "Target",
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value=None,
            native_unit_of_measurement="%",
        )
        self._label = label

    async def async_added_to_hass(self) -> None:
        """Restore last state."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "✅ Today %s Target sensor registered: %s (unique_id: %s)",
            self._label,
            self.entity_id,
            self.unique_id,
        )
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = (
                    float(last_state.state)
                    if last_state.state not in (None, "unknown", "unavailable")
                    else None
                )
            except (ValueError, TypeError):
                self._attr_native_value = None
            self._attr_extra_state_attributes = dict(last_state.attributes)


class EVSCHybridInverterDiagnosticSensor(EVSCBaseSensor):
    """Hybrid Inverter Mode diagnostic sensor (v1.8.0 — issue #20).

    Exposes the state of the curtailment-discovery state machine so users can
    observe what the probing/riding-edge loop is doing in real time. State
    string is human-readable (e.g., "PROBING (45/60s)", "RIDING_EDGE @ 10A",
    "COOLDOWN_LONG (8m left)"). Attributes carry the full diagnostic snapshot
    from HybridInverterMode.get_diagnostic_snapshot().
    """

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the hybrid inverter diagnostic sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value=HYBRID_STATE_IDLE,
        )

    async def async_added_to_hass(self) -> None:
        """Restore last state."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "✅ Hybrid Inverter Diagnostic sensor registered: %s (unique_id: %s)",
            self.entity_id,
            self.unique_id,
        )
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_native_value = last_state.state
            self._attr_extra_state_attributes = dict(last_state.attributes)


class EVSCCachedEVSOCSensor(EVSCBaseSensor):
    """Reliable cache for a cloud-based EV SOC sensor."""

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        source_entity: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the cached sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value=None,
            native_unit_of_measurement="%",
        )
        self._source_entity = source_entity
        self._attr_extra_state_attributes = {
            "source_entity": source_entity,
            "last_valid_update": None,
            "is_cached": False,
        }

    async def async_publish_cache(
        self,
        value: float,
        *,
        last_valid_update: datetime,
        is_cached: bool,
        cache_age_seconds: int,
    ) -> None:
        """Publish a cached EV SOC update."""
        await self.async_publish(
            value,
            {
                "source_entity": self._source_entity,
                "last_valid_update": last_valid_update.isoformat(),
                "is_cached": is_cached,
                "cache_age_seconds": cache_age_seconds,
            },
        )

    async def async_added_to_hass(self) -> None:
        """Restore last state."""
        await super().async_added_to_hass()
        _LOGGER.info(
            "✅ Cached EV SOC sensor registered: %s (unique_id: %s)",
            self.entity_id,
            self.unique_id,
        )
        _LOGGER.info("  🔗 Source sensor: %s", self._source_entity)
        last_state = await self.async_get_last_state()
        # v2.32.0: a cached value only belongs to the car it was read from;
        # after a car switch (or for the guest profile) start empty.
        if last_state is not None and (
            not self._source_entity
            or last_state.attributes.get("source_entity") != self._source_entity
        ):
            _LOGGER.info("  ⏭️ Cached SOC belongs to another car - not restored")
            last_state = None
        if last_state is not None:
            try:
                self._attr_native_value = (
                    float(last_state.state)
                    if last_state.state not in (None, "unknown", "unavailable")
                    else None
                )
                _LOGGER.info("  🔄 Restored cached SOC: %s%%", self._attr_native_value)
            except (ValueError, TypeError):
                self._attr_native_value = None
                _LOGGER.warning("  ⚠️ Failed to restore cached SOC from: %s", last_state.state)
            self._attr_extra_state_attributes = dict(last_state.attributes)


class EVSCMainFuseSensor(EVSCBaseSensor):
    """Configured main fuse size in A (v2.14.0). Static, from the config entry."""

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
        *,
        fuse_a: int,
    ) -> None:
        """Initialize the main fuse sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value=fuse_a,
            native_unit_of_measurement="A",
        )


class EVSCFuseHeadroomSensor(EVSCBaseSensor):
    """Current the charger may draw per phase under the main fuse (v2.14.0).

    State: the ceiling in A (0 = charging not allowed right now). Attributes
    explain how it was computed: signed grid current, charger current, house
    current and remaining room per phase, the bottleneck phase and the limiter
    state (ok / limited / ramping / stopped / hold / invalid_data).
    Published by FuseGuard.
    """

    def __init__(
        self,
        runtime_data: EVSCRuntimeData,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the headroom sensor."""
        super().__init__(
            runtime_data,
            entry_id,
            suffix,
            name,
            icon,
            native_value=None,
            native_unit_of_measurement="A",
            entity_category=None,
        )

    def publish(self, value: Any, attributes: dict[str, Any]) -> None:
        """Synchronously publish a new value (called from FuseGuard callbacks)."""
        self._attr_native_value = value
        self._attr_extra_state_attributes = dict(attributes)
        if self.hass is not None:
            self.async_write_ha_state()
