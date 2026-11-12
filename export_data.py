import argparse
import csv
import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from evaluate import evaluate

from generate import PROFILES, ROOT, START_TS
from negative_fixtures import build_negative_fixtures

TABLES = (
    "warehouse", "zone", "sensor", "storage_requirement",
    "sensor_zone_assignment", "sensor_reading", "door_event",
    "inventory_lot", "lot_storage", "alarm", "alarm_lot",
    "alarm_state_event", "inspection", "remediation",
)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sql_value(value):
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


def keys_are_unique(db, table):
    indexes = db.execute(f"PRAGMA index_list({table})").fetchall()
    for _, index, unique, *_ in indexes:
        if not unique:
            continue
        columns = [row[2] for row in db.execute(f"PRAGMA index_info({index})")]
        keys = ", ".join(columns)
        present = " AND ".join(f"{column} IS NOT NULL" for column in columns)
        if db.execute(
            f"SELECT 1 FROM {table} WHERE {present} "
            f"GROUP BY {keys} HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone():
            return False
    primary_key = [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[5]]
    if not primary_key:
        return False
    null_key = " OR ".join(f"{column} IS NULL" for column in primary_key)
    return db.execute(f"SELECT 1 FROM {table} WHERE {null_key} LIMIT 1").fetchone() is None


def validate(db, profile, evaluation):
    warehouses, zones_per_warehouse, samples, lots_per_zone = PROFILES[profile]
    zones = warehouses * zones_per_warehouse
    counts = {
        table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES
    }
    expected = {
        "warehouse": warehouses,
        "zone": zones,
        "sensor": zones * 5,
        "sensor_reading": zones * 5 * samples,
        "inventory_lot": zones * lots_per_zone,
    }

    checks = {
        "sqlite_integrity_and_constraints": (
            db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        ),
        "primary_keys_and_duplicates": all(keys_are_unique(db, table) for table in TABLES),
        "foreign_keys": db.execute("PRAGMA foreign_key_check").fetchone() is None,
        "profile_row_counts": all(counts[table] == total for table, total in expected.items()),
        "fixture_decisions_and_lot_links": evaluation["passed"],
    }

    violations = {
        "reading_values_and_cadence": """
            SELECT 1 FROM sensor_reading AS r JOIN sensor AS s USING (sensor_id)
            WHERE r.observed_at < :start
               OR (r.observed_at - :start) % s.sample_interval_seconds <> 0
               OR (s.sensor_type = 'HUMIDITY' AND r.value NOT BETWEEN 0 AND 100)
               OR (s.sensor_type = 'OCCUPANCY'
                   AND (r.value < 0 OR r.value <> CAST(r.value AS INTEGER)))
        """,
        "requirement_effective_at_receipt": """
            SELECT 1 FROM inventory_lot AS l JOIN storage_requirement AS r USING (requirement_id)
            WHERE l.received_at < r.effective_from OR l.received_at >= r.effective_to
        """,
        "dwell_date_order": """
            SELECT 1 FROM lot_storage AS s JOIN inventory_lot AS l USING (lot_id)
            WHERE s.entered_at < l.received_at
        """,
        "no_overlapping_lot_dwells": """
            SELECT 1 FROM lot_storage AS a JOIN lot_storage AS b
              ON a.lot_id = b.lot_id AND a.entered_at < b.entered_at
            WHERE a.exited_at IS NULL OR a.exited_at > b.entered_at
        """,
        "inspection_and_remediation_date_order": """
            SELECT 1 FROM inspection AS i JOIN alarm AS a USING (alarm_id)
            WHERE i.inspected_at < a.detected_at
            UNION ALL
            SELECT 1 FROM remediation AS r JOIN inspection AS i USING (inspection_id)
            WHERE r.performed_at < i.inspected_at
        """,
        "alarm_source_zone": """
            SELECT 1 FROM alarm AS a
            LEFT JOIN door_event AS d USING (door_event_id)
            LEFT JOIN lot_storage AS s USING (storage_id)
            WHERE (d.door_event_id IS NOT NULL AND
                   (d.zone_id <> a.zone_id OR d.event_type <> 'OPEN'
                    OR d.occurred_at <> a.started_at))
               OR (s.storage_id IS NOT NULL AND
                   (s.zone_id <> a.zone_id OR s.entered_at <> a.started_at))
               OR (a.sensor_id IS NOT NULL AND
                   (SELECT COUNT(*) FROM sensor_zone_assignment AS m
                    WHERE m.sensor_id = a.sensor_id
                      AND m.effective_from <= a.started_at
                      AND (m.effective_to IS NULL OR m.effective_to > a.started_at)) <> 1)
               OR (a.sensor_id IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM sensor_zone_assignment AS m
                   WHERE m.sensor_id = a.sensor_id AND m.zone_id = a.zone_id
                     AND m.effective_from <= a.started_at
                     AND (m.effective_to IS NULL OR m.effective_to > a.started_at)
               ))
        """,
        "lot_remediation_during_dwell": """
            SELECT 1 FROM remediation AS r JOIN alarm AS a USING (alarm_id)
            WHERE r.action_type = 'QUARANTINE_LOT' AND NOT EXISTS (
                SELECT 1 FROM lot_storage AS s
                WHERE s.lot_id = r.lot_id AND s.zone_id = a.zone_id
                  AND s.entered_at <= r.performed_at
                  AND (s.exited_at IS NULL OR s.exited_at > r.performed_at)
            )
        """,
        "alarm_lifecycle": """
            SELECT 1
            FROM alarm AS a
            LEFT JOIN alarm_state_event AS opened
              ON opened.alarm_id = a.alarm_id AND opened.event_seq = 1
            LEFT JOIN alarm_state_event AS ack
              ON ack.alarm_id = a.alarm_id AND ack.event_seq = 2
            LEFT JOIN alarm_state_event AS resolved
              ON resolved.alarm_id = a.alarm_id AND resolved.event_seq = 3
            WHERE opened.alarm_id IS NULL OR opened.state <> 'OPEN'
               OR opened.occurred_at <> a.detected_at
               OR ack.alarm_id IS NULL OR ack.state <> 'ACKNOWLEDGED'
               OR ack.occurred_at < opened.occurred_at
               OR (resolved.alarm_id IS NOT NULL
                   AND (resolved.state <> 'RESOLVED' OR resolved.occurred_at < ack.occurred_at))
            UNION ALL
            SELECT 1 FROM alarm_state_event WHERE event_seq > 3
        """,
    }
    for name, query in violations.items():
        checks[name] = db.execute(query + " LIMIT 1", {"start": START_TS}).fetchone() is None

    return {
        "passed": all(checks.values()),
        "checks": checks,
        "row_counts": counts,
        "evaluation": evaluation,
    }


def export_tables(db, destination):
    with (destination / "seed.sql").open("w", encoding="utf-8") as seed:
        seed.write((ROOT / "schema.sql").read_text(encoding="utf-8"))
        seed.write("\nBEGIN;\n")
        for table in TABLES:
            schema = db.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [row[1] for row in schema]
            key_columns = [row[1] for row in sorted(schema, key=lambda row: row[5]) if row[5]]
            rows = db.execute(f"SELECT * FROM {table} ORDER BY {', '.join(key_columns)}")

            with (destination / f"{table}.csv").open("w", encoding="utf-8", newline="") as csv_file:
                with (destination / f"{table}.json").open("w", encoding="utf-8") as json_file:
                    writer = csv.writer(csv_file)
                    writer.writerow(columns)
                    json_file.write("[")
                    separator = "\n"
                    for row in rows:
                        writer.writerow(row)
                        record = dict(zip(columns, row))
                        json_file.write(separator + json.dumps(record, allow_nan=False))
                        separator = ",\n"
                        values = ", ".join(sql_value(value) for value in row)
                        seed.write(f"INSERT INTO {table} VALUES ({values});\n")
                    json_file.write("\n]\n")
        seed.write("COMMIT;\n")


def main():
    parser = argparse.ArgumentParser(description="Validate and export a warehouse replay.")
    parser.add_argument("--profile", choices=PROFILES, default="starter")
    parser.add_argument("--seed", type=int, default=20261022, help="Seed used to generate the input.")
    args = parser.parse_args()

    generated = ROOT / "generated"
    database = generated / f"{args.profile}_monitored.sqlite"
    destination = generated / f"{args.profile}_export"
    if not database.is_file():
        parser.error(f"Monitored database missing: {database}")
    if destination.exists():
        parser.error(f"Export already exists: {destination}")

    truth = json.loads((generated / f"{args.profile}_ground_truth.json").read_text(encoding="utf-8"))
    diagnostics = json.loads((generated / f"{args.profile}_diagnostics.json").read_text(encoding="utf-8"))

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        report = validate(db, args.profile, evaluate(db, truth, diagnostics))
        if not report["passed"]:
            print(json.dumps(report, indent=2))
            raise SystemExit("Validation failed.")

        with tempfile.TemporaryDirectory(dir=generated) as work:
            staged = Path(work) / "export"
            staged.mkdir()
            export_tables(db, staged)
            write_json(staged / "validation_summary.json", report)
            write_json(staged / "ground_truth.json", truth)
            write_json(staged / "diagnostics.json", diagnostics)
            write_json(staged / "negative_fixtures.json", build_negative_fixtures(db, truth))
            write_json(staged / "source_manifest.json", {
                "profile": args.profile,
                "seed": args.seed,
                "source": "Synthetic warehouse data generated by this repository",
                "external_sources": [],
                "generator": f"python generate.py --profile {args.profile} --seed {args.seed}",
                "transformations": [
                    "Generate valid synthetic master and event records",
                    "Inject deterministic scenarios and write separate ground truth",
                    "Detect risks and derive lot overlap, inspections, and remediation",
                ],
                "row_counts": report["row_counts"],
                "sha256": {path.name: sha256(path) for path in sorted(staged.iterdir())},
            })
            staged.rename(destination)

    print(f"Validation passed. Exports written to {destination}")


if __name__ == "__main__":
    main()