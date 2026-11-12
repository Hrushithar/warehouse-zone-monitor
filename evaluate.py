import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing

from generate import ROOT


def signature(row, decision_field):
    return (
        row[decision_field], row["zone_id"], row["source_id"],
        row["started_at"], row["window_end_at"],
    )


def score_decisions(db, truth, diagnostics):
    expected = Counter(signature(row, "expected_decision") for row in truth)
    observed = Counter(tuple(row) for row in db.execute("""
        SELECT alarm_type, zone_id, COALESCE(sensor_id, door_event_id, storage_id),
               started_at, window_end_at
        FROM alarm
    """))
    observed.update(signature(row, "decision") for row in diagnostics)

    tp = sum((expected & observed).values())
    fp = sum((observed - expected).values())
    fn = sum((expected - observed).values())
    return {
        "matched": tp,
        "unexpected": fp,
        "missed": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "missing_decisions": list((expected - observed).elements()),
        "unexpected_decisions": list((observed - expected).elements()),
    }


def score_lot_links(db):
    dwell_by_zone = defaultdict(list)
    for storage_id, lot_id, zone, entered, exited in db.execute("""
        SELECT storage_id, lot_id, zone_id, entered_at, exited_at FROM lot_storage
    """):
        dwell_by_zone[zone].append((storage_id, lot_id, entered, exited))

    expected_seconds = defaultdict(int)
    for alarm_id, kind, zone, start, end, source_storage in db.execute("""
        SELECT alarm_id, alarm_type, zone_id, started_at, window_end_at, storage_id
        FROM alarm
    """):
        impact = {
            "SENSOR_GAP": "TELEMETRY_UNVERIFIED",
            "INCOMPATIBLE_STORAGE": "PLACEMENT_VIOLATION",
        }.get(kind, "POTENTIAL_EXPOSURE")

        for storage_id, lot_id, entered, exited in dwell_by_zone[zone]:
            if source_storage is not None and storage_id != source_storage:
                continue
            overlap_start = max(start, entered)
            overlap_end = end if exited is None else min(end, exited)
            if overlap_end > overlap_start:
                expected_seconds[alarm_id, lot_id, impact] += overlap_end - overlap_start

    expected = {
        (alarm_id, lot_id, seconds, impact)
        for (alarm_id, lot_id, impact), seconds in expected_seconds.items()
    }
    observed = {
        tuple(row) for row in db.execute("""
            SELECT alarm_id, lot_id, overlap_seconds, impact_type FROM alarm_lot
        """)
    }
    return {
        "expected": len(expected),
        "matched": len(expected & observed),
        "missing": sorted(expected - observed),
        "unexpected": sorted(observed - expected),
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate warehouse replay outputs.")
    parser.add_argument("--profile", choices=("starter", "full"), default="starter")
    args = parser.parse_args()

    output = ROOT / "generated"
    database = output / f"{args.profile}_monitored.sqlite"
    if not database.is_file():
        parser.error(f"Run monitor.py first. Missing: {database}")

    truth = json.loads((output / f"{args.profile}_ground_truth.json").read_text())
    diagnostics = json.loads((output / f"{args.profile}_diagnostics.json").read_text())

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        decisions = score_decisions(db, truth, diagnostics)
        links = score_lot_links(db)

    passed = (
        decisions["missed"] == 0
        and decisions["unexpected"] == 0
        and not links["missing"]
        and not links["unexpected"]
    )
    report = {"passed": passed, "decisions": decisions, "lot_links": links}
    rendered = json.dumps(report, indent=2)
    (output / f"{args.profile}_evaluation.json").write_text(rendered + "\n")
    print(rendered)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()