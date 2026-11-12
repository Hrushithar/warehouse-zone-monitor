import sqlite3
import unittest

from evaluate import score_decisions, score_lot_links
from generate import ROOT, populate
from inject_scenarios import COLD, inject, set_readings
from monitor import detect, persist
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
        persist(self.db, alarms)
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

    def test_missing_outage_alarm_fails_evaluation(self):
        truth = inject(self.db)
        alarms, diagnostics = detect(self.db)
        # Simulate a detector that incorrectly treats the outage as normal.
        persist(self.db, [alarm for alarm in alarms if alarm[0] != "SENSOR_GAP"])

        result = score_decisions(self.db, truth, diagnostics)
        self.assertEqual(result["missed"], 1)
        self.assertEqual(result["missing_decisions"][0][0], "SENSOR_GAP")
        self.assertEqual(result["recall"], 7 / 8)

    def test_missing_affected_lot_link_fails_evaluation(self):
        self.run_scenario()
        link = self.db.execute("""
            SELECT al.alarm_id, al.lot_id
            FROM alarm_lot AS al JOIN alarm AS a USING (alarm_id)
            WHERE a.alarm_type = 'TEMP_EXCURSION'
            ORDER BY al.lot_id LIMIT 1
        """).fetchone()
        self.assertIsNotNone(link)
        self.db.execute(
            "DELETE FROM alarm_lot WHERE alarm_id = ? AND lot_id = ?", link,
        )

        result = score_lot_links(self.db)
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["missing"][0][:2], link)
        self.assertEqual(result["unexpected"], [])

    def test_responses_preserve_history_and_inventory_links(self):
        self.run_scenario()
        inspections, actions, resolved = record_responses(self.db)
        self.assertEqual(inspections, 6)
        self.assertGreaterEqual(actions, 2)
        self.assertEqual(resolved, 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

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


if __name__ == "__main__":
    unittest.main()