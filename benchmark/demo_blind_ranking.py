"""Synthetic development fixture for the blind target-ranking protocol.

This demo never claims biological performance. It exercises the full
freeze -> score pipeline with clearly synthetic (non-expert) labels so the
protocol, digest binding and non-compensating safety gates can be validated
automatically in CI. External expert-adjudicated labels are required for any
real release claim and must live outside this repository.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from target_agent.blind_benchmark import (  # noqa: E402
    BlindBenchmarkManifest,
    BlindLabelSet,
    RankingThresholds,
    bundle_sha256,
    evaluate_benchmark,
    file_sha256,
    public_report,
    render_markdown,
)

DEFAULT_LABELS = Path(__file__).with_name("blind_demo_labels.json")

# case_id -> (disease_group_id, run_id, disease name, ranked targets)
DEMO_CASES = {
    "opaque-001": (
        "SYNTH_DISEASE_001",
        "run-opaque-001",
        "synthetic inflammatory bowel disease (dev fixture)",
        [
            {"gene": "IL12B", "decision": "CONDITIONAL_GO", "safety_blockers": ["infection risk"]},
            {"gene": "JAK2", "decision": "GO", "safety_blockers": []},
            {"gene": "TYK2", "decision": "GO", "safety_blockers": []},
            {"gene": "TNFSF15", "decision": "INSUFFICIENT_EVIDENCE", "safety_blockers": []},
            {"gene": "NOD2", "decision": "INSUFFICIENT_EVIDENCE", "safety_blockers": []},
        ],
    ),
    "opaque-002": (
        "SYNTH_DISEASE_002",
        "run-opaque-002",
        "synthetic neurodegeneration disease (dev fixture)",
        [
            {"gene": "TREM2", "decision": "GO", "safety_blockers": []},
            {"gene": "APOE", "decision": "GO", "safety_blockers": []},
            {"gene": "MS4A6A", "decision": "CONDITIONAL_GO", "safety_blockers": []},
            {"gene": "ADAM17", "decision": "NO_GO", "safety_blockers": ["trap target, no causal evidence"]},
            {"gene": "SPI1", "decision": "INSUFFICIENT_EVIDENCE", "safety_blockers": []},
        ],
    ),
    "opaque-003": (
        "SYNTH_DISEASE_003",
        "run-opaque-003",
        "synthetic cardio-metabolic disease (dev fixture)",
        [
            {"gene": "GENE_A", "decision": "GO", "safety_blockers": []},
            {"gene": "GENE_B", "decision": "GO", "safety_blockers": []},
            {"gene": "GENE_C", "decision": "NO_GO", "safety_blockers": ["cardiac liability"]},
            {"gene": "GENE_D", "decision": "INSUFFICIENT_EVIDENCE", "safety_blockers": []},
        ],
    ),
}

THRESHOLDS = {
    "min_ndcg_at_k": 0.6,
    "min_recall_at_k": 0.7,
    "min_mrr_at_k": 0.6,
    "max_trap_case_rate": 0.0,
    "min_safety_blocker_recall": 1.0,
    "max_unsafe_go_rate": 0.0,
}

_K = 10


def _write_artifacts(runs_root: Path) -> None:
    for case_id, (group_id, run_id, disease_name, ranking) in DEMO_CASES.items():
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        task = {
            "contract_version": "2.2.0",
            "task_type": "disease_to_target",
            "question": f"Discover targets for {disease_name}",
            "context": {"disease": disease_name, "disease_id": group_id, "tissue": None, "cell_type": None},
        }
        (run_dir / "task_spec.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (run_dir / "ranked_targets.json").write_text(
            json.dumps(ranking, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        status = {"terminal_status": "completed_with_gaps", "run_id": run_id, "case_id": case_id}
        (run_dir / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _freeze_manifest(runs_root: Path) -> BlindBenchmarkManifest:
    cases = []
    for case_id, (group_id, run_id, _name, _ranking) in DEMO_CASES.items():
        run_dir = runs_root / run_id
        task_digest = file_sha256(run_dir / "task_spec.json")
        ranking_digest = file_sha256(run_dir / "ranked_targets.json")
        status_digest = file_sha256(run_dir / "status.json")
        cases.append({
            "case_id": case_id, "disease_group_id": group_id, "run_id": run_id,
            "task_sha256": task_digest, "ranking_sha256": ranking_digest,
            "status_sha256": status_digest,
            "bundle_sha256": bundle_sha256(task_digest, ranking_digest, status_digest),
        })
    return BlindBenchmarkManifest(
        benchmark_id="target-dev-synthetic", split_id="demo", k=_K,
        thresholds=RankingThresholds(**THRESHOLDS),
        require_expert_adjudication=False,  # synthetic development fixture only
        cases=cases,
    )


def run_demo(out_dir: Path, labels_path: Path | None = None) -> dict:
    """Run freeze -> score end-to-end and return the public report dict."""
    labels_path = labels_path or DEFAULT_LABELS
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="target-blind-demo-") as tmp:
        runs_root = Path(tmp) / "runs"
        runs_root.mkdir(parents=True, exist_ok=True)
        _write_artifacts(runs_root)
        manifest = _freeze_manifest(runs_root)
        manifest_path = out_dir / "blind_manifest.json"
        manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

        labels = BlindLabelSet.model_validate_json(labels_path.read_text(encoding="utf-8"))
        if labels.adjudication.status != "synthetic_fixture":
            raise ValueError("demo requires a synthetic_fixture label set; refusing to run with expert labels")
        report = evaluate_benchmark(manifest, labels, runs_root)
        public = public_report(report)
        public["manifest_sha256"] = file_sha256(manifest_path)
        public["labels_sha256"] = file_sha256(labels_path)
        (out_dir / "blind_ranking_report.json").write_text(
            json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "blind_ranking_report.md").write_text(
            render_markdown(public), encoding="utf-8"
        )
        (out_dir / "blind_ranking_audit.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return public


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("benchmark/results/blind_demo"))
    parser.add_argument("--labels", type=Path, default=None,
                        help="override synthetic label fixture (must still be synthetic_fixture)")
    args = parser.parse_args()
    print("NOTE: synthetic development fixture only; not an expert-adjudicated biological result.")
    public = run_demo(args.out, args.labels)
    print(json.dumps(public["summary"], ensure_ascii=False, indent=2))
    print(json.dumps(public["gates"], ensure_ascii=False, indent=2))
    print("BLIND_DEMO_PASSED=" + ("OK" if public["passed"] else "FAIL"))
    return 0 if public["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())