<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/peatus/brand/dark_logo.png">
  <img alt="Peatus.ee Public Transport for Home Assistant" src="custom_components/peatus/brand/logo.png" width="420">
</picture>

# Peatus.ee Public Transport for Home Assistant

[![hacs][hacs-badge]][hacs]
[![Validate](https://github.com/r0mi/ha-peatus/actions/workflows/validate.yml/badge.svg)](https://github.com/r0mi/ha-peatus/actions/workflows/validate.yml)

Home Assistant integration that shows the next **10 departures** from any Estonian
public transport stop, using the same GraphQL API that powers
[web.peatus.ee](https://web.peatus.ee/).

Covers the whole national feed — Tallinn city buses, trolleybuses and trams,
Tartu and Pärnu city lines, county buses, Elron trains and ferries.

## Coverage

The API serves a single national feed (`feedId: estonia`) — 62 operators,
2360 routes and 18 313 stops across 34 fare zones, from Harju and Viru to
Tartu, Rapla and Pärnu. Every mode in the feed is supported:

| Mode | Routes | Operators |
| --- | --- | --- |
| Bus | 2310 | ~60, from Tallinn city lines to county and long-distance coaches |
| Train | 28 | Elron |
| Ferry | 11 | TS Laevad, Kihnu Veeteed, Saaremaa vald, Piiroja Invest |
| Tram | 6 | Tallinna Linnatransport |
| Trolleybus | 5 | Tallinna Linnatransport |

Only GTFS `route_type` 0, 2, 3 and 4 appear in the feed, so this list is
complete — there is nothing published that the integration cannot show. A
departure board works just as well for a ferry harbour (`Virtsu sadam`), a
railway station (`Tartu`) or a village bus stop as for a Tallinn tram platform.

The feed also carries the international coach stops that Estonian long-distance
routes call at — Riga, Vilnius, Kaunas, Warsaw, Prague — so those can be
configured too.

## Features

- **11 sensors per stop** — `Next departure` plus `Departure 1` … `Departure 10`.
- **Timestamp states**, so Home Assistant renders a live countdown without the
  integration rewriting sensor states on every poll.
- **Optional destination filter** — only show departures that actually continue
  to the stop you care about, instead of every service leaving the platform.
- **Transport mode filter** — bus, trolleybus, tram, train and ferry, including
  trolleybuses that the data source does not label as such (see below).
- **Configurable update interval**, from 1 minute upwards (default 3).
- Set up entirely from the UI, with stop search or a direct GTFS ID, in English
  and Estonian.

## Installation

### HACS (recommended)

1. In HACS, choose **Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/r0mi/ha-peatus` with category **Integration**.
3. Install **Peatus.ee Public Transport** and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   *Peatus.ee*.

### Manual

Copy `custom_components/peatus` into your Home Assistant `config/custom_components`
directory and restart.

## Configuration

The config flow walks through four short steps:

1. **How to find the stop** — search by name, or paste a GTFS ID such as
   `estonia:1292`.
2. **Pick the stop.** Stop names repeat all over Estonia, so each candidate is
   labelled with its code, direction, fare zone and the lines it serves, for
   example:

   ```
   Viru (12101-3) · Bus · Harju1 · 107, 108, 109, 111, 111A, 116, …
   Viru (7801121-1) · Kavastu suunas · Bus · 742, 764, 809, 845
   ```

   Note that a stop is a single platform in one direction — pick the side of the
   road you actually wait on.
3. **Destination (optional).** Leave it empty for all departures. Enter a stop
   name to keep only the departures whose route continues to that stop *after*
   your departure stop.
4. **Modes and update interval.** The mode of the stop you picked is preselected.

Modes and the update interval can be changed later via the integration's
**Configure** button. Changing the stop or destination means adding a new entry.

## Entities

Each configured stop becomes a device with 11 sensors:

| Entity | Description |
| --- | --- |
| `sensor.peatus_<stop>_next_departure` | The soonest matching departure |
| `sensor.peatus_<stop>_departure_1` … `_10` | The 1st to 10th matching departure |

Friendly names follow the same shape, built by Home Assistant from the device
name plus the entity name:

| | Friendly name | Entity ID |
| --- | --- | --- |
| No destination | `Laagri Departure 1` | `sensor.peatus_laagri_departure_1` |
| With destination | `Viru → Vana-Lõuna Departure 1` | `sensor.peatus_viru_vana_louna_departure_1` |

The stop code is kept out of both. It appears instead on the device card,
along with everything else needed to identify the stop:

| Device field | Example |
| --- | --- |
| Name | `Laagri` |
| Model | `Train stop Laagri`, or `Train route Laagri → Järve` when a destination is set |
| Model ID | `Rong Balti jaama suunas` — the feed's own description of the stop |
| Manufacturer | `peatus.ee` |

**Visit device** links straight to that stop's page on peatus.ee.

The config entry title carries the stop code at both ends, e.g.
`Laagri (04409-1) → Järve (06805-1)`. Stop names repeat across Estonia and a
single name can cover several platforms — there are two rail stops called
Järve, one towards Paldiski and one towards Tallinn — so the code is what
tells two boards apart in the integration list.

When a destination is configured it is included, so entity IDs stay distinct
between boards for the same stop:

```
sensor.peatus_viru_departure_1              # all departures from Viru
sensor.peatus_viru_vana_louna_departure_1   # Viru, only towards Vana-Lõuna
```

The stop code is deliberately left out to keep IDs readable. If you add two
boards for stops that share a name, Home Assistant appends `_2` to the second
one — rename it under the entity's settings if you want something clearer.
Renames you make are preserved across restarts and upgrades.

`Next departure` and `Departure 1` intentionally hold the same value —
`Next departure` gives automations a stable entity ID to reference.

The state is a **timestamp**. Sensors report `unknown` when there are fewer
matching departures than 10, which is normal at night.

### Icons

`Next departure` uses `mdi:clock-out`. Each numbered sensor is iconed by the
vehicle actually running that departure, so a stop served by several modes shows
them at a glance:

| Mode | Icon |
| --- | --- |
| Bus | `mdi:bus` |
| Trolleybus | `mdi:bus` — Material Design Icons has no trolleybus glyph |
| Tram | `mdi:tram` |
| Train | `mdi:train` |
| Ferry | `mdi:ferry` |

A numbered sensor with no departure due falls back to `mdi:timetable`, since no
vehicle mode is known at that point. All of these can be overridden per entity
in the UI.

### Attributes

| Attribute | Example | Notes |
| --- | --- | --- |
| `route` | `T3` | Line number / short name |
| `route_long_name` | `Tondi - Kadriorg` | |
| `mode` | `tram` | `bus`, `trolleybus`, `tram`, `rail` or `ferry` |
| `headsign` | `Tondi` | Where the vehicle is heading |
| `scheduled_departure` | `2026-09-16T10:21:00+00:00` | Timetable time |
| `realtime` | `false` | Whether the state came from a live vehicle feed |
| `realtime_state` | `SCHEDULED` | `SCHEDULED`, `UPDATED`, `ADDED`, … |
| `delay_seconds` | `0` | Positive when running late |
| `minutes_until` | `6` | Snapshot at poll time — see below |
| `trip_id` | `estonia:14047` | Identifies the scheduled trip; changes if the service is replaced |
| `stop`, `stop_code`, `stop_id` | `Viru`, `12102-1`, `estonia:1292` | |
| `destination` | `Vana-Lõuna` | `null` when no destination filter is set |

> **`minutes_until` is only refreshed when the sensor polls.** For a live
> countdown, use the state instead — Home Assistant's UI already renders a
> timestamp as relative time, and templates can use
> `as_timestamp(states('sensor.x')) - as_timestamp(now())`.

### About trolleybuses

Tallinn reintroduced trolleybus service with hybrid vehicles in 2026, but the
peatus.ee feed does not distinguish them: there is **no GTFS `route_type` 11 or
800 anywhere** in the 2360 published routes, and every trolleybus route is
`route_type: 3` / `mode: BUS`. The only marker is the Estonian word
`(trollibuss)` in the route's long name:

```
72  Mustamäe - Kopli (trollibuss)
81  Mustamäe - Kaubamaja (trollibuss)
83  Mustamäe - Kaubamaja (trollibuss)
84  Keskuse - Balti jaam (trollibuss)
85  Mustamäe - Balti jaam (trollibuss)
```

This integration therefore *derives* the trolleybus mode from that suffix, so
you can filter trolleybuses in or out independently of buses, and the `mode`
attribute reports `trolleybus`. Because the distinction rests on a naming
convention rather than structured data, it will stop working if the operator
changes how routes are named — buses would then simply show up as `bus`.

When you pick a stop the feed calls a bus stop, **both Bus and Trolleybus are
preselected**, so trolleybus departures are never hidden by accident.

### About realtime data

The integration reads the API's realtime departure fields and falls back to the
scheduled time when they are absent. At the time of writing the peatus.ee feed
returns `realtimeState: SCHEDULED` for every departure across buses, trams and
trains — that is, **timetable data only**. If and when the upstream feed starts
publishing vehicle positions, these sensors will pick it up with no changes, and
the `realtime` attribute tells you which is in play.

## Keeping history out of your database

Home Assistant has no per-entity history retention — `recorder`'s
`purge_keep_days` is global. Because these sensors are timestamps rather than
countdowns they write very few states, but a departure board is not something
you normally want months of history for. To keep it out of the recorder
entirely, add this to `configuration.yaml`:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.peatus_*
```

Or, to keep exactly 24 hours of history instead of none, add an automation:

```yaml
automation:
  - alias: Purge departure board history
    triggers:
      - trigger: time
        at: "04:00:00"
    actions:
      - action: recorder.purge_entities
        data:
          keep_days: 1
          entity_id: >
            {{ integration_entities('peatus') | join(', ') }}
```

## Example dashboard card

```yaml
type: entities
title: Viru → Vana-Lõuna
entities:
  - entity: sensor.peatus_viru_departure_1
    type: custom:template-entity-row
    name: >
      {{ state_attr(config.entity, 'route') }}
      → {{ state_attr(config.entity, 'headsign') }}
    state: "{{ relative_time(states(config.entity) | as_datetime) }}"
```

A plain `entities` card works too — Home Assistant renders timestamp sensors as
"in 6 minutes" automatically.

## Example automation

```yaml
automation:
  - alias: Leave for the tram
    triggers:
      - trigger: template
        value_template: >
          {{ (as_timestamp(states('sensor.peatus_viru_next_departure'), 0)
              - as_timestamp(now())) | int < 300 }}
    actions:
      - action: notify.mobile_app
        data:
          message: >
            {{ state_attr('sensor.peatus_viru_next_departure', 'route') }}
            leaves in 5 minutes.
```

## How it works

The integration queries the OpenTripPlanner GraphQL endpoint at
`https://api.peatus.ee/routing/v1/routers/estonia/index/graphql`. No API key is
required.

Destination filtering is done by **route pattern**, not by walking every trip's
stop list on each poll: the set of patterns that reach your destination after
your origin is resolved once and cached for 6 hours, and each poll only asks for
each departure's pattern code. Without filters a poll transfers roughly 2 kB.
When a filter is active the integration requests a larger batch, and grows it
only if too many departures were discarded.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install pytest-homeassistant-custom-component ruff
pytest tests -q
ruff check custom_components tests
```

## Brand images

The icon and logo ship inside the integration, in `custom_components/peatus/brand/`:

| | Standard | hDPI |
| --- | --- | --- |
| Icon | `icon.png` 256×256 | `icon@2x.png` 512×512 |
| Icon (dark) | `dark_icon.png` 256×256 | `dark_icon@2x.png` 512×512 |
| Logo | `logo.png` 914×256 | `logo@2x.png` 1828×512 |
| Logo (dark) | `dark_logo.png` 914×256 | `dark_logo@2x.png` 1828×512 |

Since **Home Assistant 2026.3** these are served by the local [Brands Proxy
API][brands-proxy] from `/api/brands/integration/peatus/...`, cached on disk
with stale-while-revalidate so they survive internet outages. Local images take
priority over the brands CDN, and no `manifest.json` entry or pull request
against [home-assistant/brands][brands] is needed — that repository's
`custom_integrations/` folder is legacy.

On Home Assistant older than 2026.3 the integration works normally, but falls
back to the default placeholder icon.

## Credits

Timetable data comes from the Estonian national public transport registry via
[peatus.ee](https://web.peatus.ee/). This project is not affiliated with or
endorsed by peatus.ee or the Transport Administration.

[brands]: https://github.com/home-assistant/brands
[brands-proxy]: https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/
[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
