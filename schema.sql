PRAGMA foreign_keys = ON;

CREATE TABLE warehouse (
    warehouse_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0)
);

CREATE TABLE zone (
    zone_id TEXT PRIMARY KEY NOT NULL,
    warehouse_id TEXT NOT NULL REFERENCES warehouse(warehouse_id),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    zone_type TEXT NOT NULL CHECK (zone_type IN ('COLD', 'AMBIENT', 'HOT')),
    occupancy_limit INTEGER NOT NULL CHECK (occupancy_limit > 0),
    door_open_limit_seconds INTEGER NOT NULL CHECK (door_open_limit_seconds > 0),
    UNIQUE (warehouse_id, name)
);

CREATE TABLE sensor (
    sensor_id TEXT PRIMARY KEY NOT NULL,
    sensor_type TEXT NOT NULL CHECK (sensor_type IN ('TEMP', 'HUMIDITY', 'OCCUPANCY')),
    sample_interval_seconds INTEGER NOT NULL CHECK (sample_interval_seconds > 0)
);

CREATE TABLE sensor_zone_assignment (
    assignment_id TEXT PRIMARY KEY NOT NULL,
    sensor_id TEXT NOT NULL REFERENCES sensor(sensor_id),
    zone_id TEXT NOT NULL REFERENCES zone(zone_id),
    effective_from INTEGER NOT NULL,
    effective_to INTEGER,
    rationale TEXT CHECK (rationale IS NULL OR length(trim(rationale)) > 0),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    UNIQUE (sensor_id, zone_id, effective_from)
);

CREATE TABLE sensor_reading (
    reading_id TEXT PRIMARY KEY NOT NULL,
    sensor_id TEXT NOT NULL REFERENCES sensor(sensor_id),
    observed_at INTEGER NOT NULL,
    value REAL NOT NULL,
    UNIQUE (sensor_id, observed_at)
);

CREATE TABLE door_event (
    door_event_id TEXT PRIMARY KEY NOT NULL,
    zone_id TEXT NOT NULL REFERENCES zone(zone_id),
    door_code TEXT NOT NULL CHECK (length(trim(door_code)) > 0),
    occurred_at INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('OPEN', 'CLOSE')),
    UNIQUE (zone_id, door_code, occurred_at)
);

CREATE INDEX idx_assignment_sensor_time
    ON sensor_zone_assignment (sensor_id, effective_from, effective_to);

CREATE INDEX idx_assignment_zone_time
    ON sensor_zone_assignment (zone_id, effective_from, effective_to);



CREATE TABLE storage_requirement (
    requirement_id TEXT PRIMARY KEY NOT NULL,
    product_code TEXT NOT NULL CHECK (length(trim(product_code)) > 0),
    required_zone_type TEXT NOT NULL
        CHECK (required_zone_type IN ('COLD', 'AMBIENT', 'HOT')),
    temp_min_c REAL NOT NULL,
    temp_max_c REAL NOT NULL,
    humidity_min_pct REAL NOT NULL,
    humidity_max_pct REAL NOT NULL,
    effective_from INTEGER NOT NULL,
    effective_to INTEGER,
    CHECK (temp_min_c <= temp_max_c),
    CHECK (humidity_min_pct BETWEEN 0 AND 100),
    CHECK (humidity_max_pct BETWEEN humidity_min_pct AND 100),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    UNIQUE (product_code, effective_from)
);

CREATE TABLE inventory_lot (
    lot_id TEXT PRIMARY KEY NOT NULL,
    requirement_id TEXT NOT NULL REFERENCES storage_requirement(requirement_id),
    received_at INTEGER NOT NULL,
    unit_count INTEGER NOT NULL CHECK (unit_count > 0)
);

CREATE TABLE lot_storage (
    storage_id TEXT PRIMARY KEY NOT NULL,
    lot_id TEXT NOT NULL REFERENCES inventory_lot(lot_id),
    zone_id TEXT NOT NULL REFERENCES zone(zone_id),
    entered_at INTEGER NOT NULL,
    exited_at INTEGER,
    CHECK (exited_at IS NULL OR exited_at > entered_at),
    UNIQUE (lot_id, entered_at)
);

CREATE TABLE alarm (
    alarm_id TEXT PRIMARY KEY NOT NULL,
    zone_id TEXT NOT NULL REFERENCES zone(zone_id),
    alarm_type TEXT NOT NULL CHECK (alarm_type IN (
        'TEMP_EXCURSION', 'HUMIDITY_EXCURSION', 'HIGH_OCCUPANCY',
        'DOOR_OPEN_TOO_LONG', 'SENSOR_GAP', 'INCOMPATIBLE_STORAGE'
    )),
    started_at INTEGER NOT NULL,
    window_end_at INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    rule_version TEXT NOT NULL CHECK (length(trim(rule_version)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    sensor_id TEXT REFERENCES sensor(sensor_id),
    door_event_id TEXT REFERENCES door_event(door_event_id),
    storage_id TEXT REFERENCES lot_storage(storage_id),
    CHECK (window_end_at > started_at),
    CHECK (detected_at >= started_at),
    CHECK (
        (alarm_type IN (
            'TEMP_EXCURSION', 'HUMIDITY_EXCURSION', 'HIGH_OCCUPANCY', 'SENSOR_GAP'
        ) AND sensor_id IS NOT NULL AND door_event_id IS NULL AND storage_id IS NULL)
        OR
        (alarm_type = 'DOOR_OPEN_TOO_LONG' AND sensor_id IS NULL
            AND door_event_id IS NOT NULL AND storage_id IS NULL)
        OR
        (alarm_type = 'INCOMPATIBLE_STORAGE' AND sensor_id IS NULL
            AND door_event_id IS NULL AND storage_id IS NOT NULL)
    )
);

CREATE TABLE alarm_lot (
    alarm_id TEXT NOT NULL REFERENCES alarm(alarm_id),
    lot_id TEXT NOT NULL REFERENCES inventory_lot(lot_id),
    overlap_seconds INTEGER NOT NULL CHECK (overlap_seconds > 0),
    impact_type TEXT NOT NULL CHECK (impact_type IN (
        'POTENTIAL_EXPOSURE', 'TELEMETRY_UNVERIFIED', 'PLACEMENT_VIOLATION'
    )),
    PRIMARY KEY (alarm_id, lot_id)
);

CREATE TABLE alarm_state_event (
    alarm_id TEXT NOT NULL REFERENCES alarm(alarm_id),
    event_seq INTEGER NOT NULL CHECK (event_seq > 0),
    state TEXT NOT NULL CHECK (state IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    occurred_at INTEGER NOT NULL,
    actor_role TEXT NOT NULL CHECK (actor_role IN (
        'SYSTEM', 'OPERATOR', 'QUALITY', 'MAINTENANCE', 'PLANNER', 'OT_IT'
    )),
    PRIMARY KEY (alarm_id, event_seq)
);

CREATE TABLE inspection (
    inspection_id TEXT PRIMARY KEY NOT NULL,
    alarm_id TEXT NOT NULL REFERENCES alarm(alarm_id),
    inspected_at INTEGER NOT NULL,
    inspector_role TEXT NOT NULL CHECK (inspector_role IN (
        'QUALITY', 'MAINTENANCE', 'OT_IT'
    )),
    finding TEXT NOT NULL CHECK (finding IN (
        'RISK_CONFIRMED', 'TELEMETRY_UNVERIFIED', 'NO_PRODUCT_IMPACT'
    )),
    notes TEXT NOT NULL CHECK (length(trim(notes)) > 0),
    UNIQUE (inspection_id, alarm_id)
);

CREATE TABLE remediation (
    remediation_id TEXT PRIMARY KEY NOT NULL,
    inspection_id TEXT NOT NULL,
    alarm_id TEXT NOT NULL,
    lot_id TEXT,
    action_type TEXT NOT NULL CHECK (action_type IN (
        'QUARANTINE_LOT', 'RELEASE_LOT', 'MOVE_LOT',
        'REPAIR_SENSOR', 'CHECK_DOOR', 'ADJUST_ENVIRONMENT'
    )),
    performed_at INTEGER NOT NULL,
    notes TEXT NOT NULL CHECK (length(trim(notes)) > 0),
    FOREIGN KEY (inspection_id, alarm_id)
        REFERENCES inspection(inspection_id, alarm_id),
    FOREIGN KEY (alarm_id, lot_id) REFERENCES alarm_lot(alarm_id, lot_id),
    CHECK (
        (action_type IN ('QUARANTINE_LOT', 'RELEASE_LOT', 'MOVE_LOT')
            AND lot_id IS NOT NULL)
        OR
        (action_type IN ('REPAIR_SENSOR', 'CHECK_DOOR', 'ADJUST_ENVIRONMENT')
            AND lot_id IS NULL)
    )
);

CREATE INDEX idx_lot_storage_zone_time
    ON lot_storage (zone_id, entered_at, exited_at);