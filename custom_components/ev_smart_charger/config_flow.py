from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
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
    CONF_HOME_CONSUMPTION,
    CONF_HOME_CONSUMPTION_L2,
    CONF_HOME_CONSUMPTION_L3,
    CONF_HYBRID_INVERTER_MODE,
    CONF_NOTIFY_SERVICES,
    CONF_PHASE_MODE,
    CONF_PV_FORECAST,
    CONF_PV_FORECAST_TOMORROW,
    CONF_SOC_CAR,
    SOC_INPUT_DOMAINS,
    CONF_SOC_HOME,
    CHARGER_MODEL_GENERIC,
    CHARGER_MODEL_TUYA,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CHARGER_MODEL,
    DEFAULT_CREATE_DASHBOARD,
    DEFAULT_HYBRID_INVERTER_MODE,
    DEFAULT_NAME,
    DEFAULT_PHASE_MODE,
    DOMAIN,
    MAX_BATTERY_CAPACITY,
    MIN_BATTERY_CAPACITY,
    PHASE_MODE_SINGLE,
    PHASE_MODE_THREE,
    CONF_FUSE_CHARGER_CURRENT,
    CONF_MAIN_FUSE_SIZE,
    FUSE_CHARGER_CURRENT_KEYS,
    FUSE_ENTITY_KEYS,
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
    LOAD_BALANCING_CHARGER,
    LOAD_BALANCING_INTEGRATION,
    LOAD_BALANCING_NONE,
    LOAD_BALANCING_OPTIONS,
    get_load_balancing,
    get_main_fuse_size,
    has_meter_import_power,
    has_solar,
    is_three_phase,
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
    ):
        data[key] = data.get(key) or None

    if data[CONF_EV_CHARGER_SWITCH]:
        data[CONF_EV_CHARGER_START_BUTTON] = None
        data[CONF_EV_CHARGER_STOP_BUTTON] = None
        return data, {}
    if data[CONF_EV_CHARGER_START_BUTTON] and data[CONF_EV_CHARGER_STOP_BUTTON]:
        return data, {}
    return data, {"base": "charger_control_required"}


_OPTIONAL_PHASE_POWER_KEYS = (
    CONF_FV_PRODUCTION_L2,
    CONF_FV_PRODUCTION_L3,
    CONF_HOME_CONSUMPTION_L2,
    CONF_HOME_CONSUMPTION_L3,
    CONF_GRID_IMPORT_L2,
    CONF_GRID_IMPORT_L3,
    # v2.18.0: optional when they can be derived from the meter step.
    CONF_GRID_IMPORT,
    CONF_HOME_CONSUMPTION,
)


def _normalize_sensor_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Write cleared optional L2/L3 power sensors as None (v2.16.1).

    An empty optional field is absent from ``user_input``; without this the
    reconfigure merge would keep the previously mapped phase sensor.
    """
    data = dict(user_input)
    for key in _OPTIONAL_PHASE_POWER_KEYS:
        data[key] = data.get(key) or None
    return data


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


def _charger_schema(current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the charger entities schema."""
    current_data = current_data or {}
    return vol.Schema(
        {
            # v2.14.0 (issue #65): the on/off control is either a switch or a
            # start/stop button pair. suggested_value (not default) so a field
            # can be cleared when moving from one to the other.
            vol.Optional(
                CONF_EV_CHARGER_SWITCH,
                description={
                    "suggested_value": current_data.get(CONF_EV_CHARGER_SWITCH)
                },
            ): _entity_selector("switch"),
            vol.Optional(
                CONF_EV_CHARGER_START_BUTTON,
                description={
                    "suggested_value": current_data.get(CONF_EV_CHARGER_START_BUTTON)
                },
            ): _entity_selector("button"),
            vol.Optional(
                CONF_EV_CHARGER_STOP_BUTTON,
                description={
                    "suggested_value": current_data.get(CONF_EV_CHARGER_STOP_BUTTON)
                },
            ): _entity_selector("button"),
            vol.Required(
                CONF_EV_CHARGER_CURRENT,
                **_field_config(current_data.get(CONF_EV_CHARGER_CURRENT)),
            ): _entity_selector(CURRENT_CONTROL_DOMAINS),
            # v2.2.0: Optional (was Required). Now a FALLBACK for the charging
            # power SSOT (used when no charging-power sensor is mapped) and the
            # source for plug/idle/finished lifecycle that power cannot express.
            # Never removed — kept prefilled in reconfigure/options so users don't
            # silently drop their fallback. No "Required-if-set" lock (unlike
            # soc_home) because status maps to zero helper entities → no orphans.
            vol.Optional(
                CONF_EV_CHARGER_STATUS,
                **_field_config(current_data.get(CONF_EV_CHARGER_STATUS)),
            ): _entity_selector("sensor"),
        }
    )


# v2.0.0: radio option labels per language. The longer per-option explanations
# live in strings.json step `description` (which HA localizes normally); these are
# just the short radio labels. Localized in Python — instead of a selector
# `translation_key` — so it works across HA versions (older cores reject
# translation_key on a SelectSelectorConfig during form serialization).
_RADIO_LABELS: dict[str, dict[str, dict[str, str]]] = {
    CONF_PHASE_MODE: {
        "en": {PHASE_MODE_SINGLE: "Single-phase", PHASE_MODE_THREE: "Three-phase"},
        "it": {PHASE_MODE_SINGLE: "Monofase", PHASE_MODE_THREE: "Trifase"},
        "nl": {PHASE_MODE_SINGLE: "Eénfase", PHASE_MODE_THREE: "Driefase"},
    },
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


def _phase_mode_schema(hass, current_data: dict[str, Any] | None = None) -> vol.Schema:
    """Build the phase-mode radio schema (single / three) — v2.0.0."""
    current_data = current_data or {}
    return _radio_schema(
        hass,
        CONF_PHASE_MODE,
        [PHASE_MODE_SINGLE, PHASE_MODE_THREE],
        current_data.get(CONF_PHASE_MODE, DEFAULT_PHASE_MODE),
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


def _sensor_schema(
    current_data: dict[str, Any] | None = None,
    three_phase: bool = False,
    *,
    solar: bool = True,
    home_battery: bool = True,
    derivable: bool = False,
) -> vol.Schema:
    """Build the sensor mapping schema.

    v2.18.0: ``derivable`` (the meter step maps import power on every phase
    and the charger current) makes grid import and home consumption optional:
    left empty, the integration computes them.

    v2.0.0: in three-phase mode, production / home-consumption / grid-import are
    asked per phase (L1 reuses the single-phase key; L2/L3 optional since
    v2.16.1). v2.17.0: only the fields that match the setup answers are shown —
    no home-battery SOC without a home battery, no production / consumption
    without solar panels.
    """
    current_data = current_data or {}

    fields: dict[Any, Any] = {
        vol.Required(CONF_SOC_CAR, **_field_config(current_data.get(CONF_SOC_CAR))): _entity_selector(SOC_INPUT_DOMAINS),
    }
    if home_battery:
        fields[
            vol.Required(CONF_SOC_HOME, **_field_config(current_data.get(CONF_SOC_HOME)))
        ] = _entity_selector(SOC_INPUT_DOMAINS)

    # Per-quantity power sensors, grouped L1[/L2/L3]. Single-phase = L1 only.
    power_groups = [(CONF_GRID_IMPORT, CONF_GRID_IMPORT_L2, CONF_GRID_IMPORT_L3)]
    if solar:
        power_groups = [
            (CONF_FV_PRODUCTION, CONF_FV_PRODUCTION_L2, CONF_FV_PRODUCTION_L3),
            (CONF_HOME_CONSUMPTION, CONF_HOME_CONSUMPTION_L2, CONF_HOME_CONSUMPTION_L3),
            *power_groups,
        ]
    for l1, l2, l3 in power_groups:
        marker = vol.Required
        if derivable and l1 in (CONF_GRID_IMPORT, CONF_HOME_CONSUMPTION):
            marker = vol.Optional
        fields[marker(l1, **_field_config(current_data.get(l1)))] = _entity_selector("sensor")
        if three_phase:
            # v2.16.1: L2/L3 optional. Only the sum is ever used, so an install
            # with a single total sensor (e.g. an inverter that reports PV
            # production as a total plus VA per phase) maps the total to L1.
            fields[vol.Optional(l2, **_field_config(current_data.get(l2)))] = _entity_selector("sensor")
            fields[vol.Optional(l3, **_field_config(current_data.get(l3)))] = _entity_selector("sensor")

    # v2.2.0: measured EV charging power — the SSOT for "is the car drawing now".
    # Optional (most installs lack a charger CT; the switch echo + status string
    # are the fallback). Single-phase = 1 sensor; three-phase = 3 summed. The
    # "all three or none" rule is enforced in ChargingModel.read_charging_power
    # (Optional fields cannot be made schema-Required without breaking the
    # single-phase / no-sensor cases).
    fields[
        vol.Optional(CONF_CHARGING_POWER, **_field_config(current_data.get(CONF_CHARGING_POWER)))
    ] = _entity_selector("sensor")
    if three_phase:
        fields[
            vol.Optional(CONF_CHARGING_POWER_L2, **_field_config(current_data.get(CONF_CHARGING_POWER_L2)))
        ] = _entity_selector("sensor")
        fields[
            vol.Optional(CONF_CHARGING_POWER_L3, **_field_config(current_data.get(CONF_CHARGING_POWER_L3)))
        ] = _entity_selector("sensor")

    # v2.6.0 (issue #36): optional grid-availability binary_sensor. When mapped
    # and OFF, Night Smart Charge grid mode stops (avoids draining the home
    # battery during a grid outage on hybrid Battery First/UPS inverters).
    # Unmapped → byte-for-byte legacy behaviour.
    fields[
        vol.Optional(CONF_GRID_AVAILABLE, **_field_config(current_data.get(CONF_GRID_AVAILABLE)))
    ] = _entity_selector("binary_sensor")

    return vol.Schema(fields)


def _main_fuse_schema(
    hass,
    current_data: dict[str, Any] | None = None,
    three_phase: bool = False,
) -> vol.Schema:
    """Build the main-fuse protection step schema (v2.14.0).

    The fuse size picks whether protection is active (0 = off). The meter
    sensors are asked per phase (L1 only in single-phase). Everything is
    optional at schema level; ``_validate_main_fuse`` enforces the minimum set
    once a fuse size is chosen.
    """
    current_data = current_data or {}
    # v2.17.0: the step is only shown when the user chose load balancing by
    # the integration, so "off" is not offered here.
    sizes = [size for size in MAIN_FUSE_SIZES if size > 0]
    current_size = get_main_fuse_size(current_data) or DEFAULT_LOAD_BALANCING_FUSE_SIZE
    if current_size not in sizes:
        current_size = DEFAULT_LOAD_BALANCING_FUSE_SIZE

    fields: dict[Any, Any] = {
        vol.Required(CONF_MAIN_FUSE_SIZE, default=str(current_size)): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {"value": str(size), "label": f"{size} A"}
                    for size in sizes
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    }
    phases = 3 if three_phase else 1
    for keys in (
        FUSE_GRID_CURRENT_KEYS,
        FUSE_GRID_IMPORT_KEYS,
        FUSE_GRID_EXPORT_KEYS,
        FUSE_GRID_VOLTAGE_KEYS,
    ):
        for key in keys[:phases]:
            fields[vol.Optional(key, **_field_config(current_data.get(key)))] = (
                _entity_selector("sensor")
            )
    fields[
        vol.Optional(
            CONF_FUSE_CHARGER_CURRENT,
            **_field_config(current_data.get(CONF_FUSE_CHARGER_CURRENT)),
        )
    ] = _entity_selector("sensor")
    for key in FUSE_CHARGER_CURRENT_KEYS[:phases]:
        fields[vol.Optional(key, **_field_config(current_data.get(key)))] = (
            _entity_selector("sensor")
        )
    return vol.Schema(fields)


def _normalize_main_fuse_input(user_input: dict[str, Any], three_phase: bool) -> dict[str, Any]:
    """Turn the form values into entry data.

    * fuse size string -> int
    * every fuse entity key is written explicitly (None when left empty), so
      clearing a field during reconfigure really removes the old mapping
      instead of ``_merge_entry_data`` silently keeping it
    * L2/L3 keys are dropped (None) in single-phase
    """
    phases = 3 if three_phase else 1
    data: dict[str, Any] = {CONF_MAIN_FUSE_SIZE: get_main_fuse_size(user_input)}
    allowed = {CONF_FUSE_CHARGER_CURRENT}
    for keys in (
        FUSE_GRID_CURRENT_KEYS,
        FUSE_GRID_IMPORT_KEYS,
        FUSE_GRID_EXPORT_KEYS,
        FUSE_GRID_VOLTAGE_KEYS,
        FUSE_CHARGER_CURRENT_KEYS,
    ):
        allowed.update(keys[:phases])
    for key in FUSE_ENTITY_KEYS:
        value = user_input.get(key) if key in allowed else None
        data[key] = value or None
    return data


def _validate_main_fuse(data: dict[str, Any], three_phase: bool) -> dict[str, str]:
    """Check that an enabled fuse protection has the readings it needs."""
    if not get_main_fuse_size(data):
        return {}
    errors: dict[str, str] = {}
    phases = 3 if three_phase else 1
    for index in range(phases):
        has_current = bool(data.get(FUSE_GRID_CURRENT_KEYS[index]))
        has_import = bool(data.get(FUSE_GRID_IMPORT_KEYS[index]))
        if not has_current and not has_import:
            errors[FUSE_GRID_CURRENT_KEYS[index]] = "fuse_phase_reading_required"
    has_charger = bool(data.get(CONF_FUSE_CHARGER_CURRENT)) or any(
        data.get(key) for key in FUSE_CHARGER_CURRENT_KEYS[:phases]
    )
    if not has_charger:
        errors[CONF_FUSE_CHARGER_CURRENT] = "fuse_charger_current_required"
    return errors


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
            vol.Optional(
                CONF_PV_FORECAST,
                **_field_config(current_data.get(CONF_PV_FORECAST)),
            ): _entity_selector("sensor"),
            vol.Optional(
                CONF_PV_FORECAST_TOMORROW,
                **_field_config(current_data.get(CONF_PV_FORECAST_TOMORROW)),
            ): _entity_selector("sensor"),
        }
    )


def _hybrid_inverter_schema(
    current_data: dict[str, Any] | None = None,
    *,
    include_toggle: bool = False,
    home_battery: bool = True,
) -> vol.Schema:
    """Build the Hybrid Inverter Mode step schema (v2.1.0 — issue #29).

    Always offers the optional signed battery-power sensor (CONF_BATTERY_POWER,
    single — never per-phase, like SOC). The enable toggle is shown only in the
    initial flow (``include_toggle=True``): it seeds the first-run state of the
    ``evsc_hybrid_inverter_mode`` switch. In reconfigure/options the switch is
    the live source of truth, so only the sensor is editable there (omitting the
    toggle key preserves the existing entry.data value via ``_merge_entry_data``).
    """
    current_data = current_data or {}
    fields: dict[Any, Any] = {}
    if include_toggle:
        fields[
            vol.Optional(
                CONF_HYBRID_INVERTER_MODE,
                default=current_data.get(
                    CONF_HYBRID_INVERTER_MODE, DEFAULT_HYBRID_INVERTER_MODE
                ),
            )
        ] = selector.BooleanSelector()
    if home_battery:
        fields[
            vol.Optional(
                CONF_BATTERY_POWER,
                **_field_config(current_data.get(CONF_BATTERY_POWER)),
            )
        ] = _entity_selector("sensor")
    return vol.Schema(fields)


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
            vol.Required(
                CONF_BATTERY_CAPACITY,
                **_field_config(
                    current_data.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
                ),
            ): vol.All(
                vol.Coerce(float),
                vol.Range(min=MIN_BATTERY_CAPACITY, max=MAX_BATTERY_CAPACITY),
            ),
            vol.Optional(
                CONF_ENERGY_FORECAST_TARGET,
                **_field_config(current_data.get(CONF_ENERGY_FORECAST_TARGET)),
            ): _entity_selector(ENERGY_TARGET_DOMAINS),
            # v2.20.0: electricity price sensor for planned night charging
            # (Nord Pool core or HACS, or any sensor with raw_today/raw_tomorrow).
            vol.Optional(
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
        LOAD_BALANCING_INTEGRATION: "Yes, this integration protects the main fuse",
        LOAD_BALANCING_CHARGER: "Yes, the charger handles it itself",
    },
    "it": {
        LOAD_BALANCING_NONE: "Nessuno",
        LOAD_BALANCING_INTEGRATION: "Sì, questa integrazione protegge il contatore",
        LOAD_BALANCING_CHARGER: "Sì, lo gestisce il caricatore",
    },
    "nl": {
        LOAD_BALANCING_NONE: "Geen",
        LOAD_BALANCING_INTEGRATION: "Ja, deze integratie bewaakt de hoofdzekering",
        LOAD_BALANCING_CHARGER: "Ja, de lader regelt het zelf",
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
        data[CONF_HYBRID_INVERTER_MODE] = False
    if get_load_balancing(data) != LOAD_BALANCING_INTEGRATION:
        data[CONF_MAIN_FUSE_SIZE] = 0
        for key in FUSE_ENTITY_KEYS:
            data[key] = None
    return data


# Logical step order shared by the three flows. "name" is initial-flow only.
_STEP_ORDER = (
    "name",
    "phase",
    "model",
    "features",
    "entities",
    # v2.18.0: the meter step comes before the sensors step, so the sensors
    # step knows whether grid import / home consumption can be derived.
    "fuse",
    "sensors",
    "hybrid",
    "forecast",
    "notifications",
    "external",
    "dashboard",
)

# Logical step -> step_id per flow kind.
_STEP_IDS: dict[str, dict[str, str]] = {
    "initial": {
        "name": "user",
        "phase": "phase_mode",
        "model": "charger_model",
        "features": "features",
        "entities": "entities",
        "sensors": "sensors",
        "hybrid": "hybrid_inverter",
        "fuse": "main_fuse",
        "forecast": "pv_forecast",
        "notifications": "notifications",
        "external": "external_connectors",
        "dashboard": "dashboard",
    },
    "reconfigure": {
        "phase": "reconfigure",
        "model": "reconfigure_charger_model",
        "features": "reconfigure_features",
        "entities": "reconfigure_entities",
        "sensors": "reconfigure_sensors",
        "hybrid": "reconfigure_hybrid_inverter",
        "fuse": "reconfigure_main_fuse",
        "forecast": "reconfigure_pv_forecast",
        "notifications": "reconfigure_notifications",
        "external": "reconfigure_external_connectors",
        "dashboard": "reconfigure_dashboard",
    },
    "options": {
        "phase": "init",
        "model": "charger_model",
        "features": "features",
        "entities": "entities",
        "sensors": "sensors",
        "hybrid": "hybrid_inverter",
        "fuse": "main_fuse",
        "forecast": "pv_forecast",
        "notifications": "notifications",
        "external": "external_connectors",
        "dashboard": "dashboard",
    },
}


def _active_steps(kind: str, features: dict[str, Any]) -> list[str]:
    """Return the logical steps shown for these setup answers."""
    steps = []
    for step in _STEP_ORDER:
        if step not in _STEP_IDS[kind]:
            continue
        if step in ("hybrid", "forecast") and not has_solar(features):
            continue
        if step == "fuse" and get_load_balancing(features) != LOAD_BALANCING_INTEGRATION:
            continue
        steps.append(step)
    return steps


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

    def _derivable(self) -> bool:
        """v2.18.0: can grid import / home consumption be computed?

        Needs the meter step (load balancing by the integration) with import
        power on every phase; that step also requires the charger current.
        """
        data = _merge_entry_data(
            self._base_data(), self.mode_info, self._features(), self.fuse_info
        )
        return has_meter_import_power(data)

    def _placeholders(self, logical: str) -> dict[str, str]:
        steps = _active_steps(self._flow_kind, self._features())
        index = steps.index(logical) + 1 if logical in steps else len(steps)
        return {"step": str(index), "total_steps": str(len(steps))}

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
        self.hybrid_info: dict[str, Any] = {}  # v2.1.0 (issue #29)
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

    async def async_step_phase_mode(self, user_input: dict[str, Any] | None = None):
        """Handle the phase-mode selection step (single / three) — v2.0.0."""
        if user_input is not None:
            self.mode_info.update(user_input)
            return await self._go_next("phase")

        return self.async_show_form(
            step_id="phase_mode",
            data_schema=_phase_mode_schema(self.hass),
            errors={},
            description_placeholders=self._placeholders("phase"),
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

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None):
        """Handle sensor entity selection step."""
        if user_input is not None:
            self.sensor_info = _normalize_sensor_input(user_input)
            return await self._go_next("sensors")

        features = self._features()
        return self.async_show_form(
            step_id="sensors",
            data_schema=_sensor_schema(
                three_phase=is_three_phase(self.mode_info),
                solar=has_solar(features),
                home_battery=wants_home_battery(features),
                derivable=self._derivable(),
            ),
            errors={},
            description_placeholders=self._placeholders("sensors"),
        )

    async def async_step_hybrid_inverter(self, user_input: dict[str, Any] | None = None):
        """Hybrid Inverter Mode: enable toggle + battery-power sensor (v2.1.0)."""
        if user_input is not None:
            self.hybrid_info = user_input
            return await self._go_next("hybrid")

        return self.async_show_form(
            step_id="hybrid_inverter",
            data_schema=_hybrid_inverter_schema(
                include_toggle=True,
                home_battery=wants_home_battery(self._features()),
            ),
            errors={},
            description_placeholders=self._placeholders("hybrid"),
        )

    async def async_step_main_fuse(self, user_input: dict[str, Any] | None = None):
        """Main fuse protection: fuse size + meter/charger current sensors (v2.14.0)."""
        return await self._fuse_step("main_fuse", user_input, {})

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

    async def async_step_dashboard(self, user_input: dict[str, Any] | None = None):
        """Final step: auto-generate the Lovelace dashboard (v1.9.0+)."""
        if user_input is not None:
            data = _apply_features(
                _merge_entry_data(
                    {},
                    self.init_info,
                    self.mode_info,
                    self.features_info,
                    self.charger_info,
                    self.sensor_info,
                    self.hybrid_info,
                    self.fuse_info,
                    self.pv_forecast_info,
                    self.notifications_info,
                    self.external_info,
                    user_input,
                )
            )
            title = self.init_info.get(CONF_NAME, DEFAULT_NAME)
            return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="dashboard",
            data_schema=_dashboard_schema(),
            errors={},
            description_placeholders=self._placeholders("dashboard"),
        )

    async def _fuse_step(
        self, step_id: str, user_input: dict[str, Any] | None, current: dict[str, Any]
    ):
        """Shared main-fuse step for the initial and the reconfigure flow."""
        three_phase = is_three_phase(self.mode_info)
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize_main_fuse_input(user_input, three_phase)
            errors = _validate_main_fuse(data, three_phase)
            if not errors:
                self.fuse_info = data
                return await self._go_next("fuse")
            current = data

        return self.async_show_form(
            step_id=step_id,
            data_schema=_main_fuse_schema(self.hass, current, three_phase),
            errors=errors,
            description_placeholders=self._placeholders("fuse"),
        )

    # ------------------------------------------------------------------
    # reconfigure flow
    # ------------------------------------------------------------------
    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Reconfigure entry point: phase-mode selection (v2.0.0)."""
        self._reconfigure_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reconfigure_entry is None:
            return self.async_abort(reason="unknown_entry")
        self._flow_kind = "reconfigure"

        if user_input is not None:
            self.mode_info.update(user_input)
            return await self._go_next("phase")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_phase_mode_schema(self.hass, self._reconfigure_entry.data),
            errors={},
            description_placeholders=self._placeholders("phase"),
        )

    async def async_step_reconfigure_charger_model(self, user_input: dict[str, Any] | None = None):
        """Charger-model selection during reconfigure (v2.0.0)."""
        if user_input is not None:
            self.mode_info.update(user_input)
            return await self._go_next("model")

        return self.async_show_form(
            step_id="reconfigure_charger_model",
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

    async def async_step_reconfigure_sensors(self, user_input: dict[str, Any] | None = None):
        """Handle sensor remapping during reconfigure."""
        if user_input is not None:
            self.sensor_info = _normalize_sensor_input(user_input)
            return await self._go_next("sensors")

        features = self._features()
        return self.async_show_form(
            step_id="reconfigure_sensors",
            data_schema=_sensor_schema(
                self._reconfigure_entry.data,
                three_phase=is_three_phase(self.mode_info),
                solar=has_solar(features),
                home_battery=wants_home_battery(features),
                derivable=self._derivable(),
            ),
            errors={},
            description_placeholders=self._placeholders("sensors"),
        )

    async def async_step_reconfigure_hybrid_inverter(
        self, user_input: dict[str, Any] | None = None
    ):
        """Hybrid Inverter Mode battery-power sensor remap (v2.1.0).

        Sensor only — the enable toggle lives on the live switch, not entry.data,
        so it is omitted here (omission preserves the existing value via merge).
        """
        if user_input is not None:
            self.hybrid_info = user_input
            return await self._go_next("hybrid")

        return self.async_show_form(
            step_id="reconfigure_hybrid_inverter",
            data_schema=_hybrid_inverter_schema(
                self._reconfigure_entry.data,
                home_battery=wants_home_battery(self._features()),
            ),
            errors={},
            description_placeholders=self._placeholders("hybrid"),
        )

    async def async_step_reconfigure_main_fuse(self, user_input: dict[str, Any] | None = None):
        """Main fuse protection: fuse size + meter/charger current sensors (v2.14.0)."""
        return await self._fuse_step(
            "reconfigure_main_fuse", user_input, dict(self._reconfigure_entry.data)
        )

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

    async def async_step_reconfigure_dashboard(
        self, user_input: dict[str, Any] | None = None
    ):
        """Final step of reconfigure: auto-generated dashboard toggle."""
        if user_input is not None:
            updated_data = _apply_features(
                _merge_entry_data(
                    self._reconfigure_entry.data,
                    self.mode_info,
                    self.features_info,
                    self.charger_info,
                    self.sensor_info,
                    self.hybrid_info,
                    self.fuse_info,
                    self.pv_forecast_info,
                    self.notifications_info,
                    self.external_info,
                    user_input,
                )
            )
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
        self.hybrid_info: dict[str, Any] = {}  # v2.1.0 (issue #29)
        self.fuse_info: dict[str, Any] = {}  # v2.14.0 main fuse protection
        self.pv_forecast_info: dict[str, Any] = {}
        self.notifications_info: dict[str, Any] = {}
        self.external_info: dict[str, Any] = {}

    def _base_data(self) -> dict[str, Any]:
        return dict(self.config_entry.data)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Options entry point: phase-mode selection (v2.0.0)."""
        if user_input is not None:
            self.mode_info.update(user_input)
            return await self._go_next("phase")

        return self.async_show_form(
            step_id="init",
            data_schema=_phase_mode_schema(self.hass, self.config_entry.data),
            description_placeholders=self._placeholders("phase"),
        )

    async def async_step_charger_model(self, user_input: dict[str, Any] | None = None):
        """Charger-model selection (tuya / generic) — v2.0.0."""
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

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None):
        """Manage sensor entities options."""
        if user_input is not None:
            self.sensor_info = _normalize_sensor_input(user_input)
            return await self._go_next("sensors")

        features = self._features()
        return self.async_show_form(
            step_id="sensors",
            data_schema=_sensor_schema(
                self.config_entry.data,
                three_phase=is_three_phase(self.mode_info),
                solar=has_solar(features),
                home_battery=wants_home_battery(features),
                derivable=self._derivable(),
            ),
            description_placeholders=self._placeholders("sensors"),
        )

    async def async_step_hybrid_inverter(self, user_input: dict[str, Any] | None = None):
        """Manage the Hybrid Inverter Mode battery-power sensor (v2.1.0).

        Sensor only (the enable toggle lives on the live switch).
        """
        if user_input is not None:
            self.hybrid_info = user_input
            return await self._go_next("hybrid")

        return self.async_show_form(
            step_id="hybrid_inverter",
            data_schema=_hybrid_inverter_schema(
                self.config_entry.data,
                home_battery=wants_home_battery(self._features()),
            ),
            description_placeholders=self._placeholders("hybrid"),
        )

    async def async_step_main_fuse(self, user_input: dict[str, Any] | None = None):
        """Main fuse protection: fuse size + meter/charger current sensors (v2.14.0)."""
        three_phase = is_three_phase(self.mode_info)
        errors: dict[str, str] = {}
        current = dict(self.config_entry.data)
        if user_input is not None:
            data = _normalize_main_fuse_input(user_input, three_phase)
            errors = _validate_main_fuse(data, three_phase)
            if not errors:
                self.fuse_info = data
                return await self._go_next("fuse")
            current = data

        return self.async_show_form(
            step_id="main_fuse",
            data_schema=_main_fuse_schema(self.hass, current, three_phase),
            errors=errors,
            description_placeholders=self._placeholders("fuse"),
        )

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

    async def async_step_dashboard(self, user_input: dict[str, Any] | None = None):
        """Final options step: auto-generated dashboard toggle."""
        if user_input is not None:
            updated_data = _apply_features(
                _merge_entry_data(
                    self.config_entry.data,
                    self.mode_info,
                    self.features_info,
                    self.charger_info,
                    self.sensor_info,
                    self.hybrid_info,
                    self.fuse_info,
                    self.pv_forecast_info,
                    self.notifications_info,
                    self.external_info,
                    user_input,
                )
            )
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
