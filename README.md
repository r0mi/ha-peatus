<img alt="Peatus.ee Public Transport for Home Assistant" src="https://raw.githubusercontent.com/r0mi/ha-peatus/main/custom_components/peatus/brand/dark_logo.png" width="420">

# Peatus.ee public transport for Home Assistant

[![hacs][hacs-badge]][hacs]
[![Release][release-badge]][releases]
[![License][license-badge]][license]
[![Validate](https://github.com/r0mi/ha-peatus/actions/workflows/validate.yml/badge.svg)](https://github.com/r0mi/ha-peatus/actions/workflows/validate.yml)

Home Assistant integration that shows the next **10 departures** from any Estonian
public transport stop, using the same GraphQL API that powers
[web.peatus.ee](https://web.peatus.ee/).

Covers the whole national feed — Tallinn city buses, trolleybuses and trams,
Tartu and Pärnu city lines, county buses, Elron trains and ferries.

## Features

Two kinds of boards:

- A **stop board** displays a single stop and lists its upcoming departures.
- A **journey board** plans a door-to-door trip between two places you track
  in Home Assistant, including walking legs.

Each board is one config entry, and appears in Home Assistant as one **device**
grouping that board's sensors.

### Stop boards

- **11 sensors per stop** — `Next departure` plus `Departure 1` … `Departure 10`.
- **Timestamp states**, so Home Assistant renders a live countdown without the
  integration rewriting sensor states on every poll.
- **Optional destination filter** — only show departures that actually continue
  to the stop you care about, instead of every service leaving the platform.
- **Transport mode filter** — bus, trolleybus, tram, train and ferry.
- **Optional line filter** — watch only the routes you travel with.
- **Configurable update interval**, from 1 minute upwards (default 3).
- Set up entirely from the UI, with stop search or a direct GTFS ID.

### Journey boards

- **7 sensors per journey** — `Journey 1` … `Journey 5`, plus `Walking` and
  `Cycling`.
- **Door to door.** Both ends are places — a zone, a person or a device tracker
  — rather than stops, so a plan includes the walk to the first stop and from
  the last one.
- **Full leg breakdown** in the attributes: every walk, ride and wait with its
  own duration, line, headsign and the route's own colours. The legs of a
  journey add up to its total exactly, so a dashboard can draw a proportional
  bar straight from them.
- **Follows you.** The origin is resolved on every poll, so a board planned
  from `person.you` re-plans as you move.
- **Always offers walking and cycling**, so you can see when the bus is not
  worth waiting for.
- **Tunable** walking and cycling speed, and a cycling route preference —
  fastest, safest, flattest or quiet streets.

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.][my-hacs-badge]][my-hacs]

Download it, then restart Home Assistant and add the integration:

[![Open your Home Assistant instance and start setting up a new integration.][my-config-flow-badge]][my-config-flow]

<details>
<summary>Or add it by hand</summary>

1. In HACS, choose **⋮ → Custom repositories**.
2. Add `https://github.com/r0mi/ha-peatus` with category **Integration**.
3. Download **Peatus.ee Public Transport** and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   *Peatus.ee*.

</details>

### Manual

Copy `custom_components/peatus` into your Home Assistant `config/custom_components`
directory and restart.

## Configuration

The config flow walks through four short steps:

1. **How to find the stop** — search by name, or paste a GTFS ID such as
   `estonia:1292`.
2. **Pick the stop.** Stop names repeat all over Estonia — there are two dozen
   stops called `Järve` — so the platforms of one stop are listed together: by
   name, then by place with Tallinn first, then by transport mode, so an
   interchange's trains sit beside each other rather than split up by its bus
   platforms. Places sort in Estonian alphabetical order. Each candidate is
   labelled with its code, municipality, direction, fare zone and the lines it
   serves, for example:

   ```
   Viru (12101-1) · Tallinna linn, Kesklinn · Bus · Harju1 · 1, 18, 20, 35, 36, 5
   Viru (7801122-1) · Tartumaa, Peipsiääre vald · Koosa suunas · Bus · 741, 764, 809
   ```

   Note that a stop is a single platform in one direction — pick the side of the
   road you actually wait on.
3. **Destination (optional).** Leave it empty for all departures. Enter a stop
   name to keep only the departures whose route continues to that stop *after*
   your departure stop.
4. **Modes, lines and update interval.** The mode of the stop you picked is
   preselected. **Lines** is optional: leave it empty for every route calling
   at the stop, or pick just the ones you travel with.

Modes, lines and the update interval can be changed later via the integration's
**Configure** button. Changing the stop or destination means adding a new entry.

Lines are matched by route number rather than by the data source's route ID,
because the feed carries a separate route per timetable period — around a
schedule change the same line exists twice, as `… (kuni 20.09)` and
`… (al 21.09)`. Filtering on the number keeps working across those changes; a
line withdrawn from the timetable altogether stays in the filter until you
remove it.

### Journey boards

Choose **Plan a journey between two places** on the first step, then:

1. **From and To.** Pick a zone, a person or a device tracker at each end.
   Prefer a **person** over a raw device tracker: a person follows whichever of
   its trackers is actually reporting a position, while a tracker that only
   knows which Wi-Fi network you are on publishes no coordinates at all and
   cannot be planned from. If you pick one that cannot be located, the board
   says so by name rather than quietly planning from the wrong place.

   Sensors and the text and select helpers are offered too, because an end is
   resolved by **state**, not by domain — anything whose state names a place
   works. That includes an entity holding *another* entity's ID, which is how a
   destination that changes with the time of day is usually built:

   ```yaml
   template:
     - sensor:
         - name: "Next destination"
           state: >
             {% if is_state('person.you', 'home') %}
               zone.work
             {% else %}
               zone.home
             {% endif %}
   ```

   Point the board's **To** at that sensor and it plans to work in the morning
   and home in the evening. Note the entity IDs of the board's sensors are
   built from the name the destination had when you created it, so give it a
   short one — or rename the device afterwards, which offers to rename all
   seven entities with it.
2. **Journey settings.** Modes, lines and update interval as above, plus
   walking speed, cycling speed, a cycling route preference and the longest
   walk you are willing to make.

Speeds are entered in km/h. They default to the planner's own assumptions —
4.8 km/h walking and 18 km/h cycling — and are a *maximum along the street*
rather than an average, so set them a little below your real pace: the planner
does not model junctions, lights or hills.

**Longest walk** is a limit rather than a preference, and the one setting worth
understanding before you change it. Every journey needing a longer walk to or
from a stop is thrown away *before* anything is ranked, so too tight a limit
does not make a plan walk less — it removes the plan. The default of 5000 m is
deliberately well above the planner's own 2000 m, which is too tight for a
destination that is not near a line.

The line filter behaves differently here than on a stop board. A journey is a
single take-it-or-leave-it offer, so it is only shown when **every** ride on it
uses one of your chosen lines — a plan that changes onto a line you filtered
out is not a plan you can use.

Journeys are planned from where you are *now*. Coordinates are rounded to about
eleven metres before planning, so a phone's GPS drifting while it sits on the
table does not re-plan the walk and rewrite every sensor on each poll.

## Updating on demand instead of on a timer

Every board polls on the interval you gave it. You can turn that off and
refresh it yourself instead, which is worth doing for a **journey board**:
planning a trip is a routing search rather than a timetable read, and a board
nobody is looking at still costs a request every five minutes all night.

Open the entry's ⋮ menu → **System options** and turn off *Enable polling for
updates*. The entry keeps its entities and its settings; it simply stops
refreshing on its own. The change takes effect immediately, in both directions,
because the integration reloads the entry whenever it is updated — no restart.

Then refresh it when it actually matters:

```yaml
automation:
  - alias: Refresh the commute board on weekday mornings
    triggers:
      - trigger: time_pattern
        minutes: "/5"
    conditions:
      - condition: time
        after: "06:30:00"
        before: "09:00:00"
        weekday: [mon, tue, wed, thu, fri]
      - condition: state
        entity_id: person.you
        state: home
    actions:
      - action: homeassistant.update_entity
        target:
          entity_id: sensor.peatus_home_work_journey_1
```

Three things worth knowing:

- **One entity refreshes the whole device.** Every sensor of a board belongs
  to one config entry, with a single coordinator behind it, so the automation
  names one `entity_id` rather than all seven. Listing them all would not fetch
  more — it would ask the same coordinator seven times over.
- **Bursts are debounced, not multiplied.** A refresh runs immediately if none
  has run in the last ten seconds. Further requests inside that window are not
  dropped — they collapse into a single extra run when it closes. So a trigger
  that fires repeatedly costs at most two plans per ten seconds, rather than
  one per trigger.
- **The first refresh always happens**, whatever the toggle says — once at
  setup and once after every Home Assistant restart. That is what lets a board
  report a problem (an origin that cannot be located, say) instead of sitting
  silently empty.

For the journey board above that is roughly 750 plans a month rather than the
8,600 a five-minute interval would make, without the board ever being stale at
the moment you look at it.

## Entities

### Stop board entities

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

**Visit device** links straight to that stop's page on peatus.ee.

The config entry title carries the stop code at both ends, e.g.
`Laagri (04409-1) → Järve (06805-1)`. Stop names repeat across Estonia and a
single name can cover several platforms — there are two rail stops called
Järve, one towards Paldiski and one towards Tallinn — so the code is what
tells two entries apart in the integration's list.

When a destination is configured it is included, so entity IDs stay distinct
between boards for the same stop:

```
sensor.peatus_viru_departure_1              # all departures from Viru
sensor.peatus_viru_vana_louna_departure_1   # Viru, only towards Vana-Lõuna
```

`Next departure` and `Departure 1` always hold the same value: both track the
soonest matching departure, with the same attributes. Their only difference is
the icon — `Next departure` keeps the clock, while the numbered sensors take
the icon of whatever vehicle is due. The alias is there for readability, so a
template can say what it means instead of relying on the reader knowing the
list is ordered by time. Either one works; nothing is lost by using
`Departure 1` throughout.

The state is a **timestamp**. Sensors report `unknown` when there are fewer
matching departures than 10, which is normal at night.

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
| `ride_minutes` | `14` | How long this departure takes to reach the destination |
| `arrival_time` | `2026-09-16T10:35:00+00:00` | When it gets there |

`ride_minutes` and `arrival_time` are only present when a destination is
configured, and are omitted for a departure whose ride the feed cannot measure.
The ride length is the scheduled one for that specific trip, which matters
because it is not constant along a line: bus 1 from Vana-Pääsküla to Viru takes
24 minutes off-peak and 34 at rush hour. The arrival is measured from the
*realtime* departure, so a service already running late arrives late too;
delay picked up during the ride itself is not reflected.

> **`minutes_until` is only refreshed when the sensor polls.** For a live
> countdown, use the state instead — Home Assistant's UI already renders a
> timestamp as relative time, and templates can use
> `as_timestamp(states('sensor.x')) - as_timestamp(now())`.

### Journey board entities

Each configured journey becomes a device with 7 sensors:

| Entity | State | Description |
| --- | --- | --- |
| `sensor.peatus_<from>_<to>_journey_1` … `_5` | timestamp | When you have to leave to catch that journey |
| `sensor.peatus_<from>_<to>_walk` | minutes | How long it takes to walk the whole way |
| `sensor.peatus_<from>_<to>_ride` | minutes | How long it takes to cycle |

There is deliberately **no "next journey" sensor**. Consecutive journeys can use
different lines and even different modes, so a single entity called "next" would
keep changing what it means — unlike a stop board, where every departure leaves
from the same platform.

The numbered sensors are timestamps for the same reason departures are: Home
Assistant renders the countdown itself. Walking and cycling are durations
instead, because they start whenever you do — a timestamp would only ever say
"now" and would be rewritten on every poll.

### Journey attributes

| Attribute | Example | Notes |
| --- | --- | --- |
| `duration_seconds` | `1712` | Authoritative total |
| `duration_minutes` | `29` | Rounded, for display |
| `start_time` | `2026-09-23T07:39:52+00:00` | Same as the state on a journey sensor; present on all seven so one card can read them alike |
| `arrival_time` | `2026-09-23T08:08:24+00:00` | |
| `departure_time` | `2026-09-23T07:48:00+00:00` | When the **first vehicle** leaves |
| `departure_stop`, `departure_stop_code` | `Vana-Pääsküla`, `04401-1` | |
| `walk_minutes`, `wait_minutes`, `walk_distance` | `12`, `0`, `796` | |
| `transfers` | `1` | One fewer than the number of rides |
| `routes`, `modes` | `["18", "34"]`, `["bus", "bus"]` | In the order ridden |
| `origin`, `destination` | `Home`, `Work` | |
| `origin_entity_id`, `destination_entity_id` | `person.romi`, `zone.work` | |
| `legs` | see below | Every segment of the journey |

The `departure_*` attributes are about **boarding**, not about leaving the
house, and are omitted entirely from a plan that boards nothing — a walk has no
stop to leave from.

Each entry in `legs` looks like this:

```json
{
  "mode": "bus",
  "start_time": "2026-09-23T07:48:00+00:00",
  "end_time": "2026-09-23T08:05:00+00:00",
  "duration": 1020,
  "minutes": 17,
  "route": "18",
  "route_long_name": "Viru keskus - Urda",
  "headsign": "Viru keskus",
  "color": "#de2c42",
  "text_color": "#ffffff",
  "from": "Vana-Pääsküla",
  "from_stop_code": "04401-1",
  "to": "Järve",
  "distance": 8045,
  "trip_id": "estonia:17206",
  "realtime": false
}
```

`mode` is one of `walk`, `bicycle`, `wait`, `bus`, `trolleybus`, `tram`, `rail`
or `ferry`. Walking, cycling and waiting legs carry no `route`, `color` or
`trip_id`, which is the simplest way for a card to tell a ride from the rest.

**The legs of a journey add up to `duration_seconds` exactly**, waiting
included, so a proportional bar is just `leg.duration / duration_seconds` per
segment with no arithmetic to get wrong. That is why waiting for a connection
appears as a `wait` leg of its own rather than only as a total: the data source
reports waiting as one number per journey, which cannot be split back across
two transfers.

Use `text_color` rather than assuming white — some lines publish dark text, and
route 191 in Tallinn is one of them.

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

This matters rather more for **journey boards** than for stop boards. A journey
sensor carries its whole `legs` list as an attribute, and Home Assistant stores
every attribute with every state it records — so seven sensors, each holding
several legs of nineteen fields, rewritten on each poll, adds up far faster
than a departure board's single timestamp does.

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

Show the next three departures, with when each one gets you there

```yaml
type: entities
title: Viru → Vana-Lõuna
entities:
  - entity: sensor.peatus_viru_departure_1
    type: custom:template-entity-row
    name: >
      {{ state_attr(config.entity, 'route') }}
      → {{ state_attr(config.entity, 'headsign') }}
    state: >-
      {% set dt = states(config.entity) | as_datetime %}
      {{ 'departing' if dt is none or dt <= now() else time_until(dt) }}
    secondary: >-
      {% set arrival = state_attr(config.entity, 'arrival_time') %}
      {% if arrival %}
        arrives {{ (arrival | as_datetime | as_local).strftime('%H:%M') }}
        ({{ state_attr(config.entity, 'ride_minutes') }} min)
      {% endif %}
  - entity: sensor.peatus_viru_departure_2
    type: custom:template-entity-row
    name: >
      {{ state_attr(config.entity, 'route') }}
      → {{ state_attr(config.entity, 'headsign') }}
    state: >-
      {% set dt = states(config.entity) | as_datetime %}
      {{ 'departing' if dt is none or dt <= now() else time_until(dt) }}
    secondary: >-
      {% set arrival = state_attr(config.entity, 'arrival_time') %}
      {% if arrival %}
        arrives {{ (arrival | as_datetime | as_local).strftime('%H:%M') }}
        ({{ state_attr(config.entity, 'ride_minutes') }} min)
      {% endif %}
  - entity: sensor.peatus_viru_departure_3
    type: custom:template-entity-row
    name: >
      {{ state_attr(config.entity, 'route') }}
      → {{ state_attr(config.entity, 'headsign') }}
    state: >-
      {% set dt = states(config.entity) | as_datetime %}
      {{ 'departing' if dt is none or dt <= now() else time_until(dt) }}
    secondary: >-
      {% set arrival = state_attr(config.entity, 'arrival_time') %}
      {% if arrival %}
        arrives {{ (arrival | as_datetime | as_local).strftime('%H:%M') }}
        ({{ state_attr(config.entity, 'ride_minutes') }} min)
      {% endif %}
```

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

Stop search goes through the Pelias geocoder at
`https://api.peatus.ee/geocoding/v1`, the same one web.peatus.ee searches with,
rather than OpenTripPlanner's `stops(name:)` field. That field parses its
argument as Lucene, so punctuation acts as an operator instead of text to match
— a hyphen reads as NOT, which makes hyphenated names like `Vana-Pääsküla`
match nothing — and it truncates every answer to ten stops without saying so.

Both geocoder endpoints are used, because neither is right alone for a stop
picker. `/autocomplete` ranks partial input well, but keeps only one platform
per stop name and locality: searching `Järve` offers one of the four platforms
in Tallinn, so the train towards Paldiski is listed and the one back is not.
`/search` has every platform, but pads its answer out with whatever else starts
alike. So the shortlist comes from `/autocomplete`, and `/search` is used only
to put back the platforms it collapsed — a stop is added when its name is
already in the shortlist, never otherwise.

The geocoder only identifies stops, so the matches are hydrated from the feed in
one batch to recover their modes and routes. If the geocoder cannot be reached,
the search falls back to `stops(name:)` with the query reduced to bare terms.

A line filter is applied to the fetched departures, since the API has no route
argument on any of its stoptimes fields. The request grows the same way a mode
filter's does, so the cost depends on how often your lines run: filtering a busy
stop down to its four frequent lines fills all ten sensors from the second
request, while picking a rarely served line at the same stop has to fetch the
full 150-departure window to find ten of them.

Journey boards use the API's `plan` query instead, which is a routing search
rather than a table read — an order of magnitude more expensive, which is why
they default to a slower poll and never ask for more than fifteen answers. Mode
filtering there is done by the **server**, since `plan` takes the modes as an
argument; line filtering is still done here, because `plan` has no route
argument at all. It does have `banned` and `preferred`, but both take route
*IDs* — the very thing that changes every timetable period — and `preferred` is
only a soft penalty rather than a filter.

Walking and cycling plans depend only on the two endpoints, not on any
timetable, so they are planned once and re-planned only when the rounded
coordinates actually move. A board that is not going anywhere therefore costs
one request per poll, not three.

The API reports waiting for a connection as a single number per journey, which
cannot be divided back among several transfers. So the gaps between legs are
reconstructed where they actually fall, as `wait` legs of their own. That keeps
one invariant true for every journey: its legs sum to its duration exactly.

Destination filtering is done by **route pattern**, not by walking every trip's
stop list on each poll: the set of patterns that reach your destination after
your origin is resolved once and cached for 6 hours, and each poll only asks for
each departure's pattern code. Without filters a poll transfers roughly 2 kB.
When a filter is active the integration requests a larger batch, and grows it
only if too many departures were discarded.

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

## Development

Requires Python 3.14.2 or newer — the floor set by the Home Assistant version
the tests run against.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-test.txt
pytest -q
ruff format --check .
ruff check .
```

These are the same commands CI runs, against the same pinned versions, so a
clean run here means a green `Validate` workflow.

## Credits

Timetable data comes from the Estonian national public transport registry via
[peatus.ee](https://web.peatus.ee/). This project is not affiliated with or
endorsed by peatus.ee or the Transport Administration.

[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[license]: https://github.com/r0mi/ha-peatus/blob/main/LICENSE
[license-badge]: https://img.shields.io/github/license/r0mi/ha-peatus?color=29c64d
[my-config-flow]: https://my.home-assistant.io/redirect/config_flow_start/?domain=peatus
[my-config-flow-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
[my-hacs]: https://my.home-assistant.io/redirect/hacs_repository/?owner=r0mi&repository=ha-peatus&category=integration
[my-hacs-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[release-badge]: https://img.shields.io/github/v/release/r0mi/ha-peatus?color=41BDF5
[releases]: https://github.com/r0mi/ha-peatus/releases
