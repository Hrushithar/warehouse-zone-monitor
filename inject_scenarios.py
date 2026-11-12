import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from generate import CADENCE, ROOT, START_TS

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


def inject(db, provenance_events=None):
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

    for zone_id, channel, value, decision in (
        (COLD, "TEMP", 11, "TEMP_EXCURSION"),
        (AMBIENT, "HUMIDITY", 72, "HUMIDITY_EXCURSION"),
        (HOT, "OCCUPANCY", 12, "HIGH_OCCUPANCY"),
    ):
        sensor_id = f"SNS-{zone_id}-{channel}-01"
        set_readings(db, sensor_id, 24, [value] * 3)
        label(decision, decision, zone_id, sensor_id, sample_time(24), sample_time(27))

    opened_at = sample_time(12)
    open_id = db.execute(
        """
        SELECT door_event_id FROM door_event
        WHERE zone_id = ? AND door_code = 'DOOR-01'
          AND occurred_at = ? AND event_type = 'OPEN'
        """,
        (COLD, opened_at),
    ).fetchone()[0]
    closed_at = opened_at + 240
    db.execute(
        """
        UPDATE door_event SET occurred_at = ?
        WHERE zone_id = ? AND door_code = 'DOOR-01'
          AND event_type = 'CLOSE' AND occurred_at > ? AND occurred_at < ?
        """,
        (closed_at, COLD, opened_at, opened_at + CADENCE),
    )
    label("LONG_DOOR_OPEN", "DOOR_OPEN_TOO_LONG", COLD, open_id, opened_at, closed_at)

    gap_sensor = f"SNS-{COLD}-HUMIDITY-01"
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
    label("SENSOR_OUTAGE", "SENSOR_GAP", COLD, gap_sensor, gap_start, gap_end)

    drift_sensor = f"SNS-{AMBIENT}-TEMP-02"
    reference_sensor = f"SNS-{AMBIENT}-TEMP-01"
    for sample, bias in enumerate((0.5, 1.0, 1.8, 2.0, 2.2), start=34):
        reference = db.execute(
            "SELECT value FROM sensor_reading WHERE sensor_id = ? AND observed_at = ?",
            (reference_sensor, sample_time(sample)),
        ).fetchone()[0]
        set_readings(db, drift_sensor, sample, [round(reference + bias, 2)])
    # The diagnostic begins when the peer difference first exceeds 1.5 °C.
    label("SENSOR_DRIFT", "SENSOR_DRIFT", AMBIENT, drift_sensor,
          sample_time(36), sample_time(39))

    storage_id, entered_at, exited_at = db.execute(
        """
        SELECT storage_id, entered_at, exited_at
        FROM lot_storage WHERE zone_id = ? ORDER BY storage_id LIMIT 1
        """,
        (COLD,),
    ).fetchone()
    db.execute("UPDATE lot_storage SET zone_id = ? WHERE storage_id = ?", (AMBIENT, storage_id))
    label("INCOMPATIBLE_LOT_ZONE", "INCOMPATIBLE_STORAGE", AMBIENT,
          storage_id, entered_at, exited_at)

    ambiguous_sensor = f"SNS-{COLD}-HUMIDITY-02"
    db.execute(
        "INSERT INTO sensor_zone_assignment VALUES (?, ?, ?, ?, ?, ?)",
        ("ASG-FIXTURE-001", ambiguous_sensor, HOT, sample_time(40), sample_time(44), None),
    )
    label("CONFLICTING_SENSOR_ASSIGNMENT", "AMBIGUOUS_SENSOR_ZONE", HOT,
          ambiguous_sensor, sample_time(40), sample_time(44))

    return truth


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("starter", "full"), default="starter")
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
                    truth = inject(db, provenance["events"])
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
