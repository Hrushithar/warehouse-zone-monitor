"""Describe deterministic output faults using keys from a validated replay."""


def build_negative_fixtures(db, truth):
    gap = next(case for case in truth if case["expected_decision"] == "SENSOR_GAP")
    gap_alarm, = db.execute("""
        SELECT alarm_id FROM alarm
        WHERE alarm_type = ? AND zone_id = ? AND sensor_id = ?
          AND started_at = ? AND window_end_at = ?
    """, (gap["expected_decision"], gap["zone_id"], gap["source_id"],
          gap["started_at"], gap["window_end_at"])).fetchone()
    alarm_id, lot_id, seconds, impact = db.execute("""
        SELECT al.alarm_id, al.lot_id, al.overlap_seconds, al.impact_type
        FROM alarm_lot AS al JOIN alarm AS a USING (alarm_id)
        WHERE a.alarm_type = 'INCOMPATIBLE_STORAGE'
        ORDER BY al.alarm_id, al.lot_id LIMIT 1
    """).fetchone()
    return [
        {
            "issue_type": "READING_GAP_TREATED_AS_NORMAL",
            "stage": "alarm detection",
            "mutation": {"omit_alarm_id": gap_alarm},
            "ground_truth": gap,
            "expected_evaluation": {"missed_decisions": 1},
        },
        {
            "issue_type": "ALARM_MISSING_AFFECTED_LOT",
            "stage": "affected-lot linkage",
            "mutation": {"omit_alarm_lot": [alarm_id, lot_id]},
            "ground_truth": {
                "alarm_id": alarm_id,
                "lot_id": lot_id,
                "overlap_seconds": seconds,
                "impact_type": impact,
            },
            "expected_evaluation": {"missing_lot_links": 1},
        },
    ]
