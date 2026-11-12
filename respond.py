import argparse
import sqlite3
from contextlib import closing

from generate import ROOT

LOT_ACTIONS = {"TEMP_EXCURSION", "HUMIDITY_EXCURSION", "INCOMPATIBLE_STORAGE"}


def record_responses(db):
    alarms = db.execute("""
        SELECT alarm_id, alarm_type, detected_at, window_end_at, sensor_id, zone_id
        FROM alarm ORDER BY alarm_id
    """).fetchall()

    if not alarms:
        raise ValueError("No alarms available. Run monitor.py first.")
    if db.execute("SELECT 1 FROM inspection LIMIT 1").fetchone():
        raise ValueError("Responses are already recorded in this database.")

    action_count = resolved_count = 0

    for alarm_id, kind, detected, window_end, sensor_id, zone_id in alarms:
        suffix = alarm_id.removeprefix("ALM-")
        inspection_id = f"INSP-{suffix}"
        lots = [
            lot_id for (lot_id,) in db.execute("""
                SELECT lot_id FROM alarm_lot
                WHERE alarm_id = ? ORDER BY lot_id
            """, (alarm_id,))
        ]

        acknowledged = detected + 60
        inspected = acknowledged + 60
        if kind not in LOT_ACTIONS:
            inspected = max(inspected, window_end)
        performed = inspected + 60

        if kind == "SENSOR_GAP":
            role = "MAINTENANCE"
            finding = "TELEMETRY_UNVERIFIED"
            notes = "Missing samples prevent verification of storage conditions."
            actions = [("REPAIR_SENSOR", None, "Simulated sensor maintenance after the outage.")]
        elif kind == "DOOR_OPEN_TOO_LONG":
            role = "MAINTENANCE"
            finding = "RISK_CONFIRMED" if lots else "NO_PRODUCT_IMPACT"
            notes = "Door duration exceeded the limit. Inventory impact uses dwell overlap."
            actions = [("CHECK_DOOR", None, "Simulated check of the door and closure mechanism.")]
        else:
            role = "QUALITY"
            finding = "RISK_CONFIRMED" if lots else "NO_PRODUCT_IMPACT"
            notes = f"Reviewed {kind} and identified {len(lots)} overlapping lots."
            actions = []
            if kind in LOT_ACTIONS:
                active_lots = db.execute("""
                    SELECT al.lot_id
                    FROM alarm_lot AS al
                    JOIN lot_storage AS s USING (lot_id)
                    WHERE al.alarm_id = ? AND s.zone_id = ?
                      AND s.entered_at <= ?
                      AND (s.exited_at IS NULL OR s.exited_at > ?)
                    ORDER BY al.lot_id
                """, (alarm_id, zone_id, performed, performed))
                actions = [
                    ("QUARANTINE_LOT", lot_id, "Simulated hold pending a quality disposition.")
                    for (lot_id,) in active_lots
                ]
                if len(actions) < len(lots):
                    notes += f" {len(lots) - len(actions)} lots require location follow-up."

        db.execute("""
            INSERT INTO alarm_state_event
            VALUES (?, 2, 'ACKNOWLEDGED', ?, 'OPERATOR')
        """, (alarm_id, acknowledged))
        db.execute("""
            INSERT INTO inspection VALUES (?, ?, ?, ?, ?, ?)
        """, (inspection_id, alarm_id, inspected, role, finding, notes))

        for action, lot_id, action_notes in actions:
            action_count += 1
            db.execute("""
                INSERT INTO remediation (
                    remediation_id, inspection_id, alarm_id, lot_id,
                    action_type, performed_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                f"REM-{action_count:06d}", inspection_id, alarm_id, lot_id,
                action, performed, action_notes,
            ))

        # Close the telemetry incident only when a recovery reading exists.
        # The affected lots retain their unverified exposure history.
        if kind == "SENSOR_GAP" and db.execute("""
            SELECT 1 FROM sensor_reading
            WHERE sensor_id = ? AND observed_at = ?
        """, (sensor_id, window_end)).fetchone():
            db.execute("""
                INSERT INTO alarm_state_event
                VALUES (?, 3, 'RESOLVED', ?, 'MAINTENANCE')
            """, (alarm_id, performed + 60))
            resolved_count += 1

    return len(alarms), action_count, resolved_count


def main():
    parser = argparse.ArgumentParser(description="Record simulated warehouse responses.")
    parser.add_argument("--profile", choices=("starter", "full"), default="starter")
    args = parser.parse_args()
    path = ROOT / "generated" / f"{args.profile}_monitored.sqlite"

    if not path.is_file():
        parser.error(f"Run monitor.py first. Missing: {path}")

    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            inspections, actions, resolved = record_responses(db)

    print(f"Inspections: {inspections}")
    print(f"Remediation actions: {actions}")
    print(f"Resolved telemetry alarms: {resolved}")


if __name__ == "__main__":
    main()