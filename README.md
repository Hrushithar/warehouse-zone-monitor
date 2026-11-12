# Warehouse Zone Monitor

A Python and SQLite pipeline that monitors temperature, humidity, occupancy, and door events by warehouse zone. It detects risks, identifies inventory lots that were present during each incident, and records simulated inspections and remediation with an auditable alarm history.

## Quick start

Use Python 3.11 or newer. The pipeline uses the Python standard library. The `sqlite3` command-line tool is used in the SQL examples below.

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m unittest discover -s tests -v

python generate.py --profile starter --seed 20261022 &&
python inject_scenarios.py --profile starter &&
python monitor.py --profile starter &&
python respond.py --profile starter &&
python evaluate.py --profile starter &&
python export_data.py --profile starter --seed 20261022
```

The starter run produces **6 alarms**, **2 diagnostics**, and **44 affected-lot links**. Evaluation matches **8 of 8 labelled decisions** and **44 of 44 lot links**, including the overlap durations. Precision and recall are both 1.0 on these synthetic fixtures.

Use `--profile full` in all six pipeline commands to generate **10 warehouses, 100 zones, 500 sensors, 1,000,000 readings, and 10,000 inventory lots**. Both profiles use the same generator. Files are written to `generated/`. For a fresh rerun, use a fresh checkout or rename the existing generated outputs.

## How decisions are made

| Signal | Decision rule |
|---|---|
| Temperature and humidity | Alarm after three consecutive samples outside the configured zone range. Values equal to a limit are allowed. |
| Occupancy | Alarm after three consecutive samples above the zone limit. |
| Door | Alarm when the door remains open longer than the zone limit. |
| Missing readings | Infer missing samples from the expected cadence between observations. Affected lots are marked `TELEMETRY_UNVERIFIED`. |
| Sensor drift | Report three consecutive in-range temperature samples more than 1.5 °C from the zone's reference probe. |
| Incompatible placement | Alarm when a lot's required zone type differs from its assigned zone type. |
| Conflicting sensor placement | Report overlapping cross-zone assignments without a rationale. Skip zone attribution when a sample matches multiple assignments. |

A lot is linked to an alarm when it was in the same zone and its dwell interval has positive overlap with the alarm window. Intervals use UTC Unix seconds and an exclusive end. Placement alarms link the offending lot. A long door opening can still raise an alarm in an empty zone.

Alarm state changes are appended as events. Inspections reference the alarm, and lot-level remediation must reference a lot linked to that same alarm. The simulated response can quarantine affected lots or record a sensor repair. A resumed sensor reading can close the telemetry alarm while preserving the unverified inventory history.

## Inspect an alarm

```bash
sqlite3 -header -column generated/starter_monitored.sqlite "
SELECT a.alarm_type, a.zone_id, l.lot_id, l.overlap_seconds, l.impact_type
FROM alarm AS a
LEFT JOIN alarm_lot AS l USING (alarm_id)
ORDER BY a.alarm_id, l.lot_id;
"
```

## Validation and reproducibility

- [Schema](schema.sql) and [data dictionary](docs/data_dictionary.md) document the entities, keys, relationships, and lifecycle rules.
- [Regression tests](tests/test_pipeline.py) cover the clean baseline, threshold boundaries, incompatible placement, conflicting assignments, missing outage alarms, missing affected-lot links, and response history.
- [Full validation summary](docs/full_validation_summary.json) records integrity and foreign-key checks, expected volumes, date and lifecycle checks, and evaluation results.
- [Full source manifest](docs/full_source_manifest.json) records the fixed seed, synthetic provenance, row counts, and SHA-256 checksums.

To reproduce the full dataset, run the six pipeline commands with `--profile full` and seed `20261022` in two fresh checkouts using the same Python version. Compare their `generated/full_export/source_manifest.json` files with `cmp`. Matching manifests demonstrate identical row counts and export checksums.

Each export includes CSV and JSON for every table, a `seed.sql` snapshot, separate `ground_truth.json`, diagnostics, a validation summary, and the source manifest. The detector does not read ground truth. All data is synthetic and the project uses no external datasets.

To restore the full export into a fresh SQLite database:

```bash
sqlite3 -bail generated/restored.sqlite < generated/full_export/seed.sql
sqlite3 generated/restored.sqlite "
SELECT COUNT(*) FROM sensor_reading;
SELECT COUNT(*) FROM inventory_lot;
PRAGMA integrity_check;
PRAGMA foreign_key_check;
"
```

The verified restore returned `1000000` readings, `10000` inventory lots, and `ok` from `integrity_check`.

## Replay assumptions

The pipeline runs a batch replay of synthetic observations. Zone temperature and humidity thresholds are selected at replay start, and lot requirements are versioned at receipt. Gap detection covers missing samples between observations. Leading, trailing, and complete sensor silence require an explicit expected monitoring schedule.
