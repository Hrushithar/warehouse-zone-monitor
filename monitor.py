import argparse
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from generate import ROOT, START_TS

RULE_VERSION = "v1"
MIN_BREACH_SAMPLES = 3
DRIFT_LIMIT_C = 1.5


def consecutive_runs(points, cadence):
    """Yield (start, exclusive end, count) for consecutive timestamps."""
    start = previous = None
    count = 0
    for timestamp in points:
        if previous is not None and timestamp != previous + cadence:
            yield start, previous + cadence, count
            count = 0
        if count == 0:
            start = timestamp
        previous = timestamp
        count += 1
    if count:
        yield start, previous + cadence, count


def active_zones(assignments, timestamp):
    return [
        zone_id
        for zone_id, start, end, _ in assignments
        if start <= timestamp and (end is None or timestamp < end)
    ]


def detect(db):
    alarms, diagnostics = [], []
    assignments = defaultdict(list)
    reference_readings = {}

    def add_alarm(kind, zone, start, end, detected, reason, source):
        alarms.append((kind, zone, start, end, detected, reason, source))

    def diagnostic(kind, zone, source, start, end):
        diagnostics.append({
            "decision": kind,
            "zone_id": zone,
            "source_id": source,
            "started_at": start,
            "window_end_at": end,
        })

    for sensor, zone, start, end, rationale in db.execute("""
        SELECT sensor_id, zone_id, effective_from, effective_to, rationale
        FROM sensor_zone_assignment
        ORDER BY sensor_id, effective_from, assignment_id
    """):
        assignments[sensor].append((zone, start, end, rationale))

    limits = {}
    for zone, occupancy, door, t_min, t_max, rh_min, rh_max in db.execute("""
        SELECT z.zone_id, z.occupancy_limit, z.door_open_limit_seconds,
               r.temp_min_c, r.temp_max_c, r.humidity_min_pct, r.humidity_max_pct
        FROM zone AS z
        JOIN storage_requirement AS r ON r.required_zone_type = z.zone_type
        WHERE r.effective_from <= ?
          AND (r.effective_to IS NULL OR r.effective_to > ?)
    """, (START_TS, START_TS)):
        if zone in limits:
            raise ValueError(f"Multiple active threshold profiles for {zone}")
        limits[zone] = (occupancy, door, t_min, t_max, rh_min, rh_max)

    if len(limits) != db.execute("SELECT COUNT(*) FROM zone").fetchone()[0]:
        raise ValueError("A zone has no active threshold profile.")

    replay_end = db.execute("""
        SELECT MAX(r.observed_at + s.sample_interval_seconds)
        FROM sensor_reading AS r JOIN sensor AS s USING (sensor_id)
    """).fetchone()[0]
    if replay_end is None:
        raise ValueError("No sensor readings available for replay.")

    for sensor, channel, cadence in db.execute("""
        SELECT sensor_id, sensor_type, sample_interval_seconds
        FROM sensor ORDER BY sensor_id
    """):
        placement = assignments[sensor]
        breaches = defaultdict(list)
        in_range_temp = defaultdict(dict)
        previous = None

        for index, (zone, start, end, rationale) in enumerate(placement):
            for other_zone, other_start, other_end, _ in placement[:index]:
                overlap_start = max(start, other_start)
                overlap_end = min(
                    end if end is not None else replay_end,
                    other_end if other_end is not None else replay_end,
                )
                if zone != other_zone and overlap_start < overlap_end and rationale is None:
                    diagnostic("AMBIGUOUS_SENSOR_ZONE", zone, sensor, overlap_start, overlap_end)

        for timestamp, value in db.execute("""
            SELECT observed_at, value FROM sensor_reading
            WHERE sensor_id = ? ORDER BY observed_at
        """, (sensor,)):
            if previous is not None and timestamp > previous + cadence:
                missing_by_zone = defaultdict(list)
                for missing_at in range(previous + cadence, timestamp, cadence):
                    zones = active_zones(placement, missing_at)
                    if len(zones) == 1:
                        missing_by_zone[zones[0]].append(missing_at)
                for zone, points in missing_by_zone.items():
                    for start, end, count in consecutive_runs(points, cadence):
                        add_alarm(
                            "SENSOR_GAP", zone, start, end, timestamp,
                            f"{count} expected samples missing", sensor,
                        )
            previous = timestamp

            zones = active_zones(placement, timestamp)
            if len(zones) != 1:
                continue
            zone = zones[0]
            occupancy, _, t_min, t_max, rh_min, rh_max = limits[zone]
            if channel == "TEMP":
                if t_min <= value <= t_max:
                    in_range_temp[zone][timestamp] = value
                else:
                    breaches[zone, "TEMP_EXCURSION", f"Temperature outside {t_min}–{t_max} °C"].append(timestamp)
            elif channel == "HUMIDITY" and (value < rh_min or value > rh_max):
                breaches[zone, "HUMIDITY_EXCURSION", f"Humidity outside {rh_min}–{rh_max}% RH"].append(timestamp)
            elif channel == "OCCUPANCY" and value > occupancy:
                breaches[zone, "HIGH_OCCUPANCY", f"Occupancy above {occupancy} people"].append(timestamp)

        for (zone, kind, reason), points in breaches.items():
            for start, end, count in consecutive_runs(points, cadence):
                if count >= MIN_BREACH_SAMPLES:
                    detected = start + (MIN_BREACH_SAMPLES - 1) * cadence
                    add_alarm(kind, zone, start, end, detected, reason, sensor)

        # The first temperature probe in each generated zone is the reference.
        for zone, readings in in_range_temp.items():
            if zone not in reference_readings:
                reference_readings[zone] = readings
                continue
            reference = reference_readings[zone]
            divergent = [
                timestamp for timestamp, value in readings.items()
                if timestamp in reference and abs(value - reference[timestamp]) > DRIFT_LIMIT_C
            ]
            for start, end, count in consecutive_runs(divergent, cadence):
                if count >= MIN_BREACH_SAMPLES:
                    diagnostic("SENSOR_DRIFT", zone, sensor, start, end)

    open_doors = {}

    def check_door(zone, start, end, event_id):
        limit = limits[zone][1]
        if end - start > limit:
            add_alarm(
                "DOOR_OPEN_TOO_LONG", zone, start, end, start + limit + 1,
                f"Door open for {end - start} seconds, limit {limit}", event_id,
            )

    for event_id, zone, door, timestamp, event_type in db.execute("""
        SELECT door_event_id, zone_id, door_code, occurred_at, event_type
        FROM door_event ORDER BY zone_id, door_code, occurred_at
    """):
        key = (zone, door)
        if event_type == "OPEN":
            if key in open_doors:
                diagnostic("INVALID_DOOR_SEQUENCE", zone, event_id, timestamp, timestamp + 1)
            else:
                open_doors[key] = (timestamp, event_id)
        elif key in open_doors:
            start, open_id = open_doors.pop(key)
            check_door(zone, start, timestamp, open_id)
        else:
            diagnostic("INVALID_DOOR_SEQUENCE", zone, event_id, timestamp, timestamp + 1)

    for (zone, _), (start, event_id) in open_doors.items():
        check_door(zone, start, replay_end, event_id)

    for storage, zone, start, end in db.execute("""
        SELECT s.storage_id, s.zone_id, s.entered_at, s.exited_at
        FROM lot_storage AS s
        JOIN inventory_lot AS l USING (lot_id)
        JOIN storage_requirement AS r USING (requirement_id)
        JOIN zone AS z USING (zone_id)
        WHERE z.zone_type <> r.required_zone_type
        ORDER BY s.storage_id
    """):
        window_end = min(end if end is not None else replay_end, replay_end)
        if start < window_end:
            add_alarm(
                "INCOMPATIBLE_STORAGE", zone, start, window_end, start,
                "Zone type differs from the lot requirement", storage,
            )

    return sorted(alarms), diagnostics


def persist(db, alarms):
    for index, (kind, zone, start, end, detected, reason, source) in enumerate(alarms, 1):
        alarm_id = f"ALM-{index:06d}"
        sensor_id = door_id = storage_id = None
        impact = "POTENTIAL_EXPOSURE"

        if kind == "DOOR_OPEN_TOO_LONG":
            door_id = source
        elif kind == "INCOMPATIBLE_STORAGE":
            storage_id = source
            impact = "PLACEMENT_VIOLATION"
        else:
            sensor_id = source
            if kind == "SENSOR_GAP":
                impact = "TELEMETRY_UNVERIFIED"

        db.execute("""
            INSERT INTO alarm VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alarm_id, zone, kind, start, end, detected, RULE_VERSION,
            reason, sensor_id, door_id, storage_id,
        ))
        db.execute(
            "INSERT INTO alarm_state_event VALUES (?, 1, 'OPEN', ?, 'SYSTEM')",
            (alarm_id, detected),
        )
        db.execute("""
            INSERT INTO alarm_lot (alarm_id, lot_id, overlap_seconds, impact_type)
            SELECT :alarm_id, lot_id,
                   SUM(MIN(COALESCE(exited_at, :end), :end) - MAX(entered_at, :start)),
                   :impact
            FROM lot_storage
            WHERE zone_id = :zone AND entered_at < :end
              AND (exited_at IS NULL OR exited_at > :start)
              AND (:storage_id IS NULL OR storage_id = :storage_id)
            GROUP BY lot_id
        """, {
            "alarm_id": alarm_id, "zone": zone, "start": start, "end": end,
            "impact": impact, "storage_id": storage_id,
        })


def main():
    parser = argparse.ArgumentParser(description="Detect warehouse risks from a scenario database.")
    parser.add_argument("--profile", choices=("starter", "full"), default="starter")
    args = parser.parse_args()
    output = ROOT / "generated"
    source = output / f"{args.profile}_scenario.sqlite"
    destination = output / f"{args.profile}_monitored.sqlite"
    diagnostic_path = output / f"{args.profile}_diagnostics.json"

    if not source.is_file():
        parser.error(f"Scenario database missing: {source}")
    if destination.exists() or diagnostic_path.exists():
        parser.error("Monitoring outputs already exist. Rename or remove them before rerunning.")

    try:
        with closing(sqlite3.connect(source)) as scenario:
            with closing(sqlite3.connect(destination)) as db:
                scenario.backup(db)
                db.execute("PRAGMA foreign_keys = ON")
                with db:
                    alarms, diagnostics = detect(db)
                    persist(db, alarms)
                    if db.execute("PRAGMA foreign_key_check").fetchone():
                        raise ValueError("Monitoring produced a broken foreign key.")
                link_count = db.execute("SELECT COUNT(*) FROM alarm_lot").fetchone()[0]
        diagnostic_path.write_text(json.dumps(diagnostics, indent=2) + "\n", encoding="utf-8")
    except Exception:
        destination.unlink(missing_ok=True)
        diagnostic_path.unlink(missing_ok=True)
        raise

    print(f"Alarms: {len(alarms)}")
    print(f"Affected-lot links: {link_count}")
    print(f"Diagnostics: {len(diagnostics)}")
    print(f"Database: {destination}")


if __name__ == "__main__":
    main()