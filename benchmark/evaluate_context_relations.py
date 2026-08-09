"""Rule-based scorer for context-relation benchmark predictions."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GOLDSET = ROOT / "benchmark" / "goldset_context_relations.jsonl"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def safe_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def evaluate(gold: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    predicted = {item["id"]: item for item in predictions}
    duplicate_count = len(predictions) - len(predicted)
    totals: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    by_family: dict[str, Counter[str]] = defaultdict(Counter)
    by_split: dict[str, Counter[str]] = defaultdict(Counter)
    split_case_counts: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []

    for case in gold:
        prediction = predicted.get(case["id"])
        checks = {
            "coverage": prediction is not None,
            "label": bool(prediction) and prediction.get("label") == case["gold"]["label"],
            "required_actions": bool(prediction) and set(case["gold"]["required_actions"]).issubset(
                set(prediction.get("actions", []))
            ),
            "forbidden_claims": bool(prediction) and not set(case["gold"]["forbidden_claims"]).intersection(
                set(prediction.get("claims", []))
            ),
        }
        family = case["task_family"]
        split = str(case.get("split") or "unknown")
        split_case_counts[split] += 1
        for name, ok in checks.items():
            totals[name] += 1
            by_family[family][f"{name}_total"] += 1
            by_split[split][f"{name}_total"] += 1
            if ok:
                passed[name] += 1
                by_family[family][f"{name}_passed"] += 1
                by_split[split][f"{name}_passed"] += 1
        if not all(checks.values()):
            failures.append({"id": case["id"], "split": case["split"], "checks": checks})

    metrics = {name: safe_div(passed[name], total) for name, total in totals.items()}
    family_metrics: dict[str, dict[str, float]] = {}
    for family, counts in by_family.items():
        family_metrics[family] = {
            name: safe_div(counts[f"{name}_passed"], counts[f"{name}_total"])
            for name in totals
        }
    split_metrics: dict[str, dict[str, float]] = {}
    for split, counts in by_split.items():
        split_metrics[split] = {
            name: safe_div(counts[f"{name}_passed"], counts[f"{name}_total"])
            for name in totals
        }
    return {
        "gold_cases": len(gold),
        "prediction_rows": len(predictions),
        "duplicate_prediction_ids": duplicate_count,
        "metrics": metrics,
        "by_family": family_metrics,
        "by_split": split_metrics,
        "split_counts": dict(sorted(split_case_counts.items())),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--goldset", type=Path, default=GOLDSET)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(read_jsonl(args.goldset), read_jsonl(args.predictions))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not report["failures"] and report["duplicate_prediction_ids"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
