import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evaluate import score_decisions, score_lot_links
from export_data import validation_report
from generate import ROOT, populate
from inject_scenarios import COLD, inject, set_readings
from monitor import detect, diagnostics_from_db, persist
from negative_fixtures import build_negative_fixtures
from respond import record_responses


class WarehousePipelineTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        populate(self.db, "starter", 20261022)

    def run_scenario(self):
        truth = inject(self.db)
        alarms, diagnostics = detect(self.db)
        persist(self.db, alarms, diagnostics)
        return truth, diagnostics

    def test_clean_baseline_has_no_alerts(self):
        alarms, diagnostics = detect(self.db)
        self.assertEqual(alarms, [])
        self.assertEqual(diagnostics, [])

    def test_temperature_limit_and_two_sample_spike(self):
        sensor = f"SNS-{COLD}-TEMP-01"
        # 8 °C is allowed. The two following breaches are below the duration rule.
        set_readings(self.db, sensor, 24, [8, 11, 11])
        alarms, _ = detect(self.db)
        self.assertEqual(alarms, [])

    def test_scenario_decisions_and_lot_links_match(self):
        truth, diagnostics = self.run_scenario()
        decisions = score_decisions(self.db, truth, diagnostics)
        links = score_lot_links(self.db)

        self.assertEqual(decisions["matched"], 8)
        self.assertEqual(decisions["missed"], 0)
        self.assertEqual(decisions["unexpected"], 0)
        self.assertEqual(links["matched"], 44)
        self.assertEqual(links["missing"], [])
        self.assertEqual(links["unexpected"], [])

        placement_lots = self.db.execute("""
            SELECT al.lot_id
            FROM alarm AS a JOIN alarm_lot AS al USING (alarm_id)
            WHERE a.alarm_type = 'INCOMPATIBLE_STORAGE'
        """).fetchall()
        self.assertEqual(placement_lots, [("LOT-2026-000001",)])
        self.assertEqual(
            {row["decision"] for row in diagnostics},
            {"SENSOR_DRIFT", "AMBIGUOUS_SENSOR_ZONE"},
        )
        self.assertEqual(diagnostics_from_db(self.db), diagnostics)
        self.assertEqual(
            self.db.execute(
                "SELECT diagnostic_id FROM diagnostic ORDER BY diagnostic_id"
            ).fetchall(),
            [("DGN-000001",), ("DGN-000002",)],
        )

    def test_gap_generation_provenance_preserves_reading_count(self):
        events = []
        before = self.db.execute("SELECT COUNT(*) FROM sensor_reading").fetchone()[0]
        inject(self.db, events)
        after = self.db.execute("SELECT COUNT(*) FROM sensor_reading").fetchone()[0]

        self.assertEqual(before, after)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["omitted_readings"]), 3)
        self.assertEqual(len(events[0]["appended_readings"]), 3)
        self.assertIn("exact profile sensor_reading count", events[0]["reason"])

    def test_missing_outage_alarm_fails_evaluation(self):
        truth, diagnostics = self.run_scenario()
        fixture, _ = build_negative_fixtures(self.db, truth)
        alarm_id = fixture["mutation"]["omit_alarm_id"]
        for table in ("alarm_lot", "alarm_state_event", "alarm"):
            self.db.execute(f"DELETE FROM {table} WHERE alarm_id = ?", (alarm_id,))

        result = score_decisions(self.db, truth, diagnostics)
        self.assertEqual(result["missed"], fixture["expected_evaluation"]["missed_decisions"])
        self.assertEqual(result["missing_decisions"][0][0], "SENSOR_GAP")
        self.assertEqual(result["recall"], 7 / 8)

    def test_missing_affected_lot_link_fails_evaluation(self):
        truth, _ = self.run_scenario()
        _, fixture = build_negative_fixtures(self.db, truth)
        link = tuple(fixture["mutation"]["omit_alarm_lot"])
        self.db.execute(
            "DELETE FROM alarm_lot WHERE alarm_id = ? AND lot_id = ?", link,
        )

        result = score_lot_links(self.db)
        self.assertEqual(len(result["missing"]), fixture["expected_evaluation"]["missing_lot_links"])
        self.assertEqual(result["missing"][0][:2], link)
        self.assertEqual(result["unexpected"], [])

    def test_responses_preserve_history_and_inventory_links(self):
        self.run_scenario()
        inspections, actions, resolved = record_responses(self.db)
        self.assertEqual(inspections, 6)
        self.assertGreaterEqual(actions, 2)
        self.assertEqual(resolved, 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

        placement_hold = self.db.execute("""
            SELECT r.performed_at, s.entered_at, s.exited_at
            FROM remediation AS r
            JOIN alarm AS a USING (alarm_id)
            JOIN lot_storage AS s ON s.storage_id = a.storage_id
            WHERE r.action_type = 'QUARANTINE_LOT'
              AND a.alarm_type = 'INCOMPATIBLE_STORAGE'
        """).fetchone()
        self.assertIsNotNone(placement_hold)
        performed_at, entered_at, exited_at = placement_hold
        self.assertLessEqual(entered_at, performed_at)
        self.assertLess(performed_at, exited_at)

        states = self.db.execute("""
            SELECT e.state
            FROM alarm_state_event AS e JOIN alarm AS a USING (alarm_id)
            WHERE a.alarm_type = 'SENSOR_GAP'
            ORDER BY e.event_seq
        """).fetchall()
        self.assertEqual(states, [("OPEN",), ("ACKNOWLEDGED",), ("RESOLVED",)])

        gap_impacts = self.db.execute("""
            SELECT DISTINCT al.impact_type
            FROM alarm_lot AS al JOIN alarm AS a USING (alarm_id)
            WHERE a.alarm_type = 'SENSOR_GAP'
        """).fetchall()
        self.assertEqual(gap_impacts, [("TELEMETRY_UNVERIFIED",)])

    def test_validation_rejects_quarantine_outside_lot_dwell(self):
        truth, diagnostics = self.run_scenario()
        record_responses(self.db)
        remediation_id, exited_at = self.db.execute("""
            SELECT r.remediation_id, s.exited_at
            FROM remediation AS r
            JOIN alarm AS a USING (alarm_id)
            JOIN lot_storage AS s
              ON s.lot_id = r.lot_id AND s.zone_id = a.zone_id
            WHERE r.action_type = 'QUARANTINE_LOT'
            ORDER BY r.remediation_id LIMIT 1
        """).fetchone()
        self.db.execute(
            "UPDATE remediation SET performed_at = ? WHERE remediation_id = ?",
            (exited_at + 1, remediation_id),
        )

        report = validation_report(self.db, "starter", truth, diagnostics)
        self.assertFalse(report["passed"])
        self.assertFalse(report["checks"]["lot_remediation_during_dwell"])
        self.assertTrue(report["checks"]["sqlite_integrity_and_constraints"])

    def test_export_recomputes_evaluation_instead_of_trusting_old_result(self):
        truth, diagnostics = self.run_scenario()
        record_responses(self.db)
        stale_passing_report = validation_report(
            self.db, "starter", truth, diagnostics
        )
        self.assertTrue(stale_passing_report["passed"])
        with tempfile.TemporaryDirectory() as work:
            old_evaluation_path = Path(work) / "starter_evaluation.json"
            old_evaluation_path.write_text(
                json.dumps(stale_passing_report["evaluation"]), encoding="utf-8"
            )

            self.db.execute("""
                DELETE FROM alarm_lot
                WHERE (alarm_id, lot_id) = (
                    SELECT al.alarm_id, al.lot_id
                    FROM alarm_lot AS al JOIN alarm AS a USING (alarm_id)
                    WHERE a.alarm_type = 'SENSOR_GAP'
                    ORDER BY al.lot_id LIMIT 1
                )
            """)
            current_report = validation_report(
                self.db, "starter", truth, diagnostics
            )
            old_evaluation = json.loads(
                old_evaluation_path.read_text(encoding="utf-8")
            )
            self.assertTrue(old_evaluation["passed"])
            self.assertFalse(current_report["passed"])
            self.assertFalse(
                current_report["checks"]["fixture_decisions_and_lot_links"]
            )

    def test_starter_reproducibility_command_is_deterministic(self):
        with tempfile.TemporaryDirectory() as work:
            report_path = Path(work) / "reproducibility.json"
            completed = subprocess.run(
                [
                    sys.executable, str(ROOT / "reproduce.py"),
                    "--profile", "starter", "--seed", "20261022",
                    "--output", str(report_path),
                ],
                cwd=ROOT, text=True, capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])
            self.assertTrue(report["row_counts_match"])
            self.assertTrue(report["export_checksums_match"])
            self.assertEqual(report["row_counts"]["diagnostic"], 2)


if __name__ == "__main__":
    unittest.main()
