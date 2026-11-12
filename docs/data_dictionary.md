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




| Table | Field definitions |
|---|---|
| `storage_requirement` | `requirement_id` TEXT PK. `product_code` TEXT, nonblank. `required_zone_type` TEXT, one of `COLD`, `AMBIENT`, `HOT`. `temp_min_c`, `temp_max_c` REAL in °C, ordered inclusive limits. `humidity_min_pct`, `humidity_max_pct` REAL in % RH, ordered inclusive limits between 0 and 100. `effective_from` INTEGER, inclusive start. `effective_to` INTEGER, optional exclusive end after start. `product_code` + `effective_from` is unique. |
| `inventory_lot` | `lot_id` TEXT PK in `LOT-2026-000001` format. `requirement_id` TEXT FK to the requirement active at receipt. `received_at` INTEGER. `unit_count` INTEGER, positive count of inventory units. |
| `lot_storage` | `storage_id` TEXT PK. `lot_id` and `zone_id` TEXT FKs. `entered_at` INTEGER, at or after receipt. `exited_at` INTEGER, optional and strictly after entry. `lot_id` + `entered_at` is unique. Validate dwell intervals for each lot against overlaps. |
| `alarm` | `alarm_id` TEXT PK. `zone_id` TEXT FK. `alarm_type` TEXT, one of `TEMP_EXCURSION`, `HUMIDITY_EXCURSION`, `HIGH_OCCUPANCY`, `DOOR_OPEN_TOO_LONG`, `SENSOR_GAP`, `INCOMPATIBLE_STORAGE`. `started_at`, `window_end_at`, `detected_at` INTEGER. Window end must follow start, and detection must be at or after start. `rule_version` and `reason` TEXT, nonblank. `sensor_id`, `door_event_id`, `storage_id` TEXT FKs. Exactly one applies based on alarm type. |
| `alarm_lot` | `alarm_id` and `lot_id` TEXT FKs and composite PK. `overlap_seconds` INTEGER, positive duration in seconds derived from intersecting intervals. `impact_type` TEXT, one of `POTENTIAL_EXPOSURE`, `TELEMETRY_UNVERIFIED`, `PLACEMENT_VIOLATION`. |
| `alarm_state_event` | `alarm_id` TEXT FK and `event_seq` positive INTEGER form the composite PK. `state` TEXT, one of `OPEN`, `ACKNOWLEDGED`, `RESOLVED`. `occurred_at` INTEGER. `actor_role` TEXT, one of `SYSTEM`, `OPERATOR`, `QUALITY`, `MAINTENANCE`, `PLANNER`, `OT_IT`. Validate sequence and timestamp order. |
| `inspection` | `inspection_id` TEXT PK. `alarm_id` TEXT FK. `inspected_at` INTEGER, at or after detection. `inspector_role` TEXT, one of `QUALITY`, `MAINTENANCE`, `OT_IT`. `finding` TEXT, one of `RISK_CONFIRMED`, `TELEMETRY_UNVERIFIED`, `NO_PRODUCT_IMPACT`. `notes` TEXT, nonblank. `inspection_id` + `alarm_id` is unique. |
| `remediation` | `remediation_id` TEXT PK. `inspection_id` + `alarm_id` TEXT composite FK. `lot_id` TEXT, required for lot actions and otherwise optional in the row with a `NULL` value. When present, `alarm_id` + `lot_id` must reference `alarm_lot`. `action_type` TEXT, one of `QUARANTINE_LOT`, `RELEASE_LOT`, `MOVE_LOT`, `REPAIR_SENSOR`, `CHECK_DOOR`, `ADJUST_ENVIRONMENT`. `performed_at` INTEGER, at or after inspection. `notes` TEXT, nonblank. |