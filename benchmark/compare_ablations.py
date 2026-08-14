"""Mechanism-ablation harness for evaluation (mid-term review, revised 2026-08-14).

Runs one goldset under a matrix of ablation switches and scores each run with the
standard benchmark runner. Methodology guardrails (mentor feedback):

- Switches belong to four categories (see src/target_agent/ablations.py):
  evidence_input / scoring / safety_negative_control / model_component. Rows are
  reported per category; they are NOT one comparable set and must not be ranked
  against each other by total score.
- ``delta_vs_baseline`` is a DESCRIPTIVE assertion-score delta on this goldset.
  Because the goldset partially asserts the ablated mechanism itself, a delta
  does NOT by itself establish an independent contribution. For that claim use
  the blind-ranking protocol (benchmark/rubric.md, BM-14 scorer) on fixed cases,
  candidates and labels: disease-macro nDCG, Recall@K, MRR, GO/NO_GO blocker
  accuracy, trap/safety violations and completed_with_gaps coverage.
- ``no_context_gate`` is a safety negative control: its readout is the
  mismatched-evidence admission / safety-violation rate, not a ranking drop.

Default matrix (deterministic fake+unit goldsets only; live matrices are run
separately because they are not byte-reproducible):
    baseline                    full mechanism stack
    each single switch          one mechanism removed at a time
    all                         every switch together (mechanism-free floor)

Usage:
    python benchmark/compare_ablations.py [--goldset benchmark/goldset_v2.jsonl]
                                          [--out benchmark/results_ablation]
                                          [--sets baseline,no_mechanism_bonus,...]

Exit code is always 0: an ablated score below 1.0 is a measurement, not a failure.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from target_agent.ablations import VALID, category_of, parse  # noqa: E402

DEFAULT_SETS = ["baseline", *sorted(VALID), "all"]


def run_set(goldset: Path, out_root: Path, name: str) -> dict:
    ablate = "" if name == "baseline" else (",".join(sorted(VALID)) if name == "all" else name)
    out_dir = out_root / name
    cmd = [sys.executable, str(ROOT / "benchmark" / "runner.py"),
           "--goldset", str(goldset), "--out", str(out_dir)]
    if ablate:
        cmd += ["--ablate", ablate]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    report_path = out_dir / "benchmark_report.json"
    if not report_path.exists():
        return {"set": name, "ablations": ablate or "none", "error": proc.stderr.strip()[-400:]}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = report["summary"]
    failed = [
        {"id": task["id"], "failures": [a["failure"] for a in task["results"] if not a["passed"]]}
        for task in report["tasks"] if not task.get("skipped") and not task["passed"]
    ]
    category = "baseline" if name == "baseline" else (
        "all_combined" if name == "all" else category_of(name))
    return {
        "set": name, "category": category, "ablations": ablate or "none",
        "score": summary["score"], "assertions": summary["assertions"],
        "assertions_passed": summary["assertions_passed"],
        "tasks_passed": summary["tasks_passed"], "tasks": summary["tasks"],
        "categories": summary["categories"], "failed_tasks": failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--goldset", type=Path, default=ROOT / "benchmark" / "goldset_v2.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "benchmark" / "results_ablation")
    parser.add_argument("--sets", type=str, default=",".join(DEFAULT_SETS),
                        help="comma-separated: baseline, any valid switch, or 'all'")
    args = parser.parse_args()

    sets = [part.strip() for part in args.sets.split(",") if part.strip()]
    for name in sets:
        if name not in ("baseline", "all"):
            parse(name)  # raises on an unknown switch, before any expensive run

    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in sets:
        print(f"[ablation] {name} ...", flush=True)
        rows.append(run_set(args.goldset, args.out, name))
        if "error" not in rows[-1]:
            print(f"[ablation] {name} -> score {rows[-1]['score']}", flush=True)

    baseline = next((row for row in rows if row["set"] == "baseline" and "error" not in row), None)
    for row in rows:
        if baseline and "error" not in row:
            row["delta_vs_baseline"] = round(baseline["score"] - row["score"], 4)

    payload = {
        "goldset": args.goldset.name,
        "method": ("Each row re-runs the identical goldset with the named mechanism(s) disabled "
                   "(evaluation-only AblationConfig). delta_vs_baseline is a DESCRIPTIVE "
                   "assertion-score delta on this goldset, grouped by ablation category; it does "
                   "not by itself establish an independent contribution — that claim requires the "
                   "blind-ranking protocol (benchmark/rubric.md, BM-14 scorer) with nDCG/Recall@K/"
                   "MRR/blocker/safety metrics on fixed cases, candidates and labels. The base "
                   "model is identical across rows."),
        "rows": rows,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "ablation_report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# Mechanism-Ablation Report (descriptive)", "",
        f"- Gold set: `{args.goldset.name}` (fake+unit, deterministic)",
        "- Each row re-runs the identical goldset with the named mechanism(s) disabled; the base "
        "model is untouched.",
        "- Rows are grouped by ablation category and are NOT comparable across categories. "
        "`no_context_gate` is a safety negative control: read it as mismatched-evidence admission, "
        "not as a ranking contribution.",
        "- A score delta here is descriptive; independent-contribution claims require the "
        "blind-ranking protocol (benchmark/rubric.md, BM-14) with nDCG / Recall@K / MRR / "
        "blocker-accuracy / trap-safety metrics on fixed cases, candidates and labels.", "",
        "| Ablation set | Category | Disabled mechanism(s) | Score | Δ vs baseline | Failed tasks |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        if "error" in row:
            lines.append(f"| {row['set']} | - | {row['ablations']} | ERROR | - | {row['error'][:80]} |")
            continue
        failed = ", ".join(task["id"] for task in row["failed_tasks"]) or "-"
        lines.append(f"| {row['set']} | {row['category']} | {row['ablations']} | {row['score']} "
                     f"| {row.get('delta_vs_baseline', 0)} | {failed} |")
    (args.out / "ablation_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({row["set"]: row.get("score", "ERROR") for row in rows}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
