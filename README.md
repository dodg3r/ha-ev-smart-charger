<div align="center">

# ⚡ EV Smart Charger

### Solar, price and fuse aware EV charging for Home Assistant

Charges the car on solar surplus during the day, in the cheapest hours at night,
and never above what the main fuse allows. Three-phase, any wallbox that Home
Assistant can control, several cars on one charger, and its own dashboard.

[![GitHub Release](https://img.shields.io/github/v/release/dodg3r/ha-ev-smart-charger?style=for-the-badge&logo=github&color=007AFF)](https://github.com/dodg3r/ha-ev-smart-charger/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-FF6F00?style=for-the-badge&logo=home-assistant&logoColor=white)](https://github.com/custom-components/hacs)
[![License: MIT](https://img.shields.io/badge/License-MIT-34C759?style=for-the-badge)](LICENSE)

<br/>

![EV Smart Charger Dashboard](docs/screenshots/dashboard.png)

</div>

> This integration started as a fork of
> [antbald/ha-ev-smart-charger](https://github.com/antbald/ha-ev-smart-charger)
> (MIT, © 2025 Antonio Baldassarre) and has since gone its own way: price-based
> night charging, main fuse protection, car profiles, three-phase only and a
> different setup. Settings and entity names are still compatible with the
> original, so an existing installation keeps working after an update.

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installation](#installation)
- [Setup](#setup)
- [How the charging works](#how-the-charging-works)
  - [Who decides](#who-decides)
  - [Solar Surplus](#solar-surplus)
  - [Night Smart Charge (cheapest hours)](#night-smart-charge-cheapest-hours)
  - [Boost Charge](#boost-charge)
  - [Main fuse protection (load balancing)](#main-fuse-protection-load-balancing)
  - [Car profiles](#car-profiles)
  - [Home battery](#home-battery)
  - [Stop Charging, Force Charge and Smart Charger Blocker](#stop-charging-force-charge-and-smart-charger-blocker)
- [Dashboard](#dashboard)
- [Created entities](#created-entities)
- [Notifications](#notifications)
- [Logging and troubleshooting](#logging-and-troubleshooting)
- [Privacy](#privacy)
- [License](#license)

---

## What it does

- ☀️ **Solar Surplus** — charges on the surplus from the solar panels and adjusts
  the current as the sun comes and goes. Waits for the charger's readings after
  every change, reacts within seconds when the house suddenly uses more, and
  never starts on grid power.
- 🌙 **Night Smart Charge** — reads the electricity prices (Nord Pool or
  compatible), works out how many hours the car needs to reach its target, and
  charges in the cheapest night hours before the time the car must be ready.
- ⚡ **Boost Charge** — the manual "charge now" button, with its own current and
  target. Stops at the target, when switched off, or when the car is unplugged.
- 🔌 **Main fuse protection** — keeps house load plus the car below the main fuse
  on every phase. *Display only* shows what should be done (for an external load
  balancer); *Display and adjust* lowers or pauses the charger itself.
- 🚗 **Car profiles** — several cars share the charger, each with its own SOC
  sensor, charge limit and battery size, plus a Guest profile without sensors.
  The car that is home and plugged in can be selected automatically.
- 🔋 **Home battery** — optional. Decide whether the battery or the car gets
  the sun first (up to a chosen SOC), let it help the car on solar, or only
  show it on the dashboard when it has its own control.
- 📅 **Weekly planner** — EV target, "car ready" and ready time per weekday.
- 📊 **Dashboard** — created automatically in the sidebar, follows the Home
  Assistant language (English, Swedish, Italian, Dutch), and each part can be
  used as its own card on other dashboards.

---

## Requirements

- Home Assistant 2024.4 or later
- A **three-phase** installation
- A charger that Home Assistant can start, stop and set the current on

The setup asks for the sensors part by part. Everything below is required unless
marked optional.

**Electricity meter** (always)

| What | Unit |
|---|---|
| Main fuse size | A |
| Net grid power — positive when buying, negative when selling | W / kW |
| Grid import power L1, L2, L3 | W |
| Grid export power L1, L2, L3 | W |
| Grid current L1, L2, L3 | A |
| Grid voltage L1, L2, L3 | V |
| Grid available (optional) | binary_sensor |

**Charger** (always)

| What | Notes |
|---|---|
| On/off | a `switch`, or a start and a stop `button` (e.g. Growatt THOR) |
| Current control | `number`, `input_number`, `select` or `input_select`, 6–32 A |
| Status | Tuya (`charger_charging` …) and OCPP (`Charging`, `SuspendedEV` …) states are understood |
| Charging power | one total sensor, or L1 with optional L2 / L3 |
| Charger current | one phase, or L1 with optional L2 / L3 |

**Solar panels** (only if you have them)

| What | Unit |
|---|---|
| Solar production — total or L1, optional L2 / L3 | W / kW |
| Solar forecast today and tomorrow | kWh |

**Home battery** (only if you have one)

| What | Unit |
|---|---|
| Battery SOC | % |
| Battery power — negative when discharging, positive when charging | W |

**Other**

| What | Notes |
|---|---|
| Electricity price | Nord Pool (core or HACS), or any sensor with `raw_today` / `raw_tomorrow` or `today` / `tomorrow` lists |
| Car owner | a `person`; notifications are only sent while this person is home |
| Mobile notify services | optional, `notify.mobile_app_*` |
| Per car | EV SOC (sensor, number or input_number), optional charge limit, battery capacity, optional location and connection sensors |

The house consumption is **computed**, not mapped:
`house = production + net grid − EV charging power − battery power`.

---

## Installation

**HACS**

1. HACS → ⋮ → **Custom repositories**
2. URL `https://github.com/dodg3r/ha-ev-smart-charger`, type **Integration**
3. Install **EV Smart Charger** and restart Home Assistant

**Manual**

Copy `custom_components/ev_smart_charger` from the
[latest release](https://github.com/dodg3r/ha-ev-smart-charger/releases) to
`/config/custom_components/ev_smart_charger/` and restart Home Assistant.

After every update: restart Home Assistant and reload the dashboard page
(Ctrl+Shift+R).

---

## Setup

**Settings → Devices & Services → Add Integration → EV Smart Charger.** Only the
steps that apply to your answers are shown.

| Step | What is asked |
|---|---|
| Name | display name |
| Charger model | **Tuya** (fixed levels 6/8/10/13/16/20/24/32 A, current lowered by stop → set → start) or **Generic** (any whole ampere, lowered live) |
| What do you have? | solar panels · home battery (and whether the integration may use it) · load balancing: *None*, *Display only*, *Display and adjust* |
| Electricity meter | see [Requirements](#requirements) |
| Charger | see [Requirements](#requirements) |
| Solar panels | production |
| Home battery | SOC and battery power |
| Solar forecast | today and tomorrow |
| Notifications | notify services and car owner |
| External | electricity price, optional helper that receives the night energy forecast |
| Car profiles | add, edit or remove cars |
| Dashboard | create the sidebar dashboard (default on) |

Everything can be changed later with **Reconfigure** or **Configure** on the
integration, without losing the settings on the dashboard. Answering "no" to
solar or home battery removes the entities that no longer apply.

---

## How the charging works

### Who decides

Several parts can want the charger at the same time. A coordinator lets the
highest one win:

| Order | Part | Active when |
|:---:|---|---|
| 0 | Stop Charging | switch on — stops and holds the charger |
| 1 | Force Charge | switch on — keeps the charger running |
| 2 | Boost Charge | switched on, or its schedule is running |
| 3 | Smart Charger Blocker | the charger starts in the blocked window |
| 4 | Night Smart Charge | the current hour is a planned hour |
| 5 | Solar Surplus | profile `solar_surplus`, daytime |

The main fuse protection is not in this list: with *Display and adjust* it caps
every command, whoever sent it.

### Solar Surplus

Runs every `evsc_check_interval` minute (default 1) between sunrise and sunset
when the charging profile is `solar_surplus`.

- **Surplus** = production − house consumption, converted to amperes at
  3 × 230 V. On three-phase 6 A is about 4.1 kW.
- **Start** at 6.5 A of surplus held for 60 s; **stop** below 5.5 A for
  `evsc_surplus_drop_delay` seconds. Between the two it keeps charging at 6 A.
  `evsc_solar_start_offset` (−4 … +4 A) moves both limits, so a negative value
  lets charging start earlier and accept a little grid import.
- **Up one level at a time** after 60 s of stable surplus; **down** after the
  drop delay.
- **Waits for the charger**: for 60 s after every change of the charger's status
  and after each of its own commands, it makes no new decision (OCPP readings
  arrive up to a minute late). After charging stops it waits 120 s before a new
  start, after a plug-in 30 s. When the charger reports `SuspendedEVSE` it holds.
- **Grid import protection**: import above `evsc_grid_import_threshold` for
  `evsc_grid_import_delay` seconds lowers the current, or stops it when even
  6 A is too much. A sudden house load (oven, washing machine) with steady solar
  is handled after `evsc_spike_response_delay` seconds (default 10) in one step
  instead of level by level.
- **Ceilings**: `evsc_solar_max_amperage` (default 16 A), and
  `evsc_offgrid_max_amperage` while the grid-available sensor says the grid is
  gone.
- **Home battery first** (`evsc_home_battery_first_soc`, see
  [Home battery](#home-battery)) decides whether the battery's charging counts
  as free sun for the car.
- A car at its charge limit is not started; an unavailable charger status skips
  the tick instead of being read as "plugged in".

### Night Smart Charge (cheapest hours)

Switch: `evsc_night_smart_charge_enabled`. Current: `evsc_night_charge_amperage`.

1. The **deadline** is the next day with *car ready* on, at that day's ready time
   (weekly planner). Days with car ready off are skipped: the car is charged on
   solar instead.
2. When the car is plugged in, the energy to the day's EV target is turned into
   a number of hours at the night current (capacity × missing % / 0.9).
3. The cheapest **night hours** (between sunset and sunrise, plus the morning of
   the deadline day) before the deadline are planned. The charger only runs in
   those hours.
4. The plan goes live when the prices reach the deadline. If they never do, it
   plans at sunset with what is known, so the car is never left empty.
5. Raising the target re-opens a finished plan; an unplug only stops the session
   after 60 s, so a status blink is not read as a disconnect.

Without a car ready day in the coming week the card shows an overview of the
known prices and the 5 cheapest hours. A car without SOC (e.g. Guest) charges in
the 5 cheapest hours.

### Boost Charge

The manual override. `evsc_boost_charge_enabled` starts charging at
`evsc_boost_charge_amperage` until `evsc_boost_target_soc`, overriding the night
plan, Solar Surplus and the blocker (not the fuse protection). It ends at the
target, when switched off, or when the car is unplugged. Without a readable SOC
it charges until unplugged or switched off.

A daily schedule is available with `evsc_boost_schedule_enabled` and the start
and end times.

### Main fuse protection (load balancing)

Chosen in "What do you have?":

- **None** — nothing is computed.
- **Display only** — the limit is computed and shown, for example "load
  balancing should lower to 10 A (L3)", but the charger is never changed. Use
  this when the charger or an external load balancer handles the fuse.
- **Display and adjust** — the integration keeps the charger below the limit.

Per phase, the weakest phase wins:

```
room = fuse − margin − (grid current − charger current)
```

- The direction of the grid current comes from the import / export power, so
  export gives extra room and reactive current is not counted as export.
- Lowered at once; raised in one step after the room has lasted 60 s.
- Below 6 A the charger is paused for 120 s and then restarted.
- A phase more than `evsc_fuse_hard_trip` (default 10 A) above the fuse stops
  the charger at once.
- Missing meter data drops to 6 A and stops after 2 minutes.

Entities (only with load balancing): `sensor.…_evsc_main_fuse`,
`sensor.…_evsc_fuse_headroom` (the limit, with per-phase details),
`number.…_evsc_fuse_margin` (default 2 A) and `number.…_evsc_fuse_hard_trip`.

### Car profiles

Several cars can share the charger. Each car has a name, its own SOC sensor, an
optional charge limit sensor (counts as full; every target is capped to it), the
battery capacity, and optionally a location (`device_tracker` / `person`) and a
connection sensor.

- A **Guest** profile always exists, without sensors.
- The active car is chosen in the dropdown at the top of the dashboard
  (`select.…_evsc_active_car`).
- **Automatic selection**: when exactly one car arrives home *and* reports that
  it is plugged in, it becomes the active car. A manual choice is kept until
  that happens.
- Daily targets and ready times are shared by all cars.

### Home battery

- **Home Battery First Up To** (`evsc_home_battery_first_soc`, default 0 %):
  below this SOC the battery gets the sun first. Its charging then counts as
  house load, so the car only gets what would otherwise be exported. At or
  above it, the battery's charging counts as free sun for the car, which
  takes it over. 0 % = car first, 100 % = battery first. It only changes how
  the surplus is counted, so it also works for a battery with its own control.
  Example: 4.6 kW sun, the battery charges 3.5 kW, the house uses 1.1 kW. Car
  first: 3.5 kW is available for the car. Battery first: nothing until the
  battery is full or leaves sun over.
- **Let EV Smart Charger use the battery** off: the battery is only shown on the
  dashboard, nothing waits for it or draws from it.
- **Use Home Battery** (`evsc_use_home_battery`): when solar is not enough, the
  car charges at `evsc_battery_support_amperage` from the battery while its SOC
  is above `evsc_home_battery_min_soc` and sunset is more than
  `evsc_battery_support_sunset_buffer` minutes away.
- **Priority Balancer** (`evsc_priority_balancer_enabled`): compares the car and
  the battery with their daily targets and decides who gets the solar first
  (car first / home battery first / both met).

### Stop Charging, Force Charge and Smart Charger Blocker

- **Stop Charging** (`evsc_stop_charging`) stops the charger at once and holds
  it stopped, also against a charger that restarts by itself. Off again: the
  automations take over, nothing is force-started.
- **Force Charge** (`evsc_forza_ricarica`) keeps the charger running. It is only
  shown on the dashboard while it is on; Boost is the normal override. Stop and
  Force can never be on at the same time.
- **Smart Charger Blocker** (`evsc_smart_charger_blocker_enabled`) stops a
  charge that starts between sunset and sunrise, unless the night plan, Boost or
  Force Charge owns it.

---

## Dashboard

With the dashboard step on, **EV Smart Charger** appears in the sidebar
(`/ev-smart-charger`). Two tabs:

- **Dashboard** (Översikt) — car dropdown, status ring with the target mark, a status line
  that says what is happening and what comes next ("Solar surplus 2.1 kW, too
  little to start (needs 4.5 kW)", "Cloud pause · stopping in 45 s",
  "Paused by main fuse protection · retrying in 1:30"), tiles for solar,
  grid, charger current, charger and home battery, Stop / Boost, weekly
  planner, night plan with price bars and the charging profile.
- **Settings** — every setting with explanation, grouped by function. Settings
  that do not apply to your setup are hidden.

**A part as its own card.** Open the dashboard → edit → raw configuration
editor, copy the card and add `sections`:

```yaml
type: custom:ev-smart-charger-dashboard
entity_prefix: ev_smart_charger_<entry_id_lowercased>
sections: [hero, night]
```

| Option | Values |
|---|---|
| `sections` | `hero`, `car`, `controls`, `weekly`, `night`, `profile`, `settings` |
| `theme` | `ha` follows the Home Assistant theme, `evsc` keeps the card's own look (default `ha` with `sections`) |

Without the auto-dashboard, add the resource
`/api/ev_smart_charger/frontend/ev-smart-charger-dashboard.js?v=<version>` as a
JavaScript module and change `<version>` after every update.

---

## Created entities

All entities belong to one **EV Smart Charger** device and keep their values
over restarts. Entity ids look like
`<platform>.ev_smart_charger_<entry_id>_<suffix>`.

| Group | Suffixes |
|---|---|
| Control | `evsc_charging_profile` (manual / solar_surplus), `evsc_active_car`, `evsc_stop_charging`, `evsc_forza_ricarica`, `evsc_force_charge_auto_disarm`, `evsc_smart_charger_blocker_enabled` |
| Solar Surplus | `evsc_check_interval`, `evsc_grid_import_threshold`, `evsc_grid_import_delay`, `evsc_surplus_drop_delay`, `evsc_spike_response_delay`, `evsc_solar_start_offset`, `evsc_solar_max_amperage`, `evsc_offgrid_max_amperage`, `evsc_nighttime_sunset_offset`, `evsc_nighttime_sunrise_offset` |
| Night | `evsc_night_smart_charge_enabled`, `evsc_night_charge_amperage`, `evsc_car_ready_<day>`, `evsc_car_ready_time_<day>`, `evsc_ev_min_soc_<day>` |
| Boost | `evsc_boost_charge_enabled`, `evsc_boost_charge_amperage`, `evsc_boost_target_soc`, `evsc_boost_schedule_enabled`, `evsc_boost_schedule_start_time`, `evsc_boost_schedule_end_time` |
| Home battery | `evsc_home_battery_first_soc` (also display-only), `evsc_use_home_battery`, `evsc_priority_balancer_enabled`, `evsc_home_battery_min_soc`, `evsc_battery_support_amperage`, `evsc_battery_support_sunset_buffer`, `evsc_max_battery_discharge_for_ev`, `evsc_home_min_soc_<day>` |
| Load balancing | `evsc_main_fuse`, `evsc_fuse_headroom`, `evsc_fuse_margin`, `evsc_fuse_hard_trip` |
| Notifications | `evsc_notify_smart_blocker_enabled`, `evsc_notify_priority_balancer_enabled`, `evsc_notify_night_charge_enabled`, `evsc_live_activities_enabled` |
| Status | `evsc_diagnostic`, `evsc_solar_surplus_diagnostic`, `evsc_priority_daily_state`, `evsc_night_charge_plan`, `evsc_night_session_state`, `evsc_today_ev_target`, `evsc_today_home_target`, `evsc_cached_ev_soc` |
| Logging | `evsc_enable_file_logging`, `evsc_trace_logging_enabled`, `evsc_log_file_path` |

Home battery entities only exist with a battery the integration may use, load
balancing entities only with load balancing. A few settings from the original
start-time based night charge (`evsc_night_charge_time`, `evsc_car_ready_time`,
`evsc_min_solar_forecast_threshold`, `evsc_night_pv_handoff_threshold`,
`evsc_preserve_home_battery`) still exist for compatibility but are not used by
the price plan.

---

## Notifications

Push notifications go to the mapped `notify.mobile_app_*` services and only
while the car owner is home. Each kind can be turned off with its switch.

**Live Activities / Live Updates** (`evsc_live_activities_enabled`, default on):
one live notification on the iPhone Lock Screen / Dynamic Island or the Android
notification shade for the whole session, updated when the mode, the target or
the SOC (every 5 %) changes. Needs HA 2026.7+, iOS 17.2+ (Android 16+) and Live
Activities allowed for the Companion app.

---

## Logging and troubleshooting

- **Status first**: the dashboard status line, and the attributes of
  `sensor.…_evsc_solar_surplus_diagnostic` and `sensor.…_evsc_night_charge_plan`
  in Developer Tools → States.
- **File log**: turn on `evsc_enable_file_logging`. One file per day under
  `/config/custom_components/ev_smart_charger/logs/<year>/<month>/<day>.log`;
  the path is in `sensor.…_evsc_log_file_path`.
- **Trace logging** (`evsc_trace_logging_enabled`) writes every reading and
  calculation; use it briefly.
- **Home Assistant log**: normal decisions are INFO, only things you need to fix
  are WARNING. For more detail:

```yaml
logger:
  logs:
    custom_components.ev_smart_charger: debug
```

| Symptom | Check |
|---|---|
| Solar Surplus does not start | profile `solar_surplus`? surplus above the start limit (status line shows how much is needed)? car at its charge limit? |
| Night charge does not run | `evsc_night_smart_charge_enabled` on, a car ready day ahead, prices available (see the night card) |
| Charger state wrong with buttons | map the charging power and status sensors; after 60 s from a press the state comes from them |
| "Charger not responding" | the charger status sensor is unavailable |
| Charging lowered or paused for the fuse | `sensor.…_evsc_fuse_headroom` shows the phase and the room |

---

## Privacy

The integration sends nothing outside your Home Assistant. (The anonymous usage
ping of the original was removed in v2.41.1.)

---

## License

MIT, see [LICENSE](LICENSE). Original work © 2025 Antonio Baldassarre.

Technical notes and the full change log are in [CLAUDE.md](CLAUDE.md).
