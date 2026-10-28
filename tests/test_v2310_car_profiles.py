"""v2.31.0: car profiles — several cars, the active one picked on the dashboard.

v2.32.0: the guest profile always exists before the own cars."""
from types import SimpleNamespace
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_smart_charger.config_flow import EVSCOptionsFlow
from custom_components.ev_smart_charger.const import (
    CONF_ACTIVE_CAR,
    CONF_BATTERY_CAPACITY,
    CONF_CAR_NAME,
    CONF_CARS,
    CONF_EV_CHARGER_SWITCH,
    CONF_EV_SOC_LIMIT,
    CONF_SOC_CAR,
    DEFAULT_CAR_NAME,
    DOMAIN,
    GUEST_CAR,
    apply_active_car,
    get_active_car_name,
    get_cars,
    get_own_cars,
)
from custom_components.ev_smart_charger.power_model import ChargingModel
from custom_components.ev_smart_charger.select import EVSCCarSelect

LEGACY = {
    CONF_SOC_CAR: "sensor.ex40_soc",
    CONF_EV_SOC_LIMIT: "sensor.ex40_limit",
    CONF_BATTERY_CAPACITY: 78.0,
}
TWO_CARS = {
    **LEGACY,
    CONF_CARS: [
        {CONF_CAR_NAME: "ABC123", **LEGACY},
        {
            CONF_CAR_NAME: "XYZ789",
            CONF_SOC_CAR: "sensor.other_soc",
            CONF_EV_SOC_LIMIT: None,
            CONF_BATTERY_CAPACITY: 40.0,
        },
    ],
    CONF_ACTIVE_CAR: "ABC123",
}


def test_legacy_entry_is_one_car():
    cars = get_own_cars(LEGACY)
    assert len(cars) == 1
    assert cars[0][CONF_CAR_NAME] == DEFAULT_CAR_NAME
    assert cars[0][CONF_SOC_CAR] == "sensor.ex40_soc"
    assert get_active_car_name(LEGACY) == DEFAULT_CAR_NAME
    # the guest profile comes first, without sensors
    assert [c[CONF_CAR_NAME] for c in get_cars(LEGACY)] == [GUEST_CAR, DEFAULT_CAR_NAME]
    assert get_cars(LEGACY)[0][CONF_SOC_CAR] is None


def test_apply_active_car_copies_its_sensors():
    data = apply_active_car(TWO_CARS, "XYZ789")
    assert data[CONF_ACTIVE_CAR] == "XYZ789"
    assert data[CONF_SOC_CAR] == "sensor.other_soc"
    assert data[CONF_EV_SOC_LIMIT] is None
    assert data[CONF_BATTERY_CAPACITY] == 40.0
    # unknown name falls back to the first car
    assert apply_active_car(TWO_CARS, "nope")[CONF_ACTIVE_CAR] == "ABC123"


def test_ev_paused_status_is_not_drawing(hass):
    model = ChargingModel.from_config({"ev_charger_status": "sensor.status"})
    hass.states.async_set("sensor.status", "suspended_ev")
    assert model.is_charging(hass) is False
    hass.states.async_set("sensor.status", "charging")
    assert model.is_charging(hass) is True


async def test_car_select_switches_the_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=dict(TWO_CARS))
    entry.add_to_hass(hass)
    entity = EVSCCarSelect(hass, entry, None)
    assert entity.options == [GUEST_CAR, "ABC123", "XYZ789"]
    assert entity.current_option == "ABC123"
    attrs = entity.extra_state_attributes["cars"]
    assert attrs[0]["ev_soc_entity"] is None
    assert attrs[2]["ev_soc_entity"] == "sensor.other_soc"

    with patch.object(hass.config_entries, "async_update_entry") as update:
        await entity.async_select_option("XYZ789")
    data = update.call_args.kwargs["data"]
    assert data[CONF_ACTIVE_CAR] == "XYZ789"
    assert data[CONF_SOC_CAR] == "sensor.other_soc"

    with patch.object(hass.config_entries, "async_update_entry") as update:
        await entity.async_select_option("ABC123")  # already active
        await entity.async_select_option("unknown")
    update.assert_not_called()

    with patch.object(hass.config_entries, "async_update_entry") as update:
        await entity.async_select_option(GUEST_CAR)
    data = update.call_args.kwargs["data"]
    assert data[CONF_ACTIVE_CAR] == GUEST_CAR
    assert data[CONF_SOC_CAR] is None
    assert data[CONF_EV_SOC_LIMIT] is None
    assert len(data[CONF_CARS]) == 2  # own cars kept


async def _options_flow_to_cars(hass, data):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="switch.charger",
        data={CONF_EV_CHARGER_SWITCH: "switch.charger", **data},
    )
    entry.add_to_hass(hass)
    flow = EVSCOptionsFlow(entry)
    flow.hass = hass
    return flow


async def test_options_flow_adds_edits_and_removes_cars(hass):
    flow = await _options_flow_to_cars(hass, LEGACY)

    result = await flow.async_step_cars()
    assert result["step_id"] == "cars"
    assert DEFAULT_CAR_NAME in result["description_placeholders"]["cars"]

    # rename the existing (active) car
    result = await flow.async_step_cars({"car_action": "edit:0"})
    assert result["step_id"] == "car"
    result = await flow.async_step_car(
        {CONF_CAR_NAME: "ABC123", CONF_SOC_CAR: "sensor.ex40_soc", CONF_BATTERY_CAPACITY: 78.0}
    )
    assert result["step_id"] == "cars"

    # a second car; a duplicate name is refused
    await flow.async_step_cars({"car_action": "add"})
    result = await flow.async_step_car(
        {CONF_CAR_NAME: "ABC123", CONF_SOC_CAR: "sensor.x", CONF_BATTERY_CAPACITY: 40.0}
    )
    assert result["errors"] == {CONF_CAR_NAME: "car_name_exists"}
    result = await flow.async_step_car(
        {CONF_CAR_NAME: "XYZ789", CONF_SOC_CAR: "sensor.other_soc", CONF_BATTERY_CAPACITY: 40.0}
    )
    assert result["step_id"] == "cars"

    # remove the active car: the other one becomes active
    await flow.async_step_cars({"car_action": "remove:0"})
    result = await flow.async_step_cars({"car_action": "done"})
    assert result["step_id"] == "dashboard"

    with patch.object(hass.config_entries, "async_update_entry") as update:
        await flow.async_step_dashboard({"create_dashboard": False})
    data = update.call_args.kwargs["data"]
    assert [c[CONF_CAR_NAME] for c in data[CONF_CARS]] == ["XYZ789"]
    assert data[CONF_ACTIVE_CAR] == "XYZ789"
    assert data[CONF_SOC_CAR] == "sensor.other_soc"
    assert data[CONF_EV_SOC_LIMIT] is None
    assert data[CONF_BATTERY_CAPACITY] == 40.0


async def test_guest_is_listed_and_its_name_is_reserved(hass):
    flow = await _options_flow_to_cars(hass, TWO_CARS)
    result = await flow.async_step_cars()
    assert result["description_placeholders"]["cars"].startswith("• Guest")
    await flow.async_step_cars({"car_action": "add"})
    result = await flow.async_step_car(
        {CONF_CAR_NAME: "Gäst", CONF_SOC_CAR: "sensor.x", CONF_BATTERY_CAPACITY: 40.0}
    )
    assert result["errors"] == {CONF_CAR_NAME: "car_name_reserved"}


async def test_removing_every_car_leaves_the_guest(hass):
    flow = await _options_flow_to_cars(hass, TWO_CARS)
    await flow.async_step_cars()
    await flow.async_step_cars({"car_action": "remove:1"})
    await flow.async_step_cars({"car_action": "remove:0"})
    data = flow._with_cars(dict(TWO_CARS))
    assert data[CONF_CARS] == []
    assert data[CONF_ACTIVE_CAR] == GUEST_CAR
    assert data[CONF_SOC_CAR] is None
    # an empty list is not read as a legacy car again
    assert [c[CONF_CAR_NAME] for c in get_cars(data)] == [GUEST_CAR]


async def test_guest_choice_stays(hass):
    """The user picked the guest: reconfiguring does not change that."""
    flow = await _options_flow_to_cars(hass, {**TWO_CARS, CONF_ACTIVE_CAR: GUEST_CAR})
    await flow.async_step_cars()
    await flow.async_step_cars({"car_action": "add"})
    await flow.async_step_car(
        {CONF_CAR_NAME: "NEW1", CONF_SOC_CAR: "sensor.new", CONF_BATTERY_CAPACITY: 60.0}
    )
    data = flow._with_cars(dict(TWO_CARS))
    assert data[CONF_ACTIVE_CAR] == GUEST_CAR
    assert len(data[CONF_CARS]) == 3


async def _initial_flow_to_cars(hass):
    from homeassistant import config_entries

    from .test_v2170_features import ENTITIES, _to_features

    result = await _to_features(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"has_solar": False, "has_home_battery": False, "load_balancing": "charger"},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTITIES)
    assert result["step_id"] == "sensors"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"grid_import": "sensor.grid"}
    )
    hass.services.async_register("notify", "mobile_app_phone", lambda call: None)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"notify_services": [], "car_owner": "person.test"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "cars"
    assert config_entries  # imported for the flow manager
    return result


async def test_initial_flow_without_car_is_guest_only(hass):
    result = await _initial_flow_to_cars(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"car_action": "done"}
    )
    assert result["step_id"] == "dashboard"
    with patch("custom_components.ev_smart_charger.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"create_dashboard": False}
        )
    data = result["data"]
    assert data[CONF_CARS] == []
    assert data[CONF_ACTIVE_CAR] == GUEST_CAR
    assert data[CONF_SOC_CAR] is None


async def test_initial_flow_with_car_makes_it_active(hass):
    result = await _initial_flow_to_cars(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"car_action": "add"}
    )
    assert result["step_id"] == "car"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CAR_NAME: "ABC123",
            CONF_SOC_CAR: "sensor.ex40_soc",
            CONF_EV_SOC_LIMIT: "sensor.ex40_limit",
            CONF_BATTERY_CAPACITY: 78.0,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"car_action": "done"}
    )
    with patch("custom_components.ev_smart_charger.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"create_dashboard": False}
        )
    data = result["data"]
    assert [c[CONF_CAR_NAME] for c in get_cars(data)] == [GUEST_CAR, "ABC123"]
    assert data[CONF_ACTIVE_CAR] == "ABC123"
    assert data[CONF_SOC_CAR] == "sensor.ex40_soc"
    assert data[CONF_EV_SOC_LIMIT] == "sensor.ex40_limit"
    assert data[CONF_BATTERY_CAPACITY] == 78.0


def test_select_platform_creates_the_car_select(hass):
    entity = EVSCCarSelect(hass, SimpleNamespace(entry_id="e", data=LEGACY), None)
    assert entity.options == [GUEST_CAR, DEFAULT_CAR_NAME]
    assert entity.entity_id == "select.ev_smart_charger_e_evsc_active_car"
