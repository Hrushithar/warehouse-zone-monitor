import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing

from generate import PROFILES, ROOT, START_TS

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

    evaluation = json.loads(
        (generated / f"{args.profile}_evaluation.json").read_text(encoding="utf-8")
    )

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        report = validate(db, args.profile, evaluation)
        if not report["passed"]:
            print(json.dumps(report, indent=2))
            raise SystemExit("Validation failed.")

        with tempfile.TemporaryDirectory(dir=generated) as work:
            from pathlib import Path

            staged = Path(work) / "export"
            staged.mkdir()
            export_tables(db, staged)
            write_json(staged / "validation_summary.json", report)
            for name in ("ground_truth", "diagnostics"):
                shutil.copyfile(
                    generated / f"{args.profile}_{name}.json",
                    staged / f"{name}.json",
                )
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