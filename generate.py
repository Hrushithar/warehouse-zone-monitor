import argparse
import math
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
START = datetime(2026, 10, 22, tzinfo=timezone.utc)
START_TS = int(START.timestamp())
CADENCE = 300

PROFILES = {
    "starter": (1, 3, 96, 10),
    "full": (10, 10, 2_000, 100),
}

# Zone type, product code, temperature limits in °C, humidity limits in % RH
REQUIREMENTS = (
    ("COLD", "CHILLED_GOODS", 2, 8, 45, 75),
    ("AMBIENT", "DRY_GOODS", 15, 25, 30, 60),
    ("HOT", "HEAT_TOLERANT_GOODS", 28, 38, 25, 55),
)

SENSOR_LAYOUT = (
    ("TEMP", 1),
    ("TEMP", 2),
    ("HUMIDITY", 1),
    ("HUMIDITY", 2),
    ("OCCUPANCY", 1),
)


def reading_rows(sensor_id, sensor_type, samples, temp, humidity, rng):
    for sample in range(samples):
        offset = sample * CADENCE
        moment = START + timedelta(seconds=offset)
        timestamp_label = moment.strftime("%Y%m%dT%H%M%SZ")
        daily_cycle = math.sin(2 * math.pi * offset / 86_400)

        if sensor_type == "TEMP":
            value = round(temp + 0.8 * daily_cycle + rng.uniform(-0.3, 0.3), 2)
        elif sensor_type == "HUMIDITY":
            value = round(humidity - 3 * daily_cycle + rng.uniform(-1, 1), 2)
        else:
            hour = moment.hour
            value = rng.randint(2, 7) if 6 <= hour < 18 else rng.randint(0, 2)

        yield (
            f"RDG-{sensor_id}-{timestamp_label}",
            sensor_id,
            START_TS + offset,
            value,
        )


def populate(db, profile, seed):
    warehouses, zones_per_warehouse, samples, lots_per_zone = PROFILES[profile]
    rng = random.Random(seed)
    storage_count = assignment_count = door_event_count = 0

    for index, rule in enumerate(REQUIREMENTS, start=1):
        zone_type, product_code, temp_min, temp_max, rh_min, rh_max = rule
        db.execute(
            "INSERT INTO storage_requirement VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"REQ-{index:03d}", product_code, zone_type,
                temp_min, temp_max, rh_min, rh_max, START_TS, None,
            ),
        )

    for warehouse_number in range(1, warehouses + 1):
        warehouse_id = f"WH-{warehouse_number:03d}"
        db.execute(
            "INSERT INTO warehouse VALUES (?, ?)",
            (warehouse_id, f"Warehouse {warehouse_number:03d}"),
        )

        for zone_number in range(1, zones_per_warehouse + 1):
            rule_index = (zone_number - 1) % len(REQUIREMENTS)
            zone_type, _, temp_min, temp_max, rh_min, rh_max = REQUIREMENTS[rule_index]
            zone_id = f"ZONE-{warehouse_id}-{zone_number:03d}"
            temp = (temp_min + temp_max) / 2 + rng.uniform(-0.5, 0.5)
            humidity = (rh_min + rh_max) / 2 + rng.uniform(-1, 1)

            db.execute(
                "INSERT INTO zone VALUES (?, ?, ?, ?, ?, ?)",
                (zone_id, warehouse_id, f"{zone_type} {zone_number:03d}", zone_type, 8, 120),
            )

            for sensor_type, number in SENSOR_LAYOUT:
                sensor_id = f"SNS-{zone_id}-{sensor_type}-{number:02d}"
                db.execute(
                    "INSERT INTO sensor VALUES (?, ?, ?)",
                    (sensor_id, sensor_type, CADENCE),
                )
                assignment_count += 1
                db.execute(
                    "INSERT INTO sensor_zone_assignment VALUES (?, ?, ?, ?, ?, ?)",
                    (f"ASG-{assignment_count:06d}", sensor_id, zone_id, START_TS, None, None),
                )
                db.executemany(
                    "INSERT INTO sensor_reading VALUES (?, ?, ?, ?)",
                    reading_rows(sensor_id, sensor_type, samples, temp, humidity, rng),
                )

            for _ in range(lots_per_zone):
                storage_count += 1
                lot_id = f"LOT-2026-{storage_count:06d}"
                received_at = START_TS + rng.randrange(samples // 4) * CADENCE
                dwell_seconds = rng.randint(samples // 4, samples // 2) * CADENCE
                db.execute(
                    "INSERT INTO inventory_lot VALUES (?, ?, ?, ?)",
                    (lot_id, f"REQ-{rule_index + 1:03d}", received_at, rng.randint(20, 200)),
                )
                db.execute(
                    "INSERT INTO lot_storage VALUES (?, ?, ?, ?, ?)",
                    (
                        f"STG-{storage_count:06d}", lot_id, zone_id,
                        received_at, received_at + dwell_seconds,
                    ),
                )

            for sample in range(12, samples, 48):
                opened_at = START_TS + sample * CADENCE
                closed_at = opened_at + rng.randint(15, 90)
                for event_type, occurred_at in (("OPEN", opened_at), ("CLOSE", closed_at)):
                    door_event_count += 1
                    db.execute(
                        "INSERT INTO door_event VALUES (?, ?, ?, ?, ?)",
                        (f"DOR-{door_event_count:06d}", zone_id, "DOOR-01", occurred_at, event_type),
                    )


def main():
    parser = argparse.ArgumentParser(description="Generate a synthetic warehouse baseline.")
    parser.add_argument("--profile", choices=PROFILES, default="starter")
    parser.add_argument("--seed", type=int, default=20261022)
    args = parser.parse_args()

    output_dir = ROOT / "generated"
    output_dir.mkdir(exist_ok=True)
    destination = output_dir / f"{args.profile}.sqlite"
    if destination.exists():
        parser.error(f"{destination} already exists. Rename or remove it before regenerating.")

    # Write a temporary snapshot and publish it after the database closes.
    temporary = destination.with_suffix(".sqlite.tmp")
    if temporary.exists():
        parser.error(f"{temporary} already exists. Remove it if a previous run was interrupted.")

    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
            with db:
                populate(db, args.profile, args.seed)
                violations = db.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(f"Foreign key violations: {violations[:5]}")
            for table in (
                "warehouse", "zone", "sensor", "sensor_reading",
                "inventory_lot", "lot_storage", "door_event",
            ):
                count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"{table}: {count:,}")
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)

    print(f"Seed: {args.seed}")
    print(f"Database: {destination}")


if __name__ == "__main__":
    main()