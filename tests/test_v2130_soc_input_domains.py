"""Tests for v2.13.0 — EVs that expose no SOC readout (issue #58)."""


def test_soc_fields_accept_plain_helpers():
    """v2.13.0 (issue #58): EVs with no SOC readout can map an input_number.

    The field used to be a sensor-only selector, so users tracking SOC by hand
    had to wrap an `input_number` in a template sensor just to get past the
    config flow. Every reader goes through `state_helper`, which is
    domain-agnostic, so widening the selector is purely a UI affordance.
    """
    from custom_components.ev_smart_charger.const import SOC_INPUT_DOMAINS

    from custom_components.ev_smart_charger.config_flow import _sensor_schema

    assert "sensor" in SOC_INPUT_DOMAINS  # never drop the normal case
    assert "input_number" in SOC_INPUT_DOMAINS
    assert "number" in SOC_INPUT_DOMAINS

    # And the config flow really offers them (a constant nobody wires up is a
    # regression waiting to happen). v2.32.0: the car SOC lives in the car
    # profile form, the home battery SOC in the sensors step.
    from custom_components.ev_smart_charger.config_flow import _car_schema

    def selectors(schema, keys):
        return {
            str(key): value.config["domain"]
            for key, value in schema.schema.items()
            if str(key) in keys
        }

    assert selectors(_sensor_schema(), ("soc_car", "soc_home")) == {
        "soc_home": SOC_INPUT_DOMAINS,
    }
    assert selectors(_car_schema(), ("soc_car",)) == {"soc_car": SOC_INPUT_DOMAINS}
