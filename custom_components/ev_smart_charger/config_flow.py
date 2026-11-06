from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ACTIVE_CAR,
    CONF_CAR_NAME,
    CONF_CAR_CONNECTION,
    CONF_CAR_LOCATION,
    CONF_CARS,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_POWER,
    CONF_CAR_OWNER,
    CONF_CHARGER_MODEL,
    CONF_CHARGING_POWER,
    CONF_CHARGING_POWER_L2,
    CONF_CHARGING_POWER_L3,
    CONF_CREATE_DASHBOARD,
    CONF_ENERGY_FORECAST_TARGET,
    CONF_PRICE_SENSOR,
    CONF_EV_CHARGER_CURRENT,
    CONF_EV_CHARGER_STATUS,
    CONF_EV_CHARGER_START_BUTTON,
    CONF_EV_CHARGER_STOP_BUTTON,
    CONF_EV_CHARGER_SWITCH,
    CONF_FV_PRODUCTION,
    CONF_FV_PRODUCTION_L2,
    CONF_FV_PRODUCTION_L3,
    CONF_GRID_AVAILABLE,
    CONF_GRID_IMPORT,
    CONF_GRID_IMPORT_L2,
    CONF_GRID_IMPORT_L3,
    CONF_GRID_EXPORT,
    CONF_GRID_EXPORT_L2,
    CONF_GRID_EXPORT_L3,
    CONF_HOME_CONSUMPTION,
    CONF_HOME_CONSUMPTION_L2,
    CONF_HOME_CONSUMPTION_L3,
    CONF_NOTIFY_SERVICES,
    CONF_PHASE_MODE,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_TOMORROW,
    CONF_SOC_CAR,
    CONF_EV_SOC_LIMIT,
    SOC_INPUT_DOMAINS,
    CONF_SOC_HOME,
    CHARGER_MODEL_GENERIC,
    CHARGER_MODEL_TUYA,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CHARGER_MODEL,
    DEFAULT_CREATE_DASHBOARD,
    DEFAULT_NAME,
    DOMAIN,
    MAX_BATTERY_CAPACITY,
    MIN_BATTERY_CAPACITY,
    PHASE_MODE_THREE,
    CONF_FUSE_CHARGER_CURRENT,
    CONF_FUSE_CHARGER_CURRENT_L1,
    CONF_FUSE_CHARGER_CURRENT_L2,
    CONF_FUSE_CHARGER_CURRENT_L3,
    CONF_MAIN_FUSE_SIZE,
    FUSE_GRID_CURRENT_KEYS,
    FUSE_GRID_EXPORT_KEYS,
    FUSE_GRID_IMPORT_KEYS,
    FUSE_GRID_VOLTAGE_KEYS,
    MAIN_FUSE_SIZES,
    CONF_HAS_HOME_BATTERY,
    CONF_HOME_BATTERY_CONTROL,
    CONF_HAS_SOLAR,
    CONF_LOAD_BALANCING,
    DEFAULT_LOAD_BALANCING_FUSE_SIZE,
    LOAD_BALANCING_DISPLAY,
    LOAD_BALANCING_INTEGRATION,
    LOAD_BALANCING_NONE,
    LOAD_BALANCING_OPTIONS,
    GUEST_CAR,
    RESERVED_CAR_NAMES,
    apply_active_car,
    get_active_car_name,
    get_own_cars,
    get_load_balancing,
    get_main_fuse_size,
    has_solar,
    wants_home_battery,
    controls_home_battery,
)

CURRENT_CONTROL_DOMAINS = ["number", "select", "input_number", "input_select"]
ENERGY_TARGET_DOMAINS = ["input_number", "number"]


def _get_mobile_notify_services(hass) -> list[str]:
    """Get list of available mobile_app notify services."""
    notify_services = hass.services.async_services().get("notify", {})
    return sorted(
        service
        for service in notify_services
        if service.startswith("mobile_app_")
    )


def _validate_external_connectors(hass, user_input: dict[str, Any]) -> dict[str, str]:
    """Validate shared external connector settings."""
    errors: dict[str, str] = {}

    battery_capacity = user_input.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
    if battery_capacity < MIN_BATTERY_CAPACITY or battery_capacity > MAX_BATTERY_CAPACITY:
        errors["battery_capacity"] = "invalid_battery_capacity"

    energy_target = user_input.get(CONF_ENERGY_FORECAST_TARGET)
    if energy_target:
        state = hass.states.get(energy_target)
        if state is None:
            errors["energy_forecast_target"] = "entity_not_found"
        elif state.domain not in ENERGY_TARGET_DOMAINS:
            errors["energy_forecast_target"] = "invalid_domain"

    return errors


def _is_duplicate_charger_switch(
    hass,
    charger_switch: str,
    *,
    exclude_entry_id: str | None = None,
) -> bool:
    """Return True when another entry already owns the same charger switch."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if exclude_entry_id is not None and entry.entry_id == exclude_entry_id:
            continue
        if _charger_control_id(entry.data) == charger_switch:
            return True
    return False


def _charger_control_id(data: dict[str, Any]) -> str | None:
    """Return the entity that identifies the charger: switch or start button."""
    return data.get(CONF_EV_CHARGER_SWITCH) or data.get(CONF_EV_CHARGER_START_BUTTON)


def _normalize_charger_input(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the charger on/off control: a switch or a start/stop button pair.

    v2.14.0 (issue #65): unset control fields are stored as None so that a
    reconfigure from switch to buttons (or back) really clears the old value
    instead of keeping it through the data merge.
    """
    data = dict(user_input)
    for key in (
        CONF_EV_CHARGER_SWITCH,
        CONF_EV_CHARGER_START_BUTTON,
        CONF_EV_CHARGER_STOP_BUTTON,
        # v2.39.0: optional L2/L3 of the charging power and charger current.
        CONF_CHARGING_POWER_L2,
        CONF_CHARGING_POWER_L3,
        CONF_FUSE_CHARGER_CURRENT_L2,
        CONF_FUSE_CHARGER_CURRENT_L3,
    ):
        data[key] = data.get(key) or None
    # v2.39.0: the one-phase charger current lives in CONF_FUSE_CHARGER_CURRENT;
    # an older dedicated L1 key was shown in that field and is dropped.
    data[CONF_FUSE_CHARGER_CURRENT_L1] = None

    if data[CONF_EV_CHARGER_SWITCH]:
        data[CONF_EV_CHARGER_START_BUTTON] = None
        data[CONF_EV_CHARGER_STOP_BUTTON] = None
        return data, {}
    if data[CONF_EV_CHARGER_START_BUTTON] and data[CONF_EV_CHARGER_STOP_BUTTON]:
        return data, {}
    return data, {"base": "charger_control_required"}


# v2.41.0: sensors from earlier versions that the meter step replaces (the
# net grid sensor is the single CONF_GRID_IMPORT; house consumption is
# computed). Written as None so a reconfigure really drops them.
_LEGACY_GRID_KEYS = (
    CONF_GRID_IMPORT_L2,
    CONF_GRID_IMPORT_L3,
    CONF_GRID_EXPORT,
    CONF_GRID_EXPORT_L2,
    CONF_GRID_EXPORT_L3,
    CONF_HOME_CONSUMPTION,
    CONF_HOME_CONSUMPTION_L2,
    CONF_HOME_CONSUMPTION_L3,
)

# v2.41.0: the electricity meter, asked in every setup (all required except
# grid available).
_METER_KEYS = (
    CONF_GRID_IMPORT,
    *FUSE_GRID_IMPORT_KEYS,
    *FUSE_GRID_EXPORT_KEYS,
    *FUSE_GRID_CURRENT_KEYS,
    *FUSE_GRID_VOLTAGE_KEYS,
)


def _normalize_meter_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Turn the meter form into entry data (v2.41.0)."""
    data = {key: user_input.get(key) or None for key in _METER_KEYS}
    data[CONF_MAIN_FUSE_SIZE] = get_main_fuse_size(user_input) or DEFAULT_LOAD_BALANCING_FUSE_SIZE
    data[CONF_GRID_AVAILABLE] = user_input.get(CONF_GRID_AVAILABLE) or None
    for key in _LEGACY_GRID_KEYS:
        data[key] = None
    return data


def _normalize_solar_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Solar production; cleared L2/L3 written as None (v2.41.0)."""
    return {
        CONF_FV_PRODUCTION: user_input.get(CONF_FV_PRODUCTION),
        CONF_FV_PRODUCTION_L2: user_input.get(CONF_FV_PRODUCTION_L2) or None,
        CONF_FV_PRODUCTION_L3: user_input.get(CONF_FV_PRODUCTION_L3) or None,
    }


def _normalize_battery_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Home battery SOC and power (v2.41.0)."""
    return {
        CONF_SOC_HOME: user_input.get(CONF_SOC_HOME),
        CONF_BATTERY_POWER: user_input.get(CONF_BATTERY_POWER),
    }


def _merge_entry_data(base_data: dict[str, Any], *sections: dict[str, Any]) -> dict[str, Any]:
    """Merge config sections into a single payload."""
    merged = dict(base_data)
    for section in sections:
        merged.update(section)
    return merged


def _entity_selector(domains: str | list[str]) -> selector.EntitySelector:
    """Create an entity selector for one or more domains."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domains))


def _field_config(default: Any) -> dict[str, Any]:
    """Return voluptuous field kwargs only when a real default is available."""
    if default is None:
        return {}
    return {"default": default}


def _suggested(current_data: dict[str, Any], key: str) -> dict[str, Any]:
    """Prefill an optional field without a default, so it can be cleared."""
    return {"description": {"suggested_value": current_data.get(key)}}


def _charger_schema(current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the charger entities schema.

    v2.39.0: everything about the charger is asked here and is required: the
    on/off control (switch or start/stop buttons), the current control, the
    status sensor, the charging power (total, or L1 with optional L2/L3) and
    the charger current (one phase, or L1 with optional L2/L3).
    """
    current_data = current_data or {}
    charger_current = current_data.get(CONF_FUSE_CHARGER_CURRENT) or current_data.get(
        CONF_FUSE_CHARGER_CURRENT_L1
    )
    return vol.Schema(
        {
            # v2.14.0 (issue #65): the on/off control is either a switch or a
            # start/stop button pair. suggested_value (not default) so a field
            # can be cleared when moving from one to the other.
            vol.Optional(
                CONF_EV_CHARGER_SWITCH, **_suggested(current_data, CONF_EV_CHARGER_SWITCH)
            ): _entity_selector("switch"),
            vol.Optional(
                CONF_EV_CHARGER_START_BUTTON,
                **_suggested(current_data, CONF_EV_CHARGER_START_BUTTON),
            ): _entity_selector("button"),
            vol.Optional(
                CONF_EV_CHARGER_STOP_BUTTON,
                **_suggested(current_data, CONF_EV_CHARGER_STOP_BUTTON),
            ): _entity_selector("button"),
            vol.Required(
                CONF_EV_CHARGER_CURRENT,
                **_field_config(current_data.get(CONF_EV_CHARGER_CURRENT)),
            ): _entity_selector(CURRENT_CONTROL_DOMAINS),
            vol.Required(
                CONF_EV_CHARGER_STATUS,
                **_field_config(current_data.get(CONF_EV_CHARGER_STATUS)),
            ): _entity_selector("sensor"),
            vol.Required(
                CONF_CHARGING_POWER,
                **_field_config(current_data.get(CONF_CHARGING_POWER)),
            ): _entity_selector("sensor"),
            vol.Optional(
                CONF_CHARGING_POWER_L2, **_suggested(current_data, CONF_CHARGING_POWER_L2)
            ): _entity_selector("sensor"),
            vol.Optional(
                CONF_CHARGING_POWER_L3, **_suggested(current_data, CONF_CHARGING_POWER_L3)
            ): _entity_selector("sensor"),
            vol.Required(
                CONF_FUSE_CHARGER_CURRENT, **_field_config(charger_current)
            ): _entity_selector("sensor"),
            vol.Optional(
                CONF_FUSE_CHARGER_CURRENT_L2,
                **_suggested(current_data, CONF_FUSE_CHARGER_CURRENT_L2),
            ): _entity_selector("sensor"),
            vol.Optional(
                CONF_FUSE_CHARGER_CURRENT_L3,
                **_suggested(current_data, CONF_FUSE_CHARGER_CURRENT_L3),
            ): _entity_selector("sensor"),
        }
    )


# v2.0.0: radio option labels per language. The longer per-option explanations
# live in strings.json step `description` (which HA localizes normally); these are
# just the short radio labels. Localized in Python — instead of a selector
# `translation_key` — so it works across HA versions (older cores reject
# translation_key on a SelectSelectorConfig during form serialization).
_RADIO_LABELS: dict[str, dict[str, dict[str, str]]] = {
    CONF_CHARGER_MODEL: {
        "en": {CHARGER_MODEL_TUYA: "Tuya (standard)", CHARGER_MODEL_GENERIC: "Generic (1 A steps)"},
        "it": {CHARGER_MODEL_TUYA: "Tuya (standard)", CHARGER_MODEL_GENERIC: "Generica (scatti da 1 A)"},
        "nl": {CHARGER_MODEL_TUYA: "Tuya (standaard)", CHARGER_MODEL_GENERIC: "Generiek (stappen van 1 A)"},
    },
}


def _radio_schema(hass, key: str, options: list[str], current_value: str) -> vol.Schema:
    """Build a single-field radio (SelectSelector LIST) schema with i18n labels."""
    lang = (getattr(hass.config, "language", None) or "en") if hass else "en"
    labels = _RADIO_LABELS[key].get(lang) or _RADIO_LABELS[key]["en"]
    return vol.Schema(
        {
            vol.Required(key, default=current_value): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": opt, "label": labels.get(opt, opt)} for opt in options
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
        }
    )


def _charger_model_schema(hass, current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the charger-model radio schema (tuya / generic) — v2.0.0."""
    current_data = current_data or {}
    return _radio_schema(
        hass,
        CONF_CHARGER_MODEL,
        [CHARGER_MODEL_TUYA, CHARGER_MODEL_GENERIC],
        current_data.get(CONF_CHARGER_MODEL, DEFAULT_CHARGER_MODEL),
    )


def _meter_schema(current_data: dict[str, Any] | None = None) -> vol.Schema:
    """The electricity meter (v2.41.0): asked in every setup.

    Main fuse size, net grid power (positive when buying, negative when
    selling), and import power, export power, current and voltage per phase.
    Grid available is optional.
    """
    current_data = current_data or {}
    sizes = [size for size in MAIN_FUSE_SIZES if size > 0]
    current_size = get_main_fuse_size(current_data) or DEFAULT_LOAD_BALANCING_FUSE_SIZE
    if current_size not in sizes:
        current_size = DEFAULT_LOAD_BALANCING_FUSE_SIZE

    fields: dict[Any, Any] = {
        vol.Required(CONF_MAIN_FUSE_SIZE, default=str(current_size)): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[{"value": str(size), "label": f"{size} A"} for size in sizes],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    }
    for key in _METER_KEYS:
        fields[vol.Required(key, **_field_config(current_data.get(key)))] = _entity_selector("sensor")
    fields[
        vol.Optional(CONF_GRID_AVAILABLE, **_suggested(current_data, CONF_GRID_AVAILABLE))
    ] = _entity_selector("binary_sensor")
    return vol.Schema(fields)


def _solar_schema(current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Solar production: total or L1, L2/L3 optional (v2.41.0)."""
    current_data = current_data or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_FV_PRODUCTION, **_field_config(current_data.get(CONF_FV_PRODUCTION))
            ): _entity_selector("sensor"),
            vol.Optional(
                CONF_FV_PRODUCTION_L2, **_suggested(current_data, CONF_FV_PRODUCTION_L2)
            ): _entity_selector("sensor"),
            vol.Optional(
                CONF_FV_PRODUCTION_L3, **_suggested(current_data, CONF_FV_PRODUCTION_L3)
            ): _entity_selector("sensor"),
        }
    )


def _battery_schema(current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Home battery SOC and signed power, both required (v2.41.0)."""
    current_data = current_data or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_SOC_HOME, **_field_config(current_data.get(CONF_SOC_HOME))
            ): _entity_selector(SOC_INPUT_DOMAINS),
            vol.Required(
                CONF_BATTERY_POWER, **_field_config(current_data.get(CONF_BATTERY_POWER))
            ): _entity_selector("sensor"),
        }
    )


def _pv_forecast_schema(current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the PV forecast schema.

    Two independent optional sensors:
      - CONF_PV_FORECAST: drives Night Smart Charge's battery-vs-grid
        decision. Semantically the "next-day" forecast for that logic,
        but kept named generically because existing installs map various
        sensor flavours (remaining-today, tomorrow, custom templates).
      - CONF_PV_FORECAST_TOMORROW (v1.11.14): dedicated to the dashboard
        "Forecast Domani" chip. Lets users map a true tomorrow-forecast
        sensor (e.g. `sensor.solcast_pv_forecast_forecast_tomorrow`)
        without disturbing their existing Night Smart Charge wiring.
    """
    current_data = current_data or {}
    return vol.Schema(
        {
            # v2.39.0: both required (the step is only shown with solar).
            vol.Required(
                CONF_PV_FORECAST,
                **_field_config(current_data.get(CONF_PV_FORECAST)),
            ): _entity_selector("sensor"),
            vol.Required(
                CONF_PV_FORECAST_TOMORROW,
                **_field_config(current_data.get(CONF_PV_FORECAST_TOMORROW)),
            ): _entity_selector("sensor"),
        }
    )



def _notifications_schema(hass, current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the notifications schema."""
    current_data = current_data or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_NOTIFY_SERVICES,
                **_field_config(current_data.get(CONF_NOTIFY_SERVICES, [])),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_get_mobile_notify_services(hass),
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_CAR_OWNER,
                **_field_config(current_data.get(CONF_CAR_OWNER)),
            ): _entity_selector("person"),
        }
    )


def _external_connectors_schema(current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the external connectors schema."""
    current_data = current_data or {}
    return vol.Schema(
        {
            # v2.32.0: the battery capacity belongs to the car profile.
            vol.Optional(
                CONF_ENERGY_FORECAST_TARGET,
                **_field_config(current_data.get(CONF_ENERGY_FORECAST_TARGET)),
            ): _entity_selector(ENERGY_TARGET_DOMAINS),
            # v2.20.0: electricity price sensor for planned night charging
            # (Nord Pool core or HACS, or any sensor with raw_today/raw_tomorrow).
            # v2.39.0: required.
            vol.Required(
                CONF_PRICE_SENSOR,
                **_field_config(current_data.get(CONF_PRICE_SENSOR)),
            ): _entity_selector("sensor"),
        }
    )


def _dashboard_schema(current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the auto-generated dashboard schema (v1.9.0+)."""
    current_data = current_data or {}
    current_value = current_data.get(CONF_CREATE_DASHBOARD, DEFAULT_CREATE_DASHBOARD)
    return vol.Schema(
        {
            vol.Optional(
                CONF_CREATE_DASHBOARD,
                default=current_value,
            ): selector.BooleanSelector(),
        }
    )


# ---------------------------------------------------------------------------
# v2.17.0: "What do you have?" setup step
# ---------------------------------------------------------------------------

_LOAD_BALANCING_LABELS: dict[str, dict[str, str]] = {
    "en": {
        LOAD_BALANCING_NONE: "None",
        LOAD_BALANCING_DISPLAY: "Display only (show what load balancing should do)",
        LOAD_BALANCING_INTEGRATION: "Display and adjust (this integration protects the main fuse)",
    },
    "it": {
        LOAD_BALANCING_NONE: "Nessuno",
        LOAD_BALANCING_DISPLAY: "Solo visualizzazione (mostra cosa dovrebbe fare il bilanciamento)",
        LOAD_BALANCING_INTEGRATION: "Visualizza e regola (questa integrazione protegge il contatore)",
    },
    "nl": {
        LOAD_BALANCING_NONE: "Geen",
        LOAD_BALANCING_DISPLAY: "Alleen tonen (laat zien wat de balancering zou moeten doen)",
        LOAD_BALANCING_INTEGRATION: "Tonen en regelen (deze integratie bewaakt de hoofdzekering)",
    },
}


def _features_defaults(current_data: dict[str, Any] | None) -> dict[str, Any]:
    """Return the setup answers for an entry (explicit or derived)."""
    current_data = current_data or {}
    return {
        CONF_HAS_SOLAR: has_solar(current_data),
        CONF_HAS_HOME_BATTERY: wants_home_battery(current_data),
        CONF_HOME_BATTERY_CONTROL: controls_home_battery(current_data),
        CONF_LOAD_BALANCING: get_load_balancing(current_data),
    }


def _features_schema(hass, current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the "What do you have?" schema (v2.17.0)."""
    defaults = _features_defaults(current_data)
    lang = (getattr(hass.config, "language", None) or "en") if hass else "en"
    labels = _LOAD_BALANCING_LABELS.get(lang) or _LOAD_BALANCING_LABELS["en"]
    return vol.Schema(
        {
            vol.Required(
                CONF_HAS_SOLAR, default=defaults[CONF_HAS_SOLAR]
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_HAS_HOME_BATTERY, default=defaults[CONF_HAS_HOME_BATTERY]
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_HOME_BATTERY_CONTROL,
                default=defaults[CONF_HOME_BATTERY_CONTROL],
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_LOAD_BALANCING, default=defaults[CONF_LOAD_BALANCING]
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": opt, "label": labels[opt]}
                        for opt in LOAD_BALANCING_OPTIONS
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _normalize_features(user_input: dict[str, Any]) -> dict[str, Any]:
    """Coerce the setup answers into entry data."""
    load_balancing = user_input.get(CONF_LOAD_BALANCING)
    if load_balancing not in LOAD_BALANCING_OPTIONS:
        load_balancing = LOAD_BALANCING_NONE
    return {
        CONF_HAS_SOLAR: bool(user_input.get(CONF_HAS_SOLAR, True)),
        CONF_HAS_HOME_BATTERY: bool(user_input.get(CONF_HAS_HOME_BATTERY, False)),
        CONF_HOME_BATTERY_CONTROL: bool(
            user_input.get(CONF_HOME_BATTERY_CONTROL, True)
        ),
        CONF_LOAD_BALANCING: load_balancing,
    }


_SOLAR_KEYS = (
    CONF_FV_PRODUCTION,
    CONF_FV_PRODUCTION_L2,
    CONF_FV_PRODUCTION_L3,
    CONF_HOME_CONSUMPTION,
    CONF_HOME_CONSUMPTION_L2,
    CONF_HOME_CONSUMPTION_L3,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_TOMORROW,
)


def _apply_features(data: dict[str, Any]) -> dict[str, Any]:
    """Clear every mapping the setup answers rule out (v2.17.0).

    Runs on the final merged entry data, so switching an answer off during
    reconfigure really removes the old sensors (and with them the helper
    entities that depend on them, e.g. the home-battery numbers).
    """
    data = dict(data)
    if not wants_home_battery(data):
        data[CONF_SOC_HOME] = None
        data[CONF_BATTERY_POWER] = None
    if not has_solar(data):
        for key in _SOLAR_KEYS:
            data[key] = None
    # v2.41.0: a saved "the charger handles it" becomes "none".
    data[CONF_LOAD_BALANCING] = get_load_balancing(data)
    # v2.39.0: single-phase installs were removed.
    data[CONF_PHASE_MODE] = PHASE_MODE_THREE
    return data


# Logical step order shared by the three flows. "name" is initial-flow only.
_STEP_ORDER = (
    "name",
    "model",
    "features",
    # v2.41.0: the sensors are asked per part: meter, charger, solar, battery.
    "meter",
    "entities",
    "solar",
    "battery",
    "forecast",
    "notifications",
    "external",
    # v2.31.0: car profiles (v2.32.0: in the initial flow too)
    "cars",
    "dashboard",
)

# Logical step -> step_id per flow kind.
_STEP_IDS: dict[str, dict[str, str]] = {
    "initial": {
        "name": "user",
        "model": "charger_model",
        "features": "features",
        "meter": "meter",
        "entities": "entities",
        "solar": "solar",
        "battery": "battery",
        "forecast": "pv_forecast",
        "notifications": "notifications",
        "external": "external_connectors",
        "cars": "cars",
        "dashboard": "dashboard",
    },
    "reconfigure": {
        # v2.39.0: the phase-mode step is gone; the charger model is the
        # entry point of reconfigure and options.
        "model": "reconfigure",
        "features": "reconfigure_features",
        "meter": "reconfigure_meter",
        "entities": "reconfigure_entities",
        "solar": "reconfigure_solar",
        "battery": "reconfigure_battery",
        "forecast": "reconfigure_pv_forecast",
        "notifications": "reconfigure_notifications",
        "external": "reconfigure_external_connectors",
        "cars": "reconfigure_cars",
        "dashboard": "reconfigure_dashboard",
    },
    "options": {
        # v2.43.0: "init" is a menu (cars only / all settings).
        "model": "charger_model",
        "features": "features",
        "meter": "meter",
        "entities": "entities",
        "solar": "solar",
        "battery": "battery",
        "forecast": "pv_forecast",
        "notifications": "notifications",
        "external": "external_connectors",
        "cars": "cars",
        "dashboard": "dashboard",
    },
}


def _active_steps(kind: str, features: dict[str, Any]) -> list[str]:
    """Return the logical steps shown for these setup answers."""
    steps = []
    for step in _STEP_ORDER:
        if step not in _STEP_IDS[kind]:
            continue
        if step in ("solar", "forecast") and not has_solar(features):
            continue
        if step == "battery" and not wants_home_battery(features):
            continue
        steps.append(step)
    return steps


# v2.31.0: car profile editor -------------------------------------------------
_CAR_ACTION = "car_action"
_CAR_ACTION_LABELS = {
    "en": {"done": "Continue", "save": "Save", "add": "Add a car", "edit": "Edit {name}", "remove": "Remove {name}"},
    "it": {"done": "Continua", "save": "Salva", "add": "Aggiungi un'auto", "edit": "Modifica {name}", "remove": "Rimuovi {name}"},
    "nl": {"done": "Doorgaan", "save": "Opslaan", "add": "Auto toevoegen", "edit": "{name} bewerken", "remove": "{name} verwijderen"},
    "sv": {"done": "Fortsätt", "save": "Spara", "add": "Lägg till bil", "edit": "Ändra {name}", "remove": "Ta bort {name}"},
}


def _cars_menu_schema(hass, cars: list[dict], save: bool = False) -> vol.Schema:
    lang = (getattr(hass.config, "language", None) or "en") if hass else "en"
    labels = _CAR_ACTION_LABELS.get(lang) or _CAR_ACTION_LABELS["en"]
    done_label = labels["save"] if save else labels["done"]
    options = [{"value": "done", "label": done_label}, {"value": "add", "label": labels["add"]}]
    for index, car in enumerate(cars):
        options.append(
            {"value": f"edit:{index}", "label": labels["edit"].format(name=car[CONF_CAR_NAME])}
        )
    for index, car in enumerate(cars):
        options.append(
            {"value": f"remove:{index}", "label": labels["remove"].format(name=car[CONF_CAR_NAME])}
        )
    return vol.Schema(
        {
            vol.Required(_CAR_ACTION, default="done"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.LIST
                )
            )
        }
    )


def _car_schema(car: dict[str, Any] | None = None) -> vol.Schema:
    car = car or {}
    return vol.Schema(
        {
            vol.Required(CONF_CAR_NAME, **_field_config(car.get(CONF_CAR_NAME))): str,
            vol.Required(CONF_SOC_CAR, **_field_config(car.get(CONF_SOC_CAR))): _entity_selector(SOC_INPUT_DOMAINS),
            vol.Optional(
                CONF_EV_SOC_LIMIT, **_field_config(car.get(CONF_EV_SOC_LIMIT))
            ): _entity_selector(SOC_INPUT_DOMAINS),
            vol.Required(
                CONF_BATTERY_CAPACITY,
                **_field_config(car.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)),
            ): vol.All(
                vol.Coerce(float),
                vol.Range(min=MIN_BATTERY_CAPACITY, max=MAX_BATTERY_CAPACITY),
            ),
            vol.Optional(
                CONF_CAR_LOCATION, **_field_config(car.get(CONF_CAR_LOCATION))
            ): _entity_selector(["device_tracker", "person"]),
            vol.Optional(
                CONF_CAR_CONNECTION, **_field_config(car.get(CONF_CAR_CONNECTION))
            ): _entity_selector(["sensor", "binary_sensor"]),
        }
    )


def _cars_list_text(cars: list[dict], active: str | None) -> str:
    """The guest profile first (always there, no sensors), then own cars."""
    lines = [f"• {GUEST_CAR}{' ✓' if active == GUEST_CAR else ''} — —"]
    for car in cars:
        mark = " ✓" if car[CONF_CAR_NAME] == active else ""
        lines.append(f"• {car[CONF_CAR_NAME]}{mark} — {car.get(CONF_SOC_CAR) or '—'}")
    return "\n".join(lines)


class _StepNavigator:
    """Shared step numbering and "next step" logic for the three flows."""

    _flow_kind = "initial"
    features_info: dict[str, Any]

    def _base_data(self) -> dict[str, Any]:
        return {}

    def _features(self) -> dict[str, Any]:
        if self.features_info:
            return self.features_info
        return _features_defaults(self._base_data())

    # v2.43.0: options menu "Cars" edits only the car profiles and saves.
    _cars_only = False

    def _placeholders(self, logical: str) -> dict[str, str]:
        if self._cars_only:
            return {"step": "1", "total_steps": "1"}
        steps = _active_steps(self._flow_kind, self._features())
        index = steps.index(logical) + 1 if logical in steps else len(steps)
        return {"step": str(index), "total_steps": str(len(steps))}

    async def _meter_step(self, step_id: str, user_input: dict[str, Any] | None):
        """Electricity meter, every setup (v2.41.0)."""
        if user_input is not None:
            self.fuse_info = _normalize_meter_input(user_input)
            return await self._go_next("meter")
        return self.async_show_form(
            step_id=step_id,
            data_schema=_meter_schema(self._base_data()),
            errors={},
            description_placeholders=self._placeholders("meter"),
        )

    async def _solar_step(self, step_id: str, user_input: dict[str, Any] | None):
        """Solar production (v2.41.0)."""
        if user_input is not None:
            self.sensor_info.update(_normalize_solar_input(user_input))
            return await self._go_next("solar")
        return self.async_show_form(
            step_id=step_id,
            data_schema=_solar_schema(self._base_data()),
            errors={},
            description_placeholders=self._placeholders("solar"),
        )

    async def _battery_step(self, step_id: str, user_input: dict[str, Any] | None):
        """Home battery (v2.41.0)."""
        if user_input is not None:
            self.sensor_info.update(_normalize_battery_input(user_input))
            return await self._go_next("battery")
        return self.async_show_form(
            step_id=step_id,
            data_schema=_battery_schema(self._base_data()),
            errors={},
            description_placeholders=self._placeholders("battery"),
        )

    async def _go_next(self, logical: str):
        steps = set(_active_steps(self._flow_kind, self._features()))
        # Walk the fixed order from the current step, so a step reached
        # without its answers (or one an answer just ruled out) still finds
        # the right successor.
        order = list(_STEP_ORDER)
        next_step = next(
            step for step in order[order.index(logical) + 1 :] if step in steps
        )
        step_id = _STEP_IDS[self._flow_kind][next_step]
        return await getattr(self, f"async_step_{step_id}")()

    # v2.31.0: car profiles ------------------------------------------------
    cars_info: list[dict] | None = None
    _editing_car: int | None = None
    _active_car: str | None = None

    _had_own_cars = False

    def _seed_cars(self) -> list[dict]:
        """The user's own cars for the editor (v2.32.0: the guest profile is
        implicit and has no sensors to edit)."""
        if self.cars_info is None:
            base = self._base_data()
            self.cars_info = get_own_cars(base)
            self._active_car = get_active_car_name(base)
            self._had_own_cars = bool(self.cars_info)
        return self.cars_info

    def _car_editor_step_id(self) -> str:
        return "reconfigure_car" if self._flow_kind == "reconfigure" else "car"

    async def _cars_step(self, user_input: dict[str, Any] | None):
        cars = self._seed_cars()
        step_id = _STEP_IDS[self._flow_kind]["cars"]
        if user_input is not None:
            action = str(user_input.get(_CAR_ACTION, "done"))
            if action == "add":
                self._editing_car = None
                return await getattr(self, f"async_step_{self._car_editor_step_id()}")()
            if action.startswith("edit:"):
                self._editing_car = int(action.split(":", 1)[1])
                return await getattr(self, f"async_step_{self._car_editor_step_id()}")()
            if action.startswith("remove:"):
                index = int(action.split(":", 1)[1])
                if 0 <= index < len(cars):
                    cars.pop(index)
                return await self._cars_step(None)
            if action == "done":
                if self._cars_only:
                    return await self._save_cars_only()
                return await self._go_next("cars")
        placeholders = self._placeholders("cars")
        placeholders["cars"] = _cars_list_text(cars, self._active_car)
        return self.async_show_form(
            step_id=step_id,
            data_schema=_cars_menu_schema(self.hass, cars, save=self._cars_only),
            errors={},
            description_placeholders=placeholders,
        )

    async def _car_step(self, user_input: dict[str, Any] | None):
        cars = self._seed_cars()
        current = cars[self._editing_car] if self._editing_car is not None else None
        errors: dict[str, str] = {}
        if user_input is not None:
            car = dict(user_input)
            car[CONF_CAR_NAME] = str(car.get(CONF_CAR_NAME) or "").strip()
            car[CONF_EV_SOC_LIMIT] = car.get(CONF_EV_SOC_LIMIT) or None
            car[CONF_CAR_LOCATION] = car.get(CONF_CAR_LOCATION) or None
            car[CONF_CAR_CONNECTION] = car.get(CONF_CAR_CONNECTION) or None
            others = [
                c[CONF_CAR_NAME] for i, c in enumerate(cars) if i != self._editing_car
            ]
            if not car[CONF_CAR_NAME]:
                errors[CONF_CAR_NAME] = "car_name_required"
            elif car[CONF_CAR_NAME].casefold() in RESERVED_CAR_NAMES:
                errors[CONF_CAR_NAME] = "car_name_reserved"
            elif car[CONF_CAR_NAME] in others:
                errors[CONF_CAR_NAME] = "car_name_exists"
            if not errors:
                if self._editing_car is None:
                    cars.append(car)
                else:
                    if current[CONF_CAR_NAME] == self._active_car:
                        self._active_car = car[CONF_CAR_NAME]
                    cars[self._editing_car] = car
                self._editing_car = None
                return await self._cars_step(None)
            current = car
        placeholders = self._placeholders("cars")
        return self.async_show_form(
            step_id=self._car_editor_step_id(),
            data_schema=_car_schema(current),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _with_cars(self, data: dict[str, Any]) -> dict[str, Any]:
        """Store the edited car list; the legacy keys follow the active car."""
        if self.cars_info is None:
            return apply_active_car(data)
        data = dict(data)
        data[CONF_CARS] = self.cars_info
        names = [car[CONF_CAR_NAME] for car in self.cars_info]
        active = self._active_car
        if active == GUEST_CAR and not self._had_own_cars and names:
            # The first car created becomes the active one; after that the
            # choice stays where the user put it.
            active = names[0]
        elif active != GUEST_CAR and active not in names:
            active = names[0] if names else GUEST_CAR
        return apply_active_car(data, active)



class EVSCConfigFlow(_StepNavigator, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the EV Smart Charger config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize mutable flow state."""
        self.init_info: dict[str, Any] = {}
        self.mode_info: dict[str, Any] = {}  # v2.0.0: phase_mode + charger_model
        self.features_info: dict[str, Any] = {}  # v2.17.0: setup answers
        self.charger_info: dict[str, Any] = {}
        self.sensor_info: dict[str, Any] = {}
        self.fuse_info: dict[str, Any] = {}  # v2.14.0 main fuse protection
        self.pv_forecast_info: dict[str, Any] = {}
        self.notifications_info: dict[str, Any] = {}
        self.external_info: dict[str, Any] = {}
        self._reconfigure_entry = None

    # The same class serves the initial flow and the reconfigure flow; the
    # reconfigure entry point switches the step-id mapping.
    def _base_data(self) -> dict[str, Any]:
        if self._reconfigure_entry is not None:
            return dict(self._reconfigure_entry.data)
        return {}

    # ------------------------------------------------------------------
    # initial flow
    # ------------------------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step where user provides a name."""
        if user_input is not None:
            self.init_info = user_input
            return await self._go_next("name")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_NAME, default=DEFAULT_NAME): str}),
            errors={},
            description_placeholders=self._placeholders("name"),
        )

    async def async_step_charger_model(self, user_input: dict[str, Any] | None = None):
        """Handle the charger-model selection step (tuya / generic) — v2.0.0."""
        if user_input is not None:
            self.mode_info.update(user_input)
            return await self._go_next("model")

        return self.async_show_form(
            step_id="charger_model",
            data_schema=_charger_model_schema(self.hass),
            errors={},
            description_placeholders=self._placeholders("model"),
        )

    async def async_step_features(self, user_input: dict[str, Any] | None = None):
        """"What do you have?" — solar, home battery, load balancing (v2.17.0)."""
        if user_input is not None:
            self.features_info = _normalize_features(user_input)
            return await self._go_next("features")

        return self.async_show_form(
            step_id="features",
            data_schema=_features_schema(self.hass),
            errors={},
            description_placeholders=self._placeholders("features"),
        )

    async def async_step_entities(self, user_input: dict[str, Any] | None = None):
        """Handle charger entity selection step."""
        errors = {}

        if user_input is not None:
            charger_info, errors = _normalize_charger_input(user_input)
            if not errors:
                await self.async_set_unique_id(_charger_control_id(charger_info))
                self._abort_if_unique_id_configured()
                self.charger_info = charger_info
                return await self._go_next("entities")

        return self.async_show_form(
            step_id="entities",
            data_schema=_charger_schema(user_input),
            errors=errors,
            description_placeholders=self._placeholders("entities"),
        )

    async def async_step_meter(self, user_input: dict[str, Any] | None = None):
        return await self._meter_step("meter", user_input)

    async def async_step_solar(self, user_input: dict[str, Any] | None = None):
        return await self._solar_step("solar", user_input)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        return await self._battery_step("battery", user_input)

    async def async_step_pv_forecast(self, user_input: dict[str, Any] | None = None):
        """Handle PV forecast entity selection step."""
        if user_input is not None:
            self.pv_forecast_info = user_input
            return await self._go_next("forecast")

        return self.async_show_form(
            step_id="pv_forecast",
            data_schema=_pv_forecast_schema(),
            errors={},
            description_placeholders=self._placeholders("forecast"),
        )

    async def async_step_notifications(self, user_input: dict[str, Any] | None = None):
        """Handle mobile notification services selection step."""
        if user_input is not None:
            self.notifications_info = user_input
            return await self._go_next("notifications")

        return self.async_show_form(
            step_id="notifications",
            data_schema=_notifications_schema(self.hass),
            errors={},
            description_placeholders=self._placeholders("notifications"),
        )

    async def async_step_external_connectors(self, user_input: dict[str, Any] | None = None):
        """Handle external connector configuration step."""
        errors = {}

        if user_input is not None:
            errors = _validate_external_connectors(self.hass, user_input)
            if not errors:
                # Write a cleared price sensor as None so it is really removed.
                user_input.setdefault(CONF_PRICE_SENSOR, None)
                self.external_info = user_input
                return await self._go_next("external")

        return self.async_show_form(
            step_id="external_connectors",
            data_schema=_external_connectors_schema(),
            errors=errors,
            description_placeholders=self._placeholders("external"),
        )

    async def async_step_cars(self, user_input: dict[str, Any] | None = None):
        """v2.32.0: car profiles; continuing without a car keeps the guest."""
        return await self._cars_step(user_input)

    async def async_step_car(self, user_input: dict[str, Any] | None = None):
        return await self._car_step(user_input)

    async def async_step_dashboard(self, user_input: dict[str, Any] | None = None):
        """Final step: auto-generate the Lovelace dashboard (v1.9.0+)."""
        if user_input is not None:
            data = self._with_cars(_apply_features(
                _merge_entry_data(
                    {},
                    self.init_info,
                    self.mode_info,
                    self.features_info,
                    self.charger_info,
                    self.sensor_info,
                    self.fuse_info,
                    self.pv_forecast_info,
                    self.notifications_info,
                    self.external_info,
                    user_input,
                )
            ))
            title = self.init_info.get(CONF_NAME, DEFAULT_NAME)
            return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="dashboard",
            data_schema=_dashboard_schema(),
            errors={},
            description_placeholders=self._placeholders("dashboard"),
        )

    # ------------------------------------------------------------------
    # reconfigure flow
    # ------------------------------------------------------------------
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Reconfigure entry point: charger model (v2.39.0; was phase mode)."""
        self._reconfigure_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reconfigure_entry is None:
            return self.async_abort(reason="unknown_entry")
        self._flow_kind = "reconfigure"

        if user_input is not None:
            self.mode_info.update(user_input)
            return await self._go_next("model")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_charger_model_schema(self.hass, self._reconfigure_entry.data),
            errors={},
            description_placeholders=self._placeholders("model"),
        )

    async def async_step_reconfigure_features(self, user_input: dict[str, Any] | None = None):
        """"What do you have?" during reconfigure (v2.17.0)."""
        if user_input is not None:
            self.features_info = _normalize_features(user_input)
            return await self._go_next("features")

        return self.async_show_form(
            step_id="reconfigure_features",
            data_schema=_features_schema(self.hass, self._reconfigure_entry.data),
            errors={},
            description_placeholders=self._placeholders("features"),
        )

    async def async_step_reconfigure_entities(self, user_input: dict[str, Any] | None = None):
        """Charger entity remapping during reconfigure."""
        errors: dict[str, str] = {}
        if user_input is not None:
            charger_info, errors = _normalize_charger_input(user_input)
            if not errors:
                if _is_duplicate_charger_switch(
                    self.hass,
                    _charger_control_id(charger_info),
                    exclude_entry_id=self._reconfigure_entry.entry_id,
                ):
                    return self.async_abort(reason="already_configured")
                self.charger_info = charger_info
                return await self._go_next("entities")

        return self.async_show_form(
            step_id="reconfigure_entities",
            data_schema=_charger_schema(user_input or self._reconfigure_entry.data),
            errors=errors,
            description_placeholders=self._placeholders("entities"),
        )

    async def async_step_reconfigure_meter(self, user_input: dict[str, Any] | None = None):
        return await self._meter_step("reconfigure_meter", user_input)

    async def async_step_reconfigure_solar(self, user_input: dict[str, Any] | None = None):
        return await self._solar_step("reconfigure_solar", user_input)

    async def async_step_reconfigure_battery(self, user_input: dict[str, Any] | None = None):
        return await self._battery_step("reconfigure_battery", user_input)

    async def async_step_reconfigure_pv_forecast(self, user_input: dict[str, Any] | None = None):
        """Handle PV forecast remapping during reconfigure."""
        if user_input is not None:
            self.pv_forecast_info = user_input
            return await self._go_next("forecast")

        return self.async_show_form(
            step_id="reconfigure_pv_forecast",
            data_schema=_pv_forecast_schema(self._reconfigure_entry.data),
            errors={},
            description_placeholders=self._placeholders("forecast"),
        )

    async def async_step_reconfigure_notifications(self, user_input: dict[str, Any] | None = None):
        """Handle notification remapping during reconfigure."""
        if user_input is not None:
            self.notifications_info = user_input
            return await self._go_next("notifications")

        return self.async_show_form(
            step_id="reconfigure_notifications",
            data_schema=_notifications_schema(self.hass, self._reconfigure_entry.data),
            errors={},
            description_placeholders=self._placeholders("notifications"),
        )

    async def async_step_reconfigure_external_connectors(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Handle external connector remapping during reconfigure."""
        errors = {}

        if user_input is not None:
            errors = _validate_external_connectors(self.hass, user_input)
            if not errors:
                # Write a cleared price sensor as None so it is really removed.
                user_input.setdefault(CONF_PRICE_SENSOR, None)
                self.external_info = user_input
                return await self._go_next("external")

        return self.async_show_form(
            step_id="reconfigure_external_connectors",
            data_schema=_external_connectors_schema(self._reconfigure_entry.data),
            errors=errors,
            description_placeholders=self._placeholders("external"),
        )

    async def async_step_reconfigure_cars(self, user_input: dict[str, Any] | None = None):
        """Car profiles (v2.31.0)."""
        return await self._cars_step(user_input)

    async def async_step_reconfigure_car(self, user_input: dict[str, Any] | None = None):
        """Add or edit one car profile (v2.31.0)."""
        return await self._car_step(user_input)

    async def async_step_reconfigure_dashboard(
        self, user_input: dict[str, Any] | None = None
    ):
        """Final step of reconfigure: auto-generated dashboard toggle."""
        if user_input is not None:
            updated_data = self._with_cars(_apply_features(
                _merge_entry_data(
                    self._reconfigure_entry.data,
                    self.mode_info,
                    self.features_info,
                    self.charger_info,
                    self.sensor_info,
                    self.fuse_info,
                    self.pv_forecast_info,
                    self.notifications_info,
                    self.external_info,
                    user_input,
                )
            ))
            self.hass.config_entries.async_update_entry(
                self._reconfigure_entry,
                data=updated_data,
                unique_id=_charger_control_id(updated_data),
            )
            return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure_dashboard",
            data_schema=_dashboard_schema(self._reconfigure_entry.data),
            errors={},
            description_placeholders=self._placeholders("dashboard"),
        )

    def _get_mobile_notify_services(self) -> list[str]:
        """Get list of available mobile_app notify services."""
        return _get_mobile_notify_services(self.hass)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return EVSCOptionsFlow(config_entry)


class EVSCOptionsFlow(_StepNavigator, config_entries.OptionsFlowWithConfigEntry):
    """Compatibility wrapper around the canonical reconfigure fields."""

    _flow_kind = "options"

    def __init__(self, config_entry):
        """Initialize options flow."""
        super().__init__(config_entry)
        self.mode_info: dict[str, Any] = {}  # v2.0.0: phase_mode + charger_model
        self.features_info: dict[str, Any] = {}  # v2.17.0: setup answers
        self.charger_info: dict[str, Any] = {}
        self.sensor_info: dict[str, Any] = {}
        self.fuse_info: dict[str, Any] = {}  # v2.14.0 main fuse protection
        self.pv_forecast_info: dict[str, Any] = {}
        self.notifications_info: dict[str, Any] = {}
        self.external_info: dict[str, Any] = {}

    def _base_data(self) -> dict[str, Any]:
        return dict(self.config_entry.data)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Options entry point (v2.43.0): only the cars, or every setting."""
        return self.async_show_menu(
            step_id="init", menu_options=["cars_only", "charger_model"]
        )

    async def async_step_cars_only(self, user_input: dict[str, Any] | None = None):
        """Edit the car profiles without the other steps (v2.43.0)."""
        self._cars_only = True
        return await self._cars_step(None)

    async def _save_cars_only(self):
        updated_data = self._with_cars(dict(self.config_entry.data))
        self.hass.config_entries.async_update_entry(self.config_entry, data=updated_data)
        return self.async_create_entry(title="", data={})

    async def async_step_charger_model(self, user_input: dict[str, Any] | None = None):
        """All settings: the charger model comes first (v2.39.0)."""
        if user_input is not None:
            self.mode_info.update(user_input)
            return await self._go_next("model")

        return self.async_show_form(
            step_id="charger_model",
            data_schema=_charger_model_schema(self.hass, self.config_entry.data),
            description_placeholders=self._placeholders("model"),
        )

    async def async_step_features(self, user_input: dict[str, Any] | None = None):
        """"What do you have?" (v2.17.0)."""
        if user_input is not None:
            self.features_info = _normalize_features(user_input)
            return await self._go_next("features")

        return self.async_show_form(
            step_id="features",
            data_schema=_features_schema(self.hass, self.config_entry.data),
            description_placeholders=self._placeholders("features"),
        )

    async def async_step_entities(self, user_input: dict[str, Any] | None = None):
        """Manage charger entities options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            charger_info, errors = _normalize_charger_input(user_input)
            if not errors:
                if _is_duplicate_charger_switch(
                    self.hass,
                    _charger_control_id(charger_info),
                    exclude_entry_id=self.config_entry.entry_id,
                ):
                    return self.async_abort(reason="already_configured")
                self.charger_info = charger_info
                return await self._go_next("entities")

        return self.async_show_form(
            step_id="entities",
            data_schema=_charger_schema(user_input or self.config_entry.data),
            errors=errors,
            description_placeholders=self._placeholders("entities"),
        )

    async def async_step_meter(self, user_input: dict[str, Any] | None = None):
        return await self._meter_step("meter", user_input)

    async def async_step_solar(self, user_input: dict[str, Any] | None = None):
        return await self._solar_step("solar", user_input)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        return await self._battery_step("battery", user_input)

    async def async_step_pv_forecast(self, user_input: dict[str, Any] | None = None):
        """Manage PV forecast entity options."""
        if user_input is not None:
            self.pv_forecast_info = user_input
            return await self._go_next("forecast")

        return self.async_show_form(
            step_id="pv_forecast",
            data_schema=_pv_forecast_schema(self.config_entry.data),
            description_placeholders=self._placeholders("forecast"),
        )

    async def async_step_notifications(self, user_input: dict[str, Any] | None = None):
        """Manage mobile notification services options."""
        if user_input is not None:
            self.notifications_info = user_input
            return await self._go_next("notifications")

        return self.async_show_form(
            step_id="notifications",
            data_schema=_notifications_schema(self.hass, self.config_entry.data),
            description_placeholders=self._placeholders("notifications"),
        )

    async def async_step_external_connectors(self, user_input: dict[str, Any] | None = None):
        """Handle external connector updates from the compatibility options flow."""
        errors = {}

        if user_input is not None:
            errors = _validate_external_connectors(self.hass, user_input)
            if not errors:
                # Write a cleared price sensor as None so it is really removed.
                user_input.setdefault(CONF_PRICE_SENSOR, None)
                self.external_info = user_input
                return await self._go_next("external")

        return self.async_show_form(
            step_id="external_connectors",
            data_schema=_external_connectors_schema(self.config_entry.data),
            errors=errors,
            description_placeholders=self._placeholders("external"),
        )

    async def async_step_cars(self, user_input: dict[str, Any] | None = None):
        """Car profiles (v2.31.0)."""
        return await self._cars_step(user_input)

    async def async_step_car(self, user_input: dict[str, Any] | None = None):
        """Add or edit one car profile (v2.31.0)."""
        return await self._car_step(user_input)

    async def async_step_dashboard(self, user_input: dict[str, Any] | None = None):
        """Final options step: auto-generated dashboard toggle."""
        if user_input is not None:
            updated_data = self._with_cars(_apply_features(
                _merge_entry_data(
                    self.config_entry.data,
                    self.mode_info,
                    self.features_info,
                    self.charger_info,
                    self.sensor_info,
                    self.fuse_info,
                    self.pv_forecast_info,
                    self.notifications_info,
                    self.external_info,
                    user_input,
                )
            ))
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=updated_data,
                unique_id=_charger_control_id(updated_data),
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="dashboard",
            data_schema=_dashboard_schema(self.config_entry.data),
            description_placeholders=self._placeholders("dashboard"),
        )

    def _get_mobile_notify_services(self) -> list[str]:
        """Get list of available mobile_app notify services."""
        return _get_mobile_notify_services(self.hass)
