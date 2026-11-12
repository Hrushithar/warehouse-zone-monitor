# Starter reviewer sample

This directory is the complete validated export for the `starter` profile with seed `20261022`: 1 warehouse, 3 zones, 15 sensors, 1,440 sensor readings, 30 inventory lots, 6 alarms, 2 persisted diagnostics, and 44 affected-lot links. The export is about 665 KB of file content and contains no SQLite database.

It was generated with:

```bash
python generate.py --profile starter --seed 20261022
python inject_scenarios.py --profile starter
python monitor.py --profile starter
python respond.py --profile starter
python evaluate.py --profile starter
python export_data.py --profile starter --seed 20261022
```

The export contains 37 generated files; `source_manifest.json` records checksums for the other 36. This explanatory README is not part of that manifest. Restore `seed.sql` into a fresh SQLite database to inspect the same rows relationally.
