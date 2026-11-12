# Data dictionary

All timestamps are UTC Unix seconds. Time intervals are half-open. An empty end means the interval is still open. Fields are non-unique unless identified as a primary key (PK) or unique (UQ), including composite keys. FK denotes a foreign key. IDs, codes, enums and prose have no physical unit.

| Table | Field | Type | Required | Allowed values, units, or validation |
|---|---|---|---|---|
| `warehouse` | `warehouse_id` | TEXT | Yes | PK, generated as `WH-001` |
| `warehouse` | `name` | TEXT | Yes | Nonblank display name |
| `zone` | `zone_id` | TEXT | Yes | PK, generated as `ZONE-WH-001-001` |
| `zone` | `warehouse_id` | TEXT | Yes | FK to `warehouse` |
| `zone` | `name` | TEXT | Yes | Nonblank, UQ within warehouse |
| `zone` | `zone_type` | TEXT | Yes | `COLD`, `AMBIENT`, `HOT` |
| `zone` | `occupancy_limit` | INTEGER | Yes | Positive count of people |
| `zone` | `door_open_limit_seconds` | INTEGER | Yes | Positive duration in seconds |
| `sensor` | `sensor_id` | TEXT | Yes | PK, generated as `SNS-ZONE-WH-001-001-TEMP-01`. Retain the ID after reassignment |
| `sensor` | `sensor_type` | TEXT | Yes | `TEMP`, `HUMIDITY`, `OCCUPANCY` |
| `sensor` | `sample_interval_seconds` | INTEGER | Yes | Positive expected time between samples, in seconds |
| `sensor_zone_assignment` | `assignment_id` | TEXT | Yes | PK, generated as `ASG-000001` |
| `sensor_zone_assignment` | `sensor_id` | TEXT | Yes | FK to `sensor`. UQ with `zone_id` and `effective_from` |
| `sensor_zone_assignment` | `zone_id` | TEXT | Yes | FK to `zone` |
| `sensor_zone_assignment` | `effective_from` | INTEGER | Yes | Inclusive start, UTC Unix seconds |
| `sensor_zone_assignment` | `effective_to` | INTEGER | No | Exclusive end, UTC Unix seconds. Must follow start. `NULL` means open-ended |
| `sensor_zone_assignment` | `rationale` | TEXT | No | Nonblank when supplied. Conflicting cross-zone assignments without documented rationale are flagged by validation |
| `sensor_reading` | `reading_id` | TEXT | Yes | PK, `RDG-<sensor-id>-<YYYYMMDDTHHMMSSZ>` |
| `sensor_reading` | `sensor_id` | TEXT | Yes | FK to `sensor`. UQ with `observed_at` |
| `sensor_reading` | `observed_at` | INTEGER | Yes | UTC Unix seconds. Validate against the active sensor assignment |
| `sensor_reading` | `value` | REAL | Yes | Finite value. Temperature in °C, humidity in 0–100% RH, occupancy in whole people ≥ 0 |
| `door_event` | `door_event_id` | TEXT | Yes | PK, generated as `DOR-000001` |
| `door_event` | `zone_id` | TEXT | Yes | FK to `zone` |
| `door_event` | `door_code` | TEXT | Yes | Nonblank code scoped to a zone, such as `DOOR-01` |
| `door_event` | `occurred_at` | INTEGER | Yes | UTC Unix seconds. UQ with `zone_id` and `door_code` |
| `door_event` | `event_type` | TEXT | Yes | `OPEN` or `CLOSE`. Validate chronological state transitions per door |


## Relationships and time conventions

- Warehouse to Zone is 1:N. Each zone belongs to one warehouse.
- Sensor to SensorReading is 1:N. Each reading belongs to one sensor.
- Zone to DoorEvent is 1:N. Each event belongs to one zone.
- Sensor to Zone is N:M over time through SensorZoneAssignment.
  Each assignment has one sensor and one zone.
- Assignment intervals are half-open: [effective_from, effective_to).
  When a sensor moves, close its current interval and insert a new assignment.
- SQL enforces row-level constraints and foreign keys. The pipeline validator
  checks cross-row rules, including conflicting assignments and door sequences,
  and cross-table rules, including sensor-specific value ranges.


| Relationship | Cardinality and rule |
|---|---|
| StorageRequirement → InventoryLot | 1:N. Each lot references the requirement version active when it was received. The lot keeps that version for traceability. |
| InventoryLot ↔ Zone | N:M over time through `lot_storage`. Each dwell row has one lot and one zone. A move closes one interval and creates another. |
| Zone → Alarm | 1:N. Each alarm belongs to one zone and points to the sensor, door event, or placement that triggered it. |
| Alarm ↔ InventoryLot | N:M through `alarm_lot`. An alarm may have zero affected lots if the zone was empty. |
| Alarm → AlarmStateEvent | 1:N. Append `OPEN → ACKNOWLEDGED → RESOLVED`. The latest event determines current state. |
| Alarm → Inspection → Remediation | 1:N at each step. An inspection may produce zero or more actions. Lot-level actions require an affected-lot link for the same alarm. |

- Sensor, DoorEvent and LotStorage each have a 1:N relationship to Alarm. An alarm has exactly one of these three optional trigger references, according to its type.
- An AlarmLot row can have zero or more remediation records. Each lot-specific remediation references exactly one AlarmLot row, and equipment remediation has no lot reference.

## Inventory and response fields

| Table | Field | Type | Required | Allowed values, units, or validation |
|---|---|---|---|---|
| storage_requirement | requirement_id | TEXT | Yes | PK. Stable requirement-version ID. |
| storage_requirement | product_code | TEXT | Yes | Nonblank. UQ with effective_from. |
| storage_requirement | required_zone_type | TEXT | Yes | COLD, AMBIENT, HOT. |
| storage_requirement | temp_min_c | REAL | Yes | Inclusive °C minimum, at or below temp_max_c. |
| storage_requirement | temp_max_c | REAL | Yes | Inclusive °C maximum, at or above temp_min_c. |
| storage_requirement | humidity_min_pct | REAL | Yes | Inclusive minimum in % RH, 0–100 and at or below humidity_max_pct. |
| storage_requirement | humidity_max_pct | REAL | Yes | Inclusive maximum in % RH, between humidity_min_pct and 100. |
| storage_requirement | effective_from | INTEGER | Yes | Inclusive start. UQ with product_code. |
| storage_requirement | effective_to | INTEGER | No | Exclusive end, after effective_from when supplied. |
| inventory_lot | lot_id | TEXT | Yes | PK, e.g. LOT-2026-000001. |
| inventory_lot | requirement_id | TEXT | Yes | FK to requirement version active at receipt. |
| inventory_lot | received_at | INTEGER | Yes | Receipt time within the requirement’s effective interval. |
| inventory_lot | unit_count | INTEGER | Yes | Positive count of inventory units. |
| lot_storage | storage_id | TEXT | Yes | PK. Stable dwell ID. |
| lot_storage | lot_id | TEXT | Yes | FK to inventory_lot. UQ with entered_at. |
| lot_storage | zone_id | TEXT | Yes | FK to zone. |
| lot_storage | entered_at | INTEGER | Yes | Inclusive start at or after receipt. UQ with lot_id. Dwells for one lot cannot overlap. |
| lot_storage | exited_at | INTEGER | No | Exclusive end, after entered_at when supplied. |
| alarm | alarm_id | TEXT | Yes | PK. Stable alarm ID. |
| alarm | zone_id | TEXT | Yes | FK to zone. Must match the trigger’s zone at the start of the incident. |
| alarm | alarm_type | TEXT | Yes | TEMP_EXCURSION, HUMIDITY_EXCURSION, HIGH_OCCUPANCY, DOOR_OPEN_TOO_LONG, SENSOR_GAP, INCOMPATIBLE_STORAGE. |
| alarm | started_at | INTEGER | Yes | Inclusive incident start. |
| alarm | window_end_at | INTEGER | Yes | Exclusive incident end, after started_at. |
| alarm | detected_at | INTEGER | Yes | Detection time at or after started_at. |
| alarm | rule_version | TEXT | Yes | Nonblank rule version. |
| alarm | reason | TEXT | Yes | Nonblank decision explanation. |
| alarm | sensor_id | TEXT | Conditional | FK to sensor. Required for temperature, humidity, occupancy and gap alarms. NULL otherwise. |
| alarm | door_event_id | TEXT | Conditional | FK to the opening door event. Required for long-open alarms. NULL otherwise. |
| alarm | storage_id | TEXT | Conditional | FK to lot_storage. Required for incompatible storage. NULL otherwise. |
| alarm_lot | alarm_id | TEXT | Yes | FK to alarm. Composite PK with lot_id. |
| alarm_lot | lot_id | TEXT | Yes | FK to inventory_lot. Composite PK with alarm_id. |
| alarm_lot | overlap_seconds | INTEGER | Yes | Positive seconds of intersecting zone dwell and alarm intervals. |
| alarm_lot | impact_type | TEXT | Yes | POTENTIAL_EXPOSURE, TELEMETRY_UNVERIFIED, PLACEMENT_VIOLATION. |
| alarm_state_event | alarm_id | TEXT | Yes | FK to alarm. Composite PK with event_seq. |
| alarm_state_event | event_seq | INTEGER | Yes | Positive event ordinal. Composite PK with alarm_id. Replay uses 1, 2 and optional 3. |
| alarm_state_event | state | TEXT | Yes | OPEN, ACKNOWLEDGED, RESOLVED, in that sequence. |
| alarm_state_event | occurred_at | INTEGER | Yes | First event equals detection time. Later events follow in timestamp order. |
| alarm_state_event | actor_role | TEXT | Yes | SYSTEM, OPERATOR, QUALITY, MAINTENANCE, PLANNER, OT_IT. |
| inspection | inspection_id | TEXT | Yes | PK. Also forms the parent key for remediation with alarm_id. |
| inspection | alarm_id | TEXT | Yes | FK to alarm. UQ with inspection_id. |
| inspection | inspected_at | INTEGER | Yes | At or after detection. |
| inspection | inspector_role | TEXT | Yes | QUALITY, MAINTENANCE, OT_IT. |
| inspection | finding | TEXT | Yes | RISK_CONFIRMED, TELEMETRY_UNVERIFIED, NO_PRODUCT_IMPACT. |
| inspection | notes | TEXT | Yes | Nonblank. Records evidence and any inventory location follow-up. |
| remediation | remediation_id | TEXT | Yes | PK. Stable action ID. |
| remediation | inspection_id | TEXT | Yes | Composite FK with alarm_id to inspection. |
| remediation | alarm_id | TEXT | Yes | Composite FK with inspection_id to inspection, and with a present lot_id to alarm_lot. |
| remediation | lot_id | TEXT | Conditional | Required for quarantine, release and move. NULL for equipment actions. Composite FK with alarm_id. |
| remediation | action_type | TEXT | Yes | QUARANTINE_LOT, RELEASE_LOT, MOVE_LOT, REPAIR_SENSOR, CHECK_DOOR, ADJUST_ENVIRONMENT. |
| remediation | performed_at | INTEGER | Yes | At or after inspection. A quarantine must fall inside an affected-zone dwell. |
| remediation | notes | TEXT | Yes | Nonblank description of simulated action. |

## Labelled scenarios

| Scenario | Input change | Expected decision |
|---|---|---|
| Temperature excursion | Cold-zone sensor reports 11 °C for three samples | `TEMP_EXCURSION` |
| Humidity excursion | Ambient-zone sensor reports 72% RH for three samples | `HUMIDITY_EXCURSION` |
| High occupancy | Hot-zone sensor reports 12 people for three samples | `HIGH_OCCUPANCY` |
| Long door opening | Cold-zone door stays open for 240 seconds | `DOOR_OPEN_TOO_LONG` |
| Sensor outage | Three consecutive samples are missing | `SENSOR_GAP` |
| Sensor drift | One temperature sensor gradually diverges from its peer | `SENSOR_DRIFT` diagnostic |
| Incompatible placement | One chilled lot is placed in the ambient zone | `INCOMPATIBLE_STORAGE` |
| Conflicting assignment | One sensor has overlapping assignments in two zones | `AMBIGUOUS_SENSOR_ZONE` diagnostic |

The export also contains labelled output omissions in `negative_fixtures.json`. Each record identifies the output stage, the exact alarm or alarm-lot key to omit, and the evaluation failure expected from that omission.
