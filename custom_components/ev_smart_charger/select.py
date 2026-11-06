"""Select platform for EV Smart Charger helper entities."""
from __future__ import annotations
import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CAR_FIELDS,
    CHARGING_PROFILES,
    CONF_ACTIVE_CAR,
    CONF_BATTERY_CAPACITY,
    CONF_CAR_CONNECTION,
    CONF_CAR_LOCATION,
    CONF_CAR_NAME,
    CONF_EV_SOC_LIMIT,
    CONF_SOC_CAR,
    HELPER_ACTIVE_CAR_SUFFIX,
    LEGACY_CHARGING_PROFILES,
    PROFILE_MANUAL,
    PROFILE_SOLAR_SURPLUS,
    apply_active_car,
    get_active_car_name,
    get_cars,
)
from .entity_base import EVSCEntityMixin
from .runtime import get_runtime_data

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EVSC select entities."""

    runtime_data = get_runtime_data(entry)
    entities = []

    # Create Charging Profile Selector
    entities.append(
        EVSCSelect(
            runtime_data,
            entry.entry_id,
            "evsc_charging_profile",
            "Charging Profile",
            "mdi:ev-station",
            CHARGING_PROFILES,
        )
    )

    # v2.31.0: active car profile
    entities.append(EVSCCarSelect(hass, entry, runtime_data))

    async_add_entities(entities)
    _LOGGER.info(f"✅ Created {len(entities)} EVSC select entities")


class EVSCSelect(EVSCEntityMixin, SelectEntity, RestoreEntity):
    """EVSC Select Entity (behaves like input_select)."""

    _attr_should_poll = False

    def __init__(
        self,
        runtime_data,
        entry_id: str,
        suffix: str,
        name: str,
        icon: str,
        options: list[str],
    ) -> None:
        """Initialize the select."""
        self._init_evsc_entity(
            runtime_data,
            entry_id,
            suffix,
            "select",
            name,
            icon,
            entity_category=EntityCategory.CONFIG,
        )
        self._attr_options = options
        # v2.44.0: a new install starts on Solar Surplus (was manual). The
        # charging-profile select only; the restore path is unchanged.
        self._current_option = (
            PROFILE_SOLAR_SURPLUS if PROFILE_SOLAR_SURPLUS in options else PROFILE_MANUAL
        )

    @property
    def current_option(self) -> str:
        """Return the selected option."""
        return self._current_option

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option in self.options:
            self._current_option = option
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore last selected option."""
        await super().async_added_to_hass()
        _LOGGER.info(f"✅ Select entity registered: {self.entity_id} (unique_id: {self.unique_id})")

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state in self.options:
                self._current_option = last_state.state
            elif last_state.state in LEGACY_CHARGING_PROFILES:
                self._current_option = PROFILE_MANUAL
                _LOGGER.warning(
                    "Legacy charging profile '%s' restored for %s - coercing to '%s'",
                    last_state.state,
                    self.entity_id,
                    PROFILE_MANUAL,
                )

        # CRITICAL FIX (v1.6.0): Push restored value to state machine immediately
        # Without this, state remains "unavailable" until manual modification
        self.async_write_ha_state()


def _card_config(entry) -> dict:
    """v2.47.0: the integration's card config without the card-only keys."""
    try:
        from .dashboard_manager import build_card_sensor_config
        return build_card_sensor_config(entry)
    except Exception:  # noqa: BLE001 - never break the select over the card
        _LOGGER.debug("Card config for the dashboard card not available", exc_info=True)
        return {}


class EVSCCarSelect(EVSCEntityMixin, SelectEntity):
    """v2.31.0: choose the active car profile.

    The choice lives in the config entry (not a restored state), so every
    component starts with the right car. Choosing another car copies its
    sensors to the entry's car keys; the entry's update listener reloads
    the integration.
    """

    _attr_should_poll = False

    def __init__(self, hass, entry, runtime_data) -> None:
        self.hass = hass
        self._entry = entry
        self._init_evsc_entity(
            runtime_data,
            entry.entry_id,
            HELPER_ACTIVE_CAR_SUFFIX,
            "select",
            "Active car",
            "mdi:car-multiple",
        )

    @property
    def _data(self) -> dict:
        return dict(getattr(self._entry, "data", {}) or {})

    @property
    def options(self) -> list[str]:
        return [car[CONF_CAR_NAME] for car in get_cars(self._data)]

    @property
    def current_option(self) -> str:
        return get_active_car_name(self._data)

    # v2.47.0: the card config is only for the dashboard card; keep it out
    # of the recorder.
    _unrecorded_attributes = frozenset({"card_config"})

    @property
    def extra_state_attributes(self) -> dict:
        return {
            # v2.47.0: the sensors the dashboard card needs, so a card with
            # only `type` and `sections` follows the integration.
            "card_config": _card_config(self._entry),
            "cars": [
                {
                    "name": car[CONF_CAR_NAME],
                    "ev_soc_entity": car.get(CONF_SOC_CAR),
                    "ev_soc_limit_entity": car.get(CONF_EV_SOC_LIMIT),
                    "battery_capacity_kwh": car.get(CONF_BATTERY_CAPACITY),
                    "location_entity": car.get(CONF_CAR_LOCATION),
                    "connection_entity": car.get(CONF_CAR_CONNECTION),
                }
                for car in get_cars(self._data)
            ],
        }

    async def async_select_option(self, option: str) -> None:
        """Switch car: store it in the entry (the update listener reloads)."""
        if option not in self.options or option == self.current_option:
            return
        new_data = apply_active_car(self._data, option)
        if all(new_data.get(k) == self._data.get(k) for k in (*CAR_FIELDS, CONF_ACTIVE_CAR)):
            return
        _LOGGER.info("Active car changed to %s", option)
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
