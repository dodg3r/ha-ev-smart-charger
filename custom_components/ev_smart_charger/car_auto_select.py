"""Automatic car selection (v2.37.0).

Each own car profile may map a location entity (device_tracker / person) and a
connection sensor. A car "matches" when every mapped condition holds: the
location reads ``home`` and the connection reads ``connected`` (or ``on``).

When a car goes from not matching to matching, it becomes the active car, the
same way as choosing it in ``select.evsc_active_car`` (the entry is updated and
reloads). Only that edge acts:

* at setup the current match state is only recorded, so a manual choice made
  while another car is home and connected is not undone by the reload;
* nothing changes when no car matches (the last choice stays);
* nothing changes when the matching car already is the active one.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CAR_CONNECTED_STATES,
    CAR_LOCATION_HOME_STATES,
    CONF_CAR_CONNECTION,
    CONF_CAR_LOCATION,
    CONF_CAR_NAME,
    apply_active_car,
    car_has_auto_select,
    get_active_car_name,
    get_own_cars,
)
from .utils.logging_helper import EVSCLogger


def _norm(state) -> str:
    return str(state.state if state is not None else "").strip().casefold().replace(" ", "_")


class CarAutoSelect:
    """Switch the active car when a car arrives home and is plugged in."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.logger = EVSCLogger("CAR SELECT")
        self._unsub = None
        self._matched: dict[str, bool] = {}

    def _cars(self) -> list[dict]:
        return [car for car in get_own_cars(self.entry.data) if car_has_auto_select(car)]

    def car_matches(self, car: dict) -> bool:
        location = car.get(CONF_CAR_LOCATION)
        connection = car.get(CONF_CAR_CONNECTION)
        if not location and not connection:
            return False
        if location and _norm(self.hass.states.get(location)) not in CAR_LOCATION_HOME_STATES:
            return False
        if connection and _norm(self.hass.states.get(connection)) not in CAR_CONNECTED_STATES:
            return False
        return True

    async def async_setup(self) -> None:
        cars = self._cars()
        if not cars:
            self.logger.debug("No car maps a location / connection entity")
            return
        entities = sorted(
            {
                entity
                for car in cars
                for entity in (car.get(CONF_CAR_LOCATION), car.get(CONF_CAR_CONNECTION))
                if entity
            }
        )
        self._matched = {car[CONF_CAR_NAME]: self.car_matches(car) for car in cars}
        self._unsub = async_track_state_change_event(self.hass, entities, self._on_change)
        self.logger.info(
            f"Automatic car selection for {', '.join(car[CONF_CAR_NAME] for car in cars)}"
        )

    @callback
    def _on_change(self, _event) -> None:
        self.evaluate()

    @callback
    def evaluate(self) -> str | None:
        """Return the car switched to, if any."""
        arrived: list[str] = []
        for car in self._cars():
            name = car[CONF_CAR_NAME]
            now = self.car_matches(car)
            if now and not self._matched.get(name, False):
                arrived.append(name)
            self._matched[name] = now
        if len(arrived) != 1:
            if len(arrived) > 1:
                self.logger.info(
                    f"Several cars matched at once ({', '.join(arrived)}) - keeping the choice"
                )
            return None
        name = arrived[0]
        if name == get_active_car_name(self.entry.data):
            return None
        self.logger.info(f"{name} is home and connected - making it the active car")
        self.hass.config_entries.async_update_entry(
            self.entry, data=apply_active_car(self.entry.data, name)
        )
        return name

    async def async_remove(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
