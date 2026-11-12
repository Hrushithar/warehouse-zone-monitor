# Data dictionary

This file is the field-level reference for every table and column in [`schema.sql`](../schema.sql). `Required` reflects the SQLite `NOT NULL` declaration, not merely application expectations. PK means primary key, FK foreign key, and UQ unique constraint.

## Time and interval conventions

All stored timestamps are integer UTC Unix seconds. Display them in SQLite with `datetime(column_name, 'unixepoch')`. Effective assignments, requirement versions, lot dwells, and alarm windows are half-open intervals: `[start, end)`. The start instant is included; the end instant is excluded. A nullable effective or dwell end means the interval remains open.

For an alarm `[a_start, a_end)` and a same-zone lot dwell `[l_start, l_end)`, the intersection is:

```text
overlap_start   = max(a_start, l_start)
overlap_end     = min(a_end, l_end)       # treat a NULL lot end as unbounded
overlap_seconds = overlap_end - overlap_start
```

An `alarm_lot` row is written only when `overlap_end > overlap_start`. If a lot has multiple qualifying dwell rows, the monitor sums their positive overlap seconds by lot.

A threshold breach means observed values were outside a configured range for at least three consecutive cadence points. A `SENSOR_GAP` means expected cadence points were absent between two readings; no environmental value is known for that interval. Gap-linked lots therefore use `TELEMETRY_UNVERIFIED`, not `POTENTIAL_EXPOSURE`.

## Location and sensor tables

| Table | Column | SQLite type | Required | Meaning, units/format, allowed values, keys and constraints |
|---|---|---:|:---:|---|
| `warehouse` | `warehouse_id` | TEXT | Yes | PK; stable warehouse identifier, generated as `WH-001`. |
| `warehouse` | `name` | TEXT | Yes | Display name; SQLite requires nonblank trimmed text. |
| `zone` | `zone_id` | TEXT | Yes | PK; stable zone identifier, generated as `ZONE-WH-001-001`. |
| `zone` | `warehouse_id` | TEXT | Yes | FK → `warehouse.warehouse_id`. |
| `zone` | `name` | TEXT | Yes | Display name; nonblank; UQ with `warehouse_id`. |
| `zone` | `zone_type` | TEXT | Yes | `COLD`, `AMBIENT`, or `HOT`; enforced by `CHECK`. |
| `zone` | `occupancy_limit` | INTEGER | Yes | Maximum allowed people before a breach; SQLite requires `> 0`. |
| `zone` | `door_open_limit_seconds` | INTEGER | Yes | Allowed door-open duration in seconds; SQLite requires `> 0`. |
| `sensor` | `sensor_id` | TEXT | Yes | PK; stable device identifier, generated like `SNS-ZONE-WH-001-001-TEMP-01`; identity is retained across reassignment. |
| `sensor` | `sensor_type` | TEXT | Yes | `TEMP`, `HUMIDITY`, or `OCCUPANCY`; enforced by `CHECK`. |
| `sensor` | `sample_interval_seconds` | INTEGER | Yes | Expected cadence in seconds; SQLite requires `> 0`; generated sensors use 300 seconds. |
| `sensor_zone_assignment` | `assignment_id` | TEXT | Yes | PK; stable assignment identifier, normally `ASG-000001`. |
| `sensor_zone_assignment` | `sensor_id` | TEXT | Yes | FK → `sensor.sensor_id`; UQ with `zone_id` and `effective_from`. |
| `sensor_zone_assignment` | `zone_id` | TEXT | Yes | FK → `zone.zone_id`; UQ with `sensor_id` and `effective_from`. |
| `sensor_zone_assignment` | `effective_from` | INTEGER | Yes | Inclusive assignment start, UTC Unix seconds; UQ component. |
| `sensor_zone_assignment` | `effective_to` | INTEGER | No | Exclusive assignment end, UTC Unix seconds; `NULL` is open-ended; SQLite requires it to be after `effective_from` when present. |
| `sensor_zone_assignment` | `rationale` | TEXT | No | Optional explanation; SQLite requires nonblank trimmed text when present. |
| `sensor_reading` | `reading_id` | TEXT | Yes | PK; generated as `RDG-<sensor-id>-<YYYYMMDDTHHMMSSZ>`. |
| `sensor_reading` | `sensor_id` | TEXT | Yes | FK → `sensor.sensor_id`; UQ with `observed_at`. |
| `sensor_reading` | `observed_at` | INTEGER | Yes | Observation time, UTC Unix seconds; UQ with `sensor_id`. |
| `sensor_reading` | `value` | REAL | Yes | Reading value. Intended units depend on sensor type: °C, percent relative humidity, or whole people. SQLite only enforces non-null storage; range/integer rules are validated by code. |
| `door_event` | `door_event_id` | TEXT | Yes | PK; stable event identifier, generated as `DOR-000001`. |
| `door_event` | `zone_id` | TEXT | Yes | FK → `zone.zone_id`; UQ with `door_code` and `occurred_at`. |
| `door_event` | `door_code` | TEXT | Yes | Zone-scoped door identifier such as `DOOR-01`; nonblank; UQ component. |
| `door_event` | `occurred_at` | INTEGER | Yes | Event time, UTC Unix seconds; UQ with `zone_id` and `door_code`. |
| `door_event` | `event_type` | TEXT | Yes | `OPEN` or `CLOSE`; enforced by `CHECK`. |

## Requirements and inventory tables

| Table | Column | SQLite type | Required | Meaning, units/format, allowed values, keys and constraints |
|---|---|---:|:---:|---|
| `storage_requirement` | `requirement_id` | TEXT | Yes | PK; stable requirement-version identifier, generated as `REQ-001`. |
| `storage_requirement` | `product_code` | TEXT | Yes | Product family; nonblank; UQ with `effective_from`. |
| `storage_requirement` | `required_zone_type` | TEXT | Yes | Required `COLD`, `AMBIENT`, or `HOT` zone; enforced by `CHECK`. |
| `storage_requirement` | `temp_min_c` | REAL | Yes | Inclusive minimum temperature in °C; SQLite requires `temp_min_c <= temp_max_c`. |
| `storage_requirement` | `temp_max_c` | REAL | Yes | Inclusive maximum temperature in °C; paired-order `CHECK` with the minimum. |
| `storage_requirement` | `humidity_min_pct` | REAL | Yes | Inclusive minimum relative humidity, percent; SQLite requires 0–100 and not above the maximum. |
| `storage_requirement` | `humidity_max_pct` | REAL | Yes | Inclusive maximum relative humidity, percent; SQLite requires it between the minimum and 100. |
| `storage_requirement` | `effective_from` | INTEGER | Yes | Inclusive version start, UTC Unix seconds; UQ with `product_code`. |
| `storage_requirement` | `effective_to` | INTEGER | No | Exclusive version end, UTC Unix seconds; `NULL` is open-ended; must follow `effective_from` when present. |
| `inventory_lot` | `lot_id` | TEXT | Yes | PK; stable lot identifier, generated like `LOT-2026-000001`. |
| `inventory_lot` | `requirement_id` | TEXT | Yes | FK → `storage_requirement.requirement_id`; preserves the requirement version selected at receipt. |
| `inventory_lot` | `received_at` | INTEGER | Yes | Receipt time, UTC Unix seconds. Requirement effectiveness at receipt is validated by code, not SQLite. |
| `inventory_lot` | `unit_count` | INTEGER | Yes | Inventory units; SQLite requires `> 0`. |
| `lot_storage` | `storage_id` | TEXT | Yes | PK; stable dwell identifier, generated as `STG-000001`. |
| `lot_storage` | `lot_id` | TEXT | Yes | FK → `inventory_lot.lot_id`; UQ with `entered_at`. |
| `lot_storage` | `zone_id` | TEXT | Yes | FK → `zone.zone_id`. |
| `lot_storage` | `entered_at` | INTEGER | Yes | Inclusive dwell start, UTC Unix seconds; UQ with `lot_id`. |
| `lot_storage` | `exited_at` | INTEGER | No | Exclusive dwell end, UTC Unix seconds; `NULL` means still present; SQLite requires it to follow `entered_at` when present. |

## Incident and response tables

| Table | Column | SQLite type | Required | Meaning, units/format, allowed values, keys and constraints |
|---|---|---:|:---:|---|
| `alarm` | `alarm_id` | TEXT | Yes | PK; stable incident identifier, generated as `ALM-000001`. |
| `alarm` | `zone_id` | TEXT | Yes | FK → `zone.zone_id`; attributed incident zone. Source-zone consistency is validated by code. |
| `alarm` | `alarm_type` | TEXT | Yes | `TEMP_EXCURSION`, `HUMIDITY_EXCURSION`, `HIGH_OCCUPANCY`, `DOOR_OPEN_TOO_LONG`, `SENSOR_GAP`, or `INCOMPATIBLE_STORAGE`; enforced by `CHECK`. |
| `alarm` | `started_at` | INTEGER | Yes | Inclusive incident start, UTC Unix seconds. |
| `alarm` | `window_end_at` | INTEGER | Yes | Exclusive incident end, UTC Unix seconds; SQLite requires it after `started_at`. |
| `alarm` | `detected_at` | INTEGER | Yes | Detection time, UTC Unix seconds; SQLite requires `>= started_at`. It can follow `window_end_at` for a gap because the next reading reveals the missing interval. |
| `alarm` | `rule_version` | TEXT | Yes | Nonblank detector rule version; current value `v1`. |
| `alarm` | `reason` | TEXT | Yes | Nonblank human-readable decision explanation. |
| `alarm` | `sensor_id` | TEXT | No | FK → `sensor.sensor_id`; the source for temperature, humidity, occupancy, and gap alarms. The alarm-type `CHECK` makes this required for those types and forbids the other source columns. |
| `alarm` | `door_event_id` | TEXT | No | FK → `door_event.door_event_id`; opening event for a door-duration alarm. The alarm-type `CHECK` makes this the sole source for that type. |
| `alarm` | `storage_id` | TEXT | No | FK → `lot_storage.storage_id`; offending dwell for incompatible storage. The alarm-type `CHECK` makes this the sole source for that type. |
| `alarm_lot` | `alarm_id` | TEXT | Yes | FK → `alarm.alarm_id`; composite PK with `lot_id`. |
| `alarm_lot` | `lot_id` | TEXT | Yes | FK → `inventory_lot.lot_id`; composite PK with `alarm_id`. |
| `alarm_lot` | `overlap_seconds` | INTEGER | Yes | Sum of positive same-zone dwell/alarm intersections, in seconds; SQLite requires `> 0`. |
| `alarm_lot` | `impact_type` | TEXT | Yes | `POTENTIAL_EXPOSURE`, `TELEMETRY_UNVERIFIED`, or `PLACEMENT_VIOLATION`; enforced by `CHECK`. |
| `diagnostic` | `diagnostic_id` | TEXT | Yes | PK; stable diagnostic identifier, generated as `DGN-000001` after deterministic signature sorting. |
| `diagnostic` | `issue_type` | TEXT | Yes | `SENSOR_DRIFT`, `AMBIGUOUS_SENSOR_ZONE`, or `INVALID_DOOR_SEQUENCE`; enforced by `CHECK`. |
| `diagnostic` | `zone_id` | TEXT | Yes | FK → `zone.zone_id`; zone associated with the finding. |
| `diagnostic` | `sensor_id` | TEXT | No | FK → `sensor.sensor_id`; required for drift and ambiguous-assignment findings and forbidden for invalid door sequences. |
| `diagnostic` | `door_event_id` | TEXT | No | FK → `door_event.door_event_id`; required for an invalid door sequence and forbidden for sensor diagnostics. |
| `diagnostic` | `started_at` | INTEGER | Yes | Inclusive diagnostic interval start, UTC Unix seconds. |
| `diagnostic` | `window_end_at` | INTEGER | Yes | Exclusive diagnostic interval end, UTC Unix seconds; SQLite requires it after `started_at`. |
| `alarm_state_event` | `alarm_id` | TEXT | Yes | FK → `alarm.alarm_id`; composite PK with `event_seq`. |
| `alarm_state_event` | `event_seq` | INTEGER | Yes | Positive per-alarm ordinal; composite PK with `alarm_id`; replay uses 1, 2, and optionally 3. |
| `alarm_state_event` | `state` | TEXT | Yes | `OPEN`, `ACKNOWLEDGED`, or `RESOLVED`; enforced by `CHECK`. Sequence and timestamp order are validated by code. |
| `alarm_state_event` | `occurred_at` | INTEGER | Yes | State-event time, UTC Unix seconds. |
| `alarm_state_event` | `actor_role` | TEXT | Yes | `SYSTEM`, `OPERATOR`, `QUALITY`, `MAINTENANCE`, `PLANNER`, or `OT_IT`; enforced by `CHECK`. |
| `inspection` | `inspection_id` | TEXT | Yes | PK; stable inspection identifier, generated as `INSP-000001`; also UQ with `alarm_id` to support the composite remediation FK. |
| `inspection` | `alarm_id` | TEXT | Yes | FK → `alarm.alarm_id`; UQ with `inspection_id`. The schema allows multiple inspections per alarm. |
| `inspection` | `inspected_at` | INTEGER | Yes | Inspection time, UTC Unix seconds; ordering after detection is validated by code. |
| `inspection` | `inspector_role` | TEXT | Yes | `QUALITY`, `MAINTENANCE`, or `OT_IT`; enforced by `CHECK`. |
| `inspection` | `finding` | TEXT | Yes | `RISK_CONFIRMED`, `TELEMETRY_UNVERIFIED`, or `NO_PRODUCT_IMPACT`; enforced by `CHECK`. |
| `inspection` | `notes` | TEXT | Yes | Nonblank audit notes. |
| `remediation` | `remediation_id` | TEXT | Yes | PK; stable action identifier, generated as `REM-000001`. |
| `remediation` | `inspection_id` | TEXT | Yes | With `alarm_id`, composite FK → `inspection(inspection_id, alarm_id)`. |
| `remediation` | `alarm_id` | TEXT | Yes | Composite FK with `inspection_id` to `inspection`; with non-null `lot_id`, composite FK to `alarm_lot`. |
| `remediation` | `lot_id` | TEXT | No | With `alarm_id`, FK → `alarm_lot(alarm_id, lot_id)`. Required for lot actions and forbidden for equipment/environment actions by `CHECK`. |
| `remediation` | `action_type` | TEXT | Yes | `QUARANTINE_LOT`, `RELEASE_LOT`, `MOVE_LOT`, `REPAIR_SENSOR`, `CHECK_DOOR`, or `ADJUST_ENVIRONMENT`; enforced by `CHECK`. |
| `remediation` | `performed_at` | INTEGER | Yes | Action time, UTC Unix seconds; ordering and quarantine-dwell membership are validated by code. |
| `remediation` | `notes` | TEXT | Yes | Nonblank simulated-action description. |

## Relationships and cardinality

- `warehouse` 1:N `zone`; every zone belongs to one warehouse.
- `sensor` 1:N `sensor_reading`; each reading belongs to one sensor.
- `sensor` N:M `zone` over time through `sensor_zone_assignment`. Each bridge row is one effective assignment. The generated baseline has one open assignment per sensor; the ambiguous fixture adds a cross-zone overlap.
- `zone` 1:N `door_event`; a door is identified within a zone by `door_code`, and events form its chronological history.
- `storage_requirement` 1:N `inventory_lot`; each lot retains one versioned requirement.
- `inventory_lot` N:M `zone` over time through `lot_storage`. Each bridge row is one dwell; a move closes a dwell and creates another. The seeded full profile contains 12 lots with sequential compatible dwells and 5 current dwells with `exited_at = NULL`; starter retains one closed dwell per lot.
- `zone` 1:N `alarm`; each alarm belongs to one zone.
- `sensor`, `door_event`, and `lot_storage` each have a possible 1:N source relationship to `alarm`. Exactly one source column is populated as determined by `alarm_type`.
- `alarm` N:M `inventory_lot` through `alarm_lot`. An alarm can have no lot links when no inventory overlaps its window. A lot can be linked to many alarms.
- `zone` 1:N `diagnostic`; each diagnostic belongs to one zone. `sensor` 1:N `diagnostic` for drift and ambiguous-assignment findings, while `door_event` 1:N `diagnostic` supplies the source for invalid sequences. The source-type `CHECK` requires exactly the applicable reference.
- `alarm` 1:N `alarm_state_event`; rows are append-only history and the greatest `event_seq` is the current state.
- `alarm` 1:N `inspection`; the current replay writes one inspection per alarm, while the schema permits more.
- `inspection` 1:N `remediation`; an inspection can produce no actions or several actions.
- `alarm_lot` 1:N `remediation` for lot-specific actions through `(alarm_id, lot_id)`. Equipment or environment actions have `lot_id = NULL` and remain tied to the alarm through their inspection.

## What SQLite enforces

With `PRAGMA foreign_keys = ON`, SQLite enforces:

- all PK, UQ, FK, and `NOT NULL` declarations shown above;
- nonblank checks on selected names, codes, rationales, rule text, and notes;
- enum checks for zone, sensor, door, alarm, impact, state, role, finding, and action values;
- positive numeric checks for configured limits, cadence, units, overlap seconds, and event sequence;
- row-local ordering for assignment, requirement, dwell, and alarm intervals;
- requirement range ordering and humidity bounds;
- the exactly-one-source pattern for each `alarm_type`;
- the applicable sensor-or-door source pattern for each diagnostic issue type;
- whether an action type requires or forbids `remediation.lot_id`.

SQLite does not infer business correctness merely because a value has type `REAL` or `INTEGER`; SQLite uses dynamic typing, and several cross-row rules require code-level validation.

## What replay and export validation enforce

The Python stages add rules that are not expressible in the current row-local schema checks:

- humidity readings are in 0–100% RH; occupancy is a nonnegative whole number; readings align to the sensor cadence and replay start;
- exactly one effective zone assignment exists when attributing a sensor reading or sensor-sourced alarm; conflicting cross-zone assignments without a rationale are diagnosed;
- diagnostic IDs are assigned after deterministic signature sorting; diagnostics are inserted in the same transaction as alarms, and the compatible `diagnostics.json` is derived from those persisted rows;
- temperature/humidity limits come from the one active requirement for the zone type at replay start; threshold alarms need three consecutive breaches; drift uses three consecutive peer differences strictly greater than 1.5 °C;
- door events follow a valid chronological open/close sequence, and duration alarms are strictly over the zone limit;
- requirement versions are effective when a lot is received; dwell starts are not before receipt; dwells for one lot do not overlap;
- full-profile validation requires 10,012 dwell rows, exactly 12 multi-dwell lots, and exactly 5 open-ended dwells while retaining 10,000 distinct inventory lots;
- alarm source zone/time matches the effective sensor assignment, opening door event, or offending storage dwell;
- affected lots have positive, correctly summed same-zone overlap, the correct impact type, and placement alarms link only their source dwell's lot;
- inspections occur no earlier than detection; remediation occurs no earlier than inspection; quarantine occurs while the affected lot is still dwelling in the alarm zone;
- lifecycle history starts with `OPEN` at detection, continues with ordered `ACKNOWLEDGED`, and optionally ends with ordered `RESOLVED`; no sequence above 3 is accepted by the current export validator;
- evaluator decisions match separate ground truth, and evaluator lot links match an independent recomputation.

The monitor also deliberately skips zone attribution for a reading with zero or multiple active assignments. Gap detection currently covers missing cadence points between observations only; without an independent expected monitoring window it cannot identify leading, trailing, or complete silence.
