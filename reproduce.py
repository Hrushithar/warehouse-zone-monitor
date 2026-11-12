import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from generate import PROFILES, ROOT


PIPELINE_FILES = (
    "schema.sql", "generate.py", "inject_scenarios.py", "monitor.py",
    "respond.py", "evaluate.py", "export_data.py", "negative_fixtures.py",
)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_pipeline(destination):
    destination.mkdir()
    for name in PIPELINE_FILES:
        shutil.copy2(ROOT / name, destination / name)


def run_stage(run_dir, arguments):
    command = [sys.executable, *arguments]
    completed = subprocess.run(
        command, cwd=run_dir, text=True, capture_output=True,
    )
    if completed.returncode:
        rendered = " ".join(command)
        raise RuntimeError(
            f"Pipeline stage failed ({rendered})\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def run_pipeline(run_dir, profile, seed):
    copy_pipeline(run_dir)
    stages = (
        ("generate.py", "--profile", profile, "--seed", str(seed)),
        ("inject_scenarios.py", "--profile", profile),
        ("monitor.py", "--profile", profile),
        ("respond.py", "--profile", profile),
        ("evaluate.py", "--profile", profile),
        ("export_data.py", "--profile", profile, "--seed", str(seed)),
    )
    for stage in stages:
        print(f"[{run_dir.name}] {Path(sys.executable).name} {' '.join(stage)}", flush=True)
        run_stage(run_dir, stage)

    export = run_dir / "generated" / f"{profile}_export"
    manifest_path = export / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "row_counts": manifest["row_counts"],
        "export_checksums": manifest["sha256"],
        "manifest_sha256": sha256(manifest_path),
    }


def differences(first, second):
    return {
        key: {"run_1": first.get(key), "run_2": second.get(key)}
        for key in sorted(set(first) | set(second))
        if first.get(key) != second.get(key)
    }


def compare_runs(profile, seed):
    with tempfile.TemporaryDirectory(prefix="warehouse-zone-reproduce-") as work:
        work_path = Path(work)
        runs = [
            run_pipeline(work_path / f"run-{number}", profile, seed)
            for number in (1, 2)
        ]

    row_differences = differences(runs[0]["row_counts"], runs[1]["row_counts"])
    checksum_differences = differences(
        runs[0]["export_checksums"], runs[1]["export_checksums"]
    )
    report = {
        "profile": profile,
        "seed": seed,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "independent_runs": 2,
        "row_counts": runs[0]["row_counts"],
        "row_counts_match": not row_differences,
        "export_files_compared": len(
            set(runs[0]["export_checksums"]) | set(runs[1]["export_checksums"])
        ),
        "export_checksums_match": not checksum_differences,
        "manifest_sha256": runs[0]["manifest_sha256"],
        "run_manifest_sha256": [run["manifest_sha256"] for run in runs],
        "differences": {
            "row_counts": row_differences,
            "export_checksums": checksum_differences,
        },
    }
    report["passed"] = report["row_counts_match"] and report["export_checksums_match"]
    return report


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(rendered)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def main():
    parser = argparse.ArgumentParser(
        description="Run two isolated warehouse pipelines and compare deterministic exports."
    )
    parser.add_argument("--profile", choices=PROFILES, default="starter")
    parser.add_argument("--seed", type=int, default=20261022)
    parser.add_argument(
        "--output", type=Path,
        help="Report path (default: generated/<profile>_reproducibility.json)",
    )
    args = parser.parse_args()
    output = args.output or ROOT / "generated" / f"{args.profile}_reproducibility.json"

    try:
        report = compare_runs(args.profile, args.seed)
    except Exception as error:
        raise SystemExit(f"Reproducibility run failed: {error}") from error
    write_report(output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"Reproducibility report: {output}")
    if not report["passed"]:
        raise SystemExit("Reproducibility comparison failed; see differences in the report.")


if __name__ == "__main__":
    main()
