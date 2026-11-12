import argparse
import json
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from generate import CADENCE, ROOT, START_TS, scenario_warehouses

COLD = "ZONE-WH-001-001"
AMBIENT = "ZONE-WH-001-002"
HOT = "ZONE-WH-001-003"


def sample_time(number):
    return START_TS + number * CADENCE


def set_readings(db, sensor_id, first_sample, values):
    for offset, value in enumerate(values):
        result = db.execute(
            "UPDATE sensor_reading SET value = ? WHERE sensor_id = ? AND observed_at = ?",
            (value, sensor_id, sample_time(first_sample + offset)),
        )
        if result.rowcount != 1:
            raise ValueError(f"Expected one reading for {sensor_id} at sample {first_sample + offset}")


def reading_reference(reading_id, observed_at):
    return {
        "reading_id": reading_id,
        "observed_at": observed_at,
        "observed_at_utc": datetime.fromtimestamp(
            observed_at, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def choose_one(db, rng, query, parameters=()):
    candidates = db.execute(query, parameters).fetchall()
    if not candidates:
        raise ValueError("No eligible record for a full-profile scenario")
    return rng.choice(candidates)


def select_full_scenarios(db, seed):
    rng = random.Random(seed ^ 0xC0DE)
    warehouses = [row[0] for row in db.execute(
        "SELECT warehouse_id FROM warehouse ORDER BY warehouse_id"
    )]
    plan = scenario_warehouses("full", seed, warehouses)
    transfer_at = sample_time(25)

    temp_zone, transfer_lot = choose_one(db, rng, """
        SELECT first.zone_id, first.lot_id
        FROM lot_storage AS first
        JOIN zone AS z ON z.zone_id = first.zone_id
        WHERE z.warehouse_id = ? AND first.exited_at = ?
          AND EXISTS (
              SELECT 1 FROM lot_storage AS next
              WHERE next.lot_id = first.lot_id
                AND next.entered_at = first.exited_at
          )
        ORDER BY first.storage_id
    """, (plan["TEMP_EXCURSION"], transfer_at))
    temp_sensor, = choose_one(db, rng, """
        SELECT s.sensor_id
        FROM sensor AS s JOIN sensor_zone_assignment AS a USING (sensor_id)
        WHERE a.zone_id = ? AND s.sensor_type = 'TEMP'
        ORDER BY s.sensor_id
    """, (temp_zone,))

    humidity_zone, humidity_sensor = choose_one(db, rng, """
        SELECT z.zone_id, s.sensor_id
        FROM zone AS z
        JOIN sensor_zone_assignment AS a USING (zone_id)
        JOIN sensor AS s USING (sensor_id)
        WHERE z.warehouse_id = ? AND s.sensor_type = 'HUMIDITY'
        ORDER BY z.zone_id, s.sensor_id
    """, (plan["HUMIDITY_EXCURSION"],))
    occupancy_zone, occupancy_sensor = choose_one(db, rng, """
        SELECT z.zone_id, s.sensor_id
        FROM zone AS z
        JOIN sensor_zone_assignment AS a USING (zone_id)
        JOIN sensor AS s USING (sensor_id)
        WHERE z.warehouse_id = ? AND s.sensor_type = 'OCCUPANCY'
        ORDER BY z.zone_id, s.sensor_id
    """, (plan["HIGH_OCCUPANCY"],))
    door_zone, door_event = choose_one(db, rng, """
        SELECT z.zone_id, d.door_event_id
        FROM zone AS z JOIN door_event AS d USING (zone_id)
        WHERE z.warehouse_id = ? AND d.event_type = 'OPEN' AND d.occurred_at = ?
        ORDER BY z.zone_id, d.door_event_id
    """, (plan["DOOR_OPEN_TOO_LONG"], sample_time(12)))
    gap_zone, gap_sensor = choose_one(db, rng, """
        SELECT DISTINCT z.zone_id, sensor.sensor_id
        FROM zone AS z
        JOIN lot_storage AS dwell USING (zone_id)
        JOIN sensor_zone_assignment AS a USING (zone_id)
        JOIN sensor USING (sensor_id)
        WHERE z.warehouse_id = ? AND dwell.exited_at IS NULL
          AND sensor.sensor_type = 'HUMIDITY'
        ORDER BY z.zone_id, sensor.sensor_id
    """, (plan["SENSOR_GAP"],))
    drift_zone, drift_sensor = choose_one(db, rng, """
        SELECT z.zone_id, s.sensor_id
        FROM zone AS z
        JOIN sensor_zone_assignment AS a USING (zone_id)
        JOIN sensor AS s USING (sensor_id)
        WHERE z.warehouse_id = ? AND s.sensor_type = 'TEMP'
          AND s.sensor_id LIKE '%-TEMP-02'
        ORDER BY z.zone_id, s.sensor_id
    """, (plan["SENSOR_DRIFT"],))
    reference_sensor = drift_sensor.removesuffix("-02") + "-01"

    storage_id, incompatible_source_zone = choose_one(db, rng, """
        SELECT s.storage_id, s.zone_id
        FROM lot_storage AS s
        JOIN zone AS z USING (zone_id)
        JOIN inventory_lot AS l USING (lot_id)
        JOIN storage_requirement AS r USING (requirement_id)
        WHERE z.warehouse_id = ? AND z.zone_type = r.required_zone_type
          AND s.exited_at IS NOT NULL
          AND (SELECT COUNT(*) FROM lot_storage AS all_dwell
               WHERE all_dwell.lot_id = s.lot_id) = 1
        ORDER BY s.storage_id
    """, (plan["INCOMPATIBLE_STORAGE"],))
    incompatible_zone, = choose_one(db, rng, """
        SELECT candidate.zone_id
        FROM zone AS candidate JOIN zone AS source ON source.zone_id = ?
        WHERE candidate.warehouse_id = ?
          AND candidate.zone_type <> source.zone_type
        ORDER BY candidate.zone_id
    """, (incompatible_source_zone, plan["INCOMPATIBLE_STORAGE"]))

    ambiguous_sensor, ambiguous_source_zone = choose_one(db, rng, """
        SELECT s.sensor_id, a.zone_id
        FROM sensor AS s JOIN sensor_zone_assignment AS a USING (sensor_id)
        JOIN zone AS z USING (zone_id)
        WHERE z.warehouse_id = ?
        ORDER BY s.sensor_id
    """, (plan["AMBIGUOUS_SENSOR_ZONE"],))
    ambiguous_zone, = choose_one(db, rng, """
        SELECT zone_id FROM zone
        WHERE warehouse_id = ? AND zone_id <> ?
        ORDER BY zone_id
    """, (plan["AMBIGUOUS_SENSOR_ZONE"], ambiguous_source_zone))

    return {
        "temperature": (temp_zone, temp_sensor, transfer_lot),
        "humidity": (humidity_zone, humidity_sensor),
        "occupancy": (occupancy_zone, occupancy_sensor),
        "door": (door_zone, door_event),
        "gap": (gap_zone, gap_sensor),
        "drift": (drift_zone, drift_sensor, reference_sensor),
        "incompatible": (storage_id, incompatible_zone),
        "ambiguous": (ambiguous_sensor, ambiguous_zone),
    }


def inject(db, provenance_events=None, profile="starter", seed=20261022):
    truth = []

    def label(issue_type, expected_decision, zone_id, source_id, start, end):
        truth.append({
            "case_id": f"CASE-{len(truth) + 1:03d}",
            "issue_type": issue_type,
            "expected_decision": expected_decision,
            "zone_id": zone_id,
            "source_id": source_id,
            "started_at": start,
            "window_end_at": end,
        })

    if profile == "starter":
        scenario = {
            "temperature": (COLD, f"SNS-{COLD}-TEMP-01", None),
            "humidity": (AMBIENT, f"SNS-{AMBIENT}-HUMIDITY-01"),
            "occupancy": (HOT, f"SNS-{HOT}-OCCUPANCY-01"),
            "door": (COLD, None),
            "gap": (COLD, f"SNS-{COLD}-HUMIDITY-01"),
            "drift": (
                AMBIENT, f"SNS-{AMBIENT}-TEMP-02", f"SNS-{AMBIENT}-TEMP-01"
            ),
            "incompatible": (None, AMBIENT),
            "ambiguous": (f"SNS-{COLD}-HUMIDITY-02", HOT),
        }
        signal_cases = (
            (*scenario["temperature"][:2], 11, "TEMP_EXCURSION"),
            (*scenario["humidity"], 72, "HUMIDITY_EXCURSION"),
            (*scenario["occupancy"], 12, "HIGH_OCCUPANCY"),
        )
    else:
        scenario = select_full_scenarios(db, seed)
        temp_zone, temp_sensor, _ = scenario["temperature"]
        humidity_zone, humidity_sensor = scenario["humidity"]
        occupancy_zone, occupancy_sensor = scenario["occupancy"]
        temp_max, = db.execute("""
            SELECT r.temp_max_c FROM zone AS z
            JOIN storage_requirement AS r
              ON r.required_zone_type = z.zone_type
            WHERE z.zone_id = ? AND r.effective_from <= ?
              AND (r.effective_to IS NULL OR r.effective_to > ?)
        """, (temp_zone, START_TS, START_TS)).fetchone()
        humidity_max, = db.execute("""
            SELECT r.humidity_max_pct FROM zone AS z
            JOIN storage_requirement AS r
              ON r.required_zone_type = z.zone_type
            WHERE z.zone_id = ? AND r.effective_from <= ?
              AND (r.effective_to IS NULL OR r.effective_to > ?)
        """, (humidity_zone, START_TS, START_TS)).fetchone()
        occupancy_limit, = db.execute(
            "SELECT occupancy_limit FROM zone WHERE zone_id = ?",
            (occupancy_zone,),
        ).fetchone()
        signal_cases = (
            (temp_zone, temp_sensor, temp_max + 3, "TEMP_EXCURSION"),
            (humidity_zone, humidity_sensor, humidity_max + 12, "HUMIDITY_EXCURSION"),
            (occupancy_zone, occupancy_sensor, occupancy_limit + 4, "HIGH_OCCUPANCY"),
        )

    for zone_id, sensor_id, value, decision in signal_cases:
        set_readings(db, sensor_id, 24, [value] * 3)
        label(decision, decision, zone_id, sensor_id, sample_time(24), sample_time(27))

    opened_at = sample_time(12)
    door_zone, selected_open_id = scenario["door"]
    open_id = selected_open_id or db.execute("""
        SELECT door_event_id FROM door_event
        WHERE zone_id = ? AND door_code = 'DOOR-01'
          AND occurred_at = ? AND event_type = 'OPEN'
    """, (door_zone, opened_at)).fetchone()[0]
    closed_at = opened_at + 240
    db.execute(
        """
        UPDATE door_event SET occurred_at = ?
        WHERE zone_id = ? AND door_code = 'DOOR-01'
          AND event_type = 'CLOSE' AND occurred_at > ? AND occurred_at < ?
        """,
        (closed_at, door_zone, opened_at, opened_at + CADENCE),
    )
    label(
        "LONG_DOOR_OPEN", "DOOR_OPEN_TOO_LONG",
        door_zone, open_id, opened_at, closed_at,
    )

    gap_zone, gap_sensor = scenario["gap"]
    gap_start, gap_end = sample_time(30), sample_time(33)
    reading_count_before = db.execute("SELECT COUNT(*) FROM sensor_reading").fetchone()[0]
    omitted = db.execute(
        """
        SELECT reading_id, observed_at FROM sensor_reading
        WHERE sensor_id = ? AND observed_at >= ? AND observed_at < ?
        ORDER BY observed_at
        """,
        (gap_sensor, gap_start, gap_end),
    ).fetchall()
    last_time, last_value = db.execute(
        """
        SELECT observed_at, value FROM sensor_reading
        WHERE sensor_id = ? ORDER BY observed_at DESC LIMIT 1
        """,
        (gap_sensor,),
    ).fetchone()
    removed = db.execute(
        """
        DELETE FROM sensor_reading
        WHERE sensor_id = ? AND observed_at >= ? AND observed_at < ?
        """,
        (gap_sensor, gap_start, gap_end),
    ).rowcount
    assert removed == 3, f"Expected three missing readings, got {removed}"

    # Extend this sensor's observation period to preserve the profile row count.
    appended = []
    for offset in range(1, removed + 1):
        observed_at = last_time + offset * CADENCE
        stamp = datetime.fromtimestamp(observed_at, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        reading_id = f"RDG-{gap_sensor}-{stamp}"
        db.execute(
            "INSERT INTO sensor_reading VALUES (?, ?, ?, ?)",
            (reading_id, gap_sensor, observed_at, last_value),
        )
        appended.append(reading_reference(reading_id, observed_at))
    reading_count_after = db.execute("SELECT COUNT(*) FROM sensor_reading").fetchone()[0]
    assert reading_count_after == reading_count_before
    if provenance_events is not None:
        provenance_events.append({
            "operation": "CREATE_INTERNAL_SENSOR_GAP",
            "sensor_id": gap_sensor,
            "gap_started_at": gap_start,
            "gap_window_end_at": gap_end,
            "omitted_readings": [reading_reference(*row) for row in omitted],
            "appended_readings": appended,
            "reason": (
                "Append the same number of readings after the baseline observation "
                "period so the scenario retains the exact profile sensor_reading count."
            ),
            "sensor_reading_count_before": reading_count_before,
            "sensor_reading_count_after": reading_count_after,
        })
    label("SENSOR_OUTAGE", "SENSOR_GAP", gap_zone, gap_sensor, gap_start, gap_end)

    drift_zone, drift_sensor, reference_sensor = scenario["drift"]
    for sample, bias in enumerate((0.5, 1.0, 1.8, 2.0, 2.2), start=34):
        reference = db.execute(
            "SELECT value FROM sensor_reading WHERE sensor_id = ? AND observed_at = ?",
            (reference_sensor, sample_time(sample)),
        ).fetchone()[0]
        set_readings(db, drift_sensor, sample, [round(reference + bias, 2)])
    # The diagnostic begins when the peer difference first exceeds 1.5 °C.
    label("SENSOR_DRIFT", "SENSOR_DRIFT", drift_zone, drift_sensor,
          sample_time(36), sample_time(39))

    storage_id, incompatible_zone = scenario["incompatible"]
    if storage_id is None:
        storage_id, entered_at, exited_at = db.execute("""
            SELECT storage_id, entered_at, exited_at
            FROM lot_storage WHERE zone_id = ? ORDER BY storage_id LIMIT 1
        """, (COLD,)).fetchone()
    else:
        entered_at, exited_at = db.execute("""
            SELECT entered_at, exited_at FROM lot_storage WHERE storage_id = ?
        """, (storage_id,)).fetchone()
    db.execute(
        "UPDATE lot_storage SET zone_id = ? WHERE storage_id = ?",
        (incompatible_zone, storage_id),
    )
    label("INCOMPATIBLE_LOT_ZONE", "INCOMPATIBLE_STORAGE", incompatible_zone,
          storage_id, entered_at, exited_at)

    ambiguous_sensor, ambiguous_zone = scenario["ambiguous"]
    db.execute(
        "INSERT INTO sensor_zone_assignment VALUES (?, ?, ?, ?, ?, ?)",
        (
            "ASG-FIXTURE-001", ambiguous_sensor, ambiguous_zone,
            sample_time(40), sample_time(44), None,
        ),
    )
    label("CONFLICTING_SENSOR_ASSIGNMENT", "AMBIGUOUS_SENSOR_ZONE", ambiguous_zone,
          ambiguous_sensor, sample_time(40), sample_time(44))

    return truth


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("starter", "full"), default="starter")
    parser.add_argument("--seed", type=int, default=20261022)
    args = parser.parse_args()

    output = ROOT / "generated"
    source = output / f"{args.profile}.sqlite"
    scenario = output / f"{args.profile}_scenario.sqlite"
    truth_path = output / f"{args.profile}_ground_truth.json"
    provenance_path = output / f"{args.profile}_generation_provenance.json"

    if not source.is_file():
        parser.error(f"Generate the {args.profile} baseline first.")
    if scenario.exists() or truth_path.exists() or provenance_path.exists():
        parser.error("Scenario outputs already exist. Rename or remove them before rerunning.")

    try:
        provenance = {
            "artifact_version": 1,
            "profile": args.profile,
            "purpose": "Record deterministic scenario-generation mutations; not detector input or incident ground truth.",
            "events": [],
        }
        with closing(sqlite3.connect(source)) as baseline:
            with closing(sqlite3.connect(scenario)) as db:
                baseline.backup(db)
                db.execute("PRAGMA foreign_keys = ON")
                with db:
                    truth = inject(
                        db, provenance["events"], profile=args.profile, seed=args.seed
                    )
                    if db.execute("PRAGMA foreign_key_check").fetchone():
                        raise ValueError("Scenario contains a broken foreign key.")
        truth_path.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
        provenance_path.write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except Exception:
        scenario.unlink(missing_ok=True)
        truth_path.unlink(missing_ok=True)
        provenance_path.unlink(missing_ok=True)
        raise

    print(f"Scenario database: {scenario}")
    print(f"Ground truth: {truth_path}")
    print(f"Generation provenance: {provenance_path}")
    print(f"Labelled scenarios: {len(truth)}")


if __name__ == "__main__":
    main()
