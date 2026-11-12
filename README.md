# Warehouse Zone Monitor

Warehouse Zone Monitor is a deterministic batch replay that turns synthetic warehouse telemetry and inventory dwell history into auditable incidents, affected-lot links, and simulated response records. It is designed so a reviewer can reproduce the submission without services, credentials, or external data.

## Problem and approach

Warehouses need more than a threshold alert. When temperature, humidity, occupancy, a door, or telemetry itself becomes unsafe or uncertain, an operator also needs to know which inventory was present, for how long, and what happened next.

This pipeline:

1. Generates a valid synthetic warehouse baseline: zones, sensors, effective sensor assignments, readings, door events, product requirements, lots, and lot dwell intervals.
2. Injects eight labelled scenarios into a copy of that baseline and writes the labels to a separate ground-truth file.
3. Replays the observations with fixed rules. Confirmed incidents become `alarm` rows; drift and ambiguous assignment findings remain diagnostics.
4. Intersects each alarm's half-open interval with same-zone lot dwell intervals. Positive overlaps become `alarm_lot` rows with the exact overlap in seconds and an impact classification.
5. Appends alarm state events, inspections, and simulated remediation. These records demonstrate the response model; they do not operate real equipment or inventory.
6. Scores the result against the independent labels, validates relational and lifecycle invariants, and exports reviewable CSV, JSON, and SQL artifacts.

Python keeps the generator, detector, response, evaluation, and export logic readable and testable. SQLite provides transactions, checks, foreign keys, and portable inspection with a single local file. A fixed random seed plus explicit, versioned rules makes both profiles deterministic and lets two independent runs be compared byte for byte.

The principal rules are:

| Signal | Decision |
|---|---|
| Temperature or humidity | Three consecutive samples outside the active zone-type range. Values exactly at a minimum or maximum are allowed. |
| Occupancy | Three consecutive samples strictly above the zone's limit. |
| Door | Open duration strictly greater than the zone's limit. |
| Missing telemetry | One or more expected cadence points between two observed readings produce a `SENSOR_GAP`; overlapping lots are `TELEMETRY_UNVERIFIED`. |
| Incompatible placement | A lot dwelling in a zone whose type differs from its stored requirement version produces `INCOMPATIBLE_STORAGE`. |
| Sensor drift | Three consecutive in-range temperature readings more than 1.5 °C from the generated reference probe produce a diagnostic. |
| Conflicting assignment | Overlapping assignments to different zones without a rationale produce an `AMBIGUOUS_SENSOR_ZONE` diagnostic; ambiguous samples are not attributed to either zone. |

See the [data dictionary](docs/data_dictionary.md) for the complete schema, field constraints, relationships, interval conventions, and the boundary between database constraints and replay validation.

## Setup from a fresh clone

Prerequisites are Git, Python 3.11 or newer, and the `sqlite3` command-line client. The pipeline itself uses only the Python standard library.

macOS with Homebrew:

```bash
xcode-select --install  # skip if Command Line Tools are already installed
brew install python@3.11 sqlite
git clone https://github.com/Hrushithar/warehouse-zone-monitor.git
cd warehouse-zone-monitor
python3.11 -m venv .venv
source .venv/bin/activate
python -m unittest discover -s tests -v
```

Debian 12 or Ubuntu 24.04+ Linux:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv sqlite3
git clone https://github.com/Hrushithar/warehouse-zone-monitor.git
cd warehouse-zone-monitor
python3 -m venv .venv
source .venv/bin/activate
python -m unittest discover -s tests -v
```

The test command should report six passing tests and `OK`. No `pip install` step is required.

## Run the pipeline in order

Run each profile in a fresh clone or with no existing outputs for that profile. The scripts intentionally refuse to overwrite databases, ground truth, diagnostics, or export directories. All outputs go under `generated/`.

### Starter profile

The starter profile is the fast reviewer path: one warehouse, three zones, 15 sensors, 1,440 readings, and 30 lots.

```bash
python generate.py --profile starter --seed 20261022
python inject_scenarios.py --profile starter
python monitor.py --profile starter
python respond.py --profile starter
python evaluate.py --profile starter
python export_data.py --profile starter --seed 20261022
```

Expected headline output is 8 labelled scenarios, 6 alarms, 2 diagnostics, 44 affected-lot links, 6 inspections, 23 remediation actions, one resolved telemetry alarm, a passing evaluation, and a passing export validation.

### Full profile

The full profile exercises the same code and fixtures at submission scale: 10 warehouses, 100 zones, 500 sensors, 1,000,000 readings, and 10,000 lots.

```bash
python generate.py --profile full --seed 20261022
python inject_scenarios.py --profile full
python monitor.py --profile full
python respond.py --profile full
python evaluate.py --profile full
python export_data.py --profile full --seed 20261022
```

The full profile produces the same 6 alarms and 2 diagnostics. The committed full-run evidence records 8 of 8 decisions matched, precision 1.0, recall 1.0, 32 of 32 affected-lot links matched, 6 inspections, 16 remediation actions, and 13 alarm-state events. These are full-profile figures; the 44 lot links and 23 remediation actions above are starter-profile figures.

### What each stage does

| Stage | Reads | Writes | Success looks like |
|---|---|---|---|
| `generate.py` | `schema.sql`, profile, seed | `generated/<profile>.sqlite` | Prints profile row counts, seed `20261022`, and the database path. This is the clean baseline. |
| `inject_scenarios.py` | Baseline database | `generated/<profile>_scenario.sqlite`, `generated/<profile>_ground_truth.json` | Prints `Labelled scenarios: 8`. The baseline remains unchanged. |
| `monitor.py` | Scenario database | `generated/<profile>_monitored.sqlite`, `generated/<profile>_diagnostics.json` | Prints 6 alarms and 2 diagnostics, plus 44 starter or 32 full affected-lot links. |
| `respond.py` | Monitored database | Appends state, inspection, and remediation rows to that database | Prints 6 inspections and one resolved telemetry alarm. Responses are simulated. |
| `evaluate.py` | Monitored database, ground truth, diagnostics | `generated/<profile>_evaluation.json` | Exits zero with `"passed": true`; decisions and independently recomputed lot links have no misses or unexpected rows. |
| `export_data.py` | Responded database, ground truth, diagnostics, schema, seed | `generated/<profile>_export/` | Prints `Validation passed`; the directory contains table exports, evidence reports, and a restorable `seed.sql`. |

The order matters: injection copies the baseline, monitoring copies the scenario database, response mutates the monitored copy, evaluation reads those results, and export validates the completed replay.

## Trace the sensor-gap example

After the starter commands finish, these queries trace the deliberately removed humidity samples through the alarm, affected inventory, and simulated response.

```bash
sqlite3 -header -column generated/starter_monitored.sqlite <<'SQL'
SELECT a.alarm_id,
       a.alarm_type,
       a.zone_id,
       a.sensor_id,
       datetime(a.started_at, 'unixepoch') AS gap_start_utc,
       datetime(a.window_end_at, 'unixepoch') AS gap_end_utc,
       a.reason
FROM alarm AS a
WHERE a.alarm_type = 'SENSOR_GAP';

SELECT al.lot_id,
       al.overlap_seconds,
       al.impact_type,
       datetime(s.entered_at, 'unixepoch') AS entered_utc,
       datetime(s.exited_at, 'unixepoch') AS exited_utc
FROM alarm AS a
JOIN alarm_lot AS al USING (alarm_id)
JOIN lot_storage AS s
  ON s.lot_id = al.lot_id
 AND s.zone_id = a.zone_id
 AND s.entered_at < a.window_end_at
 AND (s.exited_at IS NULL OR s.exited_at > a.started_at)
WHERE a.alarm_type = 'SENSOR_GAP'
ORDER BY al.lot_id;

SELECT i.inspection_id,
       i.finding,
       i.inspector_role,
       r.action_type,
       COALESCE(r.lot_id, '(equipment)') AS target,
       r.notes
FROM alarm AS a
JOIN inspection AS i USING (alarm_id)
LEFT JOIN remediation AS r USING (inspection_id, alarm_id)
WHERE a.alarm_type = 'SENSOR_GAP';
SQL
```

The fixed starter run shows `ALM-000005` for humidity sensor `SNS-ZONE-WH-001-001-HUMIDITY-01`: three expected five-minute samples are absent from 02:30 through the exclusive end at 02:45 UTC. Nine cold-zone lots overlap that interval. Eight overlap all 900 seconds; `LOT-2026-000003`, which exits at 02:35, overlaps 300 seconds. Every link is `TELEMETRY_UNVERIFIED`, meaning conditions cannot be confirmed rather than that a threshold breach was observed. `INSP-000005` has the same finding and a `REPAIR_SENSOR` equipment action. The recovered reading at the gap's end allows the response stage to append `OPEN`, `ACKNOWLEDGED`, and `RESOLVED` state events while retaining the lot-impact history.

## Validation, negative cases, and reproducibility

`inject_scenarios.py` writes ground truth before detection. The monitor never reads it. The eight labels cover six persisted alarms—temperature, humidity, occupancy, door duration, telemetry gap, and incompatible storage—and two diagnostics—sensor drift and ambiguous assignment. The evaluator compares decision type, zone, source, start, and exclusive end as a multiset. Separately, it reconstructs expected lot links and overlap seconds from `alarm` and `lot_storage`, then compares the complete `(alarm_id, lot_id, overlap_seconds, impact_type)` set.

### Four required negative cases

The hackathon's four required cases and their verified outcomes are:

1. **Incompatible lot placement:** moving a cold-storage lot into an ambient zone produces an `INCOMPATIBLE_STORAGE` alarm with `PLACEMENT_VIOLATION` impact, linked to the offending lot (`LOT-2026-000001`).
2. **Ambiguous sensor assignment:** mapping one sensor to multiple zones over the same interval without a rationale produces an `AMBIGUOUS_SENSOR_ZONE` diagnostic. Readings with multiple active assignments are not attributed to either zone.
3. **Reading gap treated as normal:** deliberately omitting the expected `SENSOR_GAP` alarm is caught by decision evaluation as one missed decision and reduces recall from 1.0 to 7/8.
4. **Alarm missing an affected-lot link:** deliberately omitting the expected `(alarm_id, lot_id)` row is caught by independent lot-link reconciliation as one missing link.

Cases 1 and 2 are deterministic input scenarios labelled by `inject_scenarios.py` and asserted by the scenario regression test. Cases 3 and 4 are the two deliberate output mutations in [the committed full negative-fixture report](docs/full_negative_fixtures.json), which identifies the exact rows to omit and the expected evaluator failures.

Additional coverage protects against false positives: a clean generated baseline must produce no alarms or diagnostics, and a reading exactly at the 8 °C limit followed by only two high samples must not produce a temperature alarm.

Evidence included in Git:

- [Full validation summary](docs/full_validation_summary.json): all integrity, key, foreign-key, volume, date-order, source-zone, dwell, lifecycle, and evaluation checks passed.
- [Full source manifest](docs/full_source_manifest.json): fixed seed, synthetic provenance, row counts, and SHA-256 values for exported files.
- [Full reproducibility report](docs/full_reproducibility.json): two independent full runs on Python 3.14.7 had matching row counts and all 33 compared export-file checksums.
- [Full negative fixtures](docs/full_negative_fixtures.json): the two deliberate output omissions described above.
- [Regression tests](tests/test_pipeline.py) and [schema](schema.sql): executable behavior and database constraints.

To reproduce the manifest comparison, make two fresh clones, run the six full-profile commands in each with seed `20261022`, and compare the completed manifests:

```bash
git clone https://github.com/Hrushithar/warehouse-zone-monitor.git warehouse-zone-monitor-run-a
git clone https://github.com/Hrushithar/warehouse-zone-monitor.git warehouse-zone-monitor-run-b

# In each clone, create/activate a virtual environment and run the six full commands above.
cmp warehouse-zone-monitor-run-a/generated/full_export/source_manifest.json \
    warehouse-zone-monitor-run-b/generated/full_export/source_manifest.json
```

`cmp` exits silently with status 0 when the manifests match. Because each manifest contains the row counts and SHA-256 digest of every other export artifact, equality verifies the exported content. Use the same Python version in both clones; the committed comparison used Python 3.14.7.

Restore an exported SQL snapshot into a new database and verify it with:

```bash
sqlite3 -bail generated/restored.sqlite < generated/full_export/seed.sql
sqlite3 -header -column generated/restored.sqlite <<'SQL'
SELECT COUNT(*) AS sensor_readings FROM sensor_reading;
SELECT COUNT(*) AS inventory_lots FROM inventory_lot;
PRAGMA integrity_check;
PRAGMA foreign_key_check;
SQL
```

For the full export, the counts should be `1000000` and `10000`; `integrity_check` should return `ok`, and `foreign_key_check` should return no rows. Choose a database path that does not already contain these tables.

## Deliverables and limits

Local runtime artifacts are intentionally kept under `generated/` and out of Git:

- `generated/<profile>.sqlite`, `<profile>_scenario.sqlite`, and `<profile>_monitored.sqlite`
- `generated/<profile>_ground_truth.json`, `<profile>_diagnostics.json`, and `<profile>_evaluation.json`
- `generated/<profile>_export/`, containing CSV and JSON for all 14 tables plus `seed.sql`, `ground_truth.json`, `diagnostics.json`, `negative_fixtures.json`, `validation_summary.json`, and `source_manifest.json`

Only the compact full-run evidence reports listed above are committed. The generated datasets themselves are not.

This is a batch replay over synthetic data, not a streaming service or control system, and every response is simulated. Gap detection currently finds cadence-aligned missing samples only between two observed readings. It does not infer leading silence, trailing silence, or a sensor that is completely silent because the current schema has no independent expected monitoring window. A production system would need that schedule, ingestion-time/watermark handling, durable streaming state, notification delivery, authentication, and integrations with warehouse and quality systems.
