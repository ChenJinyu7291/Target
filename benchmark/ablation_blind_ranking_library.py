"""Disease-library-scale ablation x blind-ranking matrix (synthetic fixture).

Extends benchmark/ablation_blind_ranking.py from 3 hand-built cases to all 18
diseases in configs/disease_library.yaml. Synthetic labels are DERIVED from the
library's evidence-graded reference_targets (approved_drug > gwas > mendelian >
clinical_trial > mechanistic); per benchmark/rubric.md the public library is
train/dev sanity data, so every number here is a development measurement with
`adjudication.status = synthetic_fixture` — never an external blind result.

Per disease the synthetic evidence is built so that each ablation arm has a
chance to move ranking metrics: reference targets receive evidence matching
their curated grade (approved_drug: genetics + omics + measured perturbation +
known drug; gwas/mendelian: genetics-anchored; clinical_trial: omics + drug;
mechanistic: literature only), plus one trap decoy (strong opposing evidence,
must not GO) and one disease-mismatched omics item (context-gate negative
control readout).

Usage:
    python benchmark/ablation_blind_ranking_library.py \
        [--out benchmark/results_ablation_blind_library] [--sets baseline,all]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmark"))

from ablation_blind_ranking import (  # noqa: E402
    METRIC_KEYS,
    THRESHOLDS,
    _literature,
    _mismatched_omics,
    _omics,
    _open_targets,
    _opposing,
    _perturb,
    _safety,
    _strict_genetics,
)
from target_agent.ablations import VALID, AblationConfig, category_of  # noqa: E402
from target_agent.blind_benchmark import (  # noqa: E402
    BlindBenchmarkManifest,
    BlindLabelSet,
    RankingThresholds,
    bundle_sha256,
    evaluate_benchmark,
    file_sha256,
)
from target_agent.contracts import TaskContext, TerminalStatus  # noqa: E402
from target_agent.ranking import rank_targets  # noqa: E402

LIBRARY_PATH = ROOT / "configs" / "disease_library.yaml"

GRADE_BY_EVIDENCE = {
    "approved_drug": 3,
    "gwas": 2,
    "mendelian": 2,
    "clinical_trial": 1,
    "mechanistic": 1,
}

BENCHMARK_ID = "target-dev-synthetic-library"
SPLIT_ID = "library-demo"


def load_library() -> list[dict]:
    raw = yaml.safe_load(LIBRARY_PATH.read_text(encoding="utf-8"))
    return list(raw["diseases"])


def build_labels(diseases: list[dict]) -> dict:
    """Derive the synthetic label set from curated reference targets."""
    cases = []
    for entry in diseases:
        targets = entry["reference_targets"]
        relevance = [
            {"gene": t["gene"], "grade": GRADE_BY_EVIDENCE[t["evidence"]],
             "source_ids": [f"library-{entry['id']}-{t['evidence']}"]}
            for t in targets
        ]
        approved = next((t for t in targets if t["evidence"] == "approved_drug"), None)
        safety = [{
            "gene": approved["gene"],
            "allowed_decisions": ["CONDITIONAL_GO", "NO_GO", "INSUFFICIENT_EVIDENCE"],
            "required_blocker_terms": ["liability"],
            "source_ids": [f"library-{entry['id']}-safety"],
        }] if approved else []
        cases.append({
            "case_id": f"lib-{entry['id']}",
            "judgment_pool_id": f"pool-library-{entry['id']}",
            "unjudged_policy": "treat_as_nonrelevant",
            "relevance": relevance,
            "trap_targets": [{"gene": f"TRAP_{entry['id'].upper()}",
                              "expected_behavior": "do_not_go"}],
            "safety_expectations": safety,
        })
    return {
        "contract_version": "1.0.0",
        "benchmark_id": BENCHMARK_ID,
        "split_id": SPLIT_ID,
        "adjudication": {
            "status": "synthetic_fixture",
            "reviewer_count": 2,
            "reviewers_blinded": True,
            "evidence_cutoff": "2026-08-14",
            "source_snapshot_ids": ["disease_library.yaml@1.0.0"],
        },
        "cases": cases,
    }


def build_case_inputs(entry: dict) -> dict:
    """Synthetic evidence per disease; each ablation arm can move the ranking."""
    disease = entry["name"]
    tissue = (entry.get("context") or {}).get("tissue") or "whole blood"
    candidates: list[str] = []
    evidence = []
    results = []
    safety_done = False
    for target in entry["reference_targets"]:
        gene, grade = target["gene"], target["evidence"]
        candidates.append(gene)
        if grade == "approved_drug":
            evidence += [_strict_genetics(gene, disease, tissue),
                         _omics(gene, disease, tissue),
                         _perturb(gene, disease, tissue)]
            results.append(_open_targets(gene, disease, 4))
            if not safety_done:
                evidence.append(_safety(gene, disease, tissue, f"{gene} class-wide liability"))
                safety_done = True
        elif grade == "gwas":
            # genetics-anchored: the no_human_genetics arm must move these
            evidence += [_strict_genetics(gene, disease, tissue),
                         _omics(gene, disease, tissue, strength=0.5)]
        elif grade == "mendelian":
            evidence += [_strict_genetics(gene, disease, tissue),
                         _literature(gene, disease, tissue)]
        elif grade == "clinical_trial":
            evidence.append(_omics(gene, disease, tissue))
            results.append(_open_targets(gene, disease, 2))
        else:  # mechanistic
            evidence.append(_literature(gene, disease, tissue))
    trap = f"TRAP_{entry['id'].upper()}"
    candidates.append(trap)
    evidence += [_omics(trap, disease, tissue), _opposing(trap, disease, tissue)]
    mismatch = f"MISMATCH_{entry['id'].upper()}"
    candidates.append(mismatch)
    evidence.append(_mismatched_omics(mismatch, "synthetic unrelated disease (dev fixture)", tissue))
    return {"candidates": candidates, "evidence": evidence, "results": results,
            "disease": disease, "tissue": tissue}


def run_arm(arm: str, diseases: list[dict], labels: BlindLabelSet, out_root: Path) -> dict:
    config = AblationConfig() if arm == "baseline" else AblationConfig(
        frozenset(VALID) if arm == "all" else frozenset({arm}))
    runs_root = out_root / arm / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    mismatched_admitted = False
    manifest_cases = []
    for entry in diseases:
        case_id = f"lib-{entry['id']}"
        run_id = f"run-{arm}-{case_id}"
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        spec = build_case_inputs(entry)
        mismatched_ids = {item.evidence_id for item in spec["evidence"]
                          if item.tool_run_id.startswith("tr-mismatch-")}
        ranked = rank_targets(
            spec["candidates"], spec["evidence"], spec["results"], findings=[],
            task_context=TaskContext(disease=spec["disease"], tissue=spec["tissue"]),
            terminal_status=TerminalStatus.COMPLETED, ablations=config,
        )
        rows = [
            {"gene": row.gene, "decision": row.decision, "safety_blockers": row.safety_blockers,
             "rank": index,
             "scores": {"human_genetics": row.scores.human_genetics,
                        "disease_omics": row.scores.disease_omics,
                        "perturbation": row.scores.perturbation,
                        "mechanism": row.scores.mechanism,
                        "druggability": row.scores.druggability,
                        "safety_translation": row.scores.safety_translation,
                        "total": row.scores.total}}
            for index, row in enumerate(ranked, start=1)
        ]
        admitted_ids = {eid for row in ranked for eid in row.evidence_ids}
        if mismatched_ids & admitted_ids and "no_context_gate" in config:
            mismatched_admitted = True
        task = {
            "contract_version": "2.2.0", "task_type": "disease_to_target",
            "question": f"Discover targets for {spec['disease']}",
            "context": {"disease": spec["disease"], "disease_id": entry["ontology_id"],
                        "tissue": spec["tissue"], "cell_type": None},
        }
        (run_dir / "task_spec.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "ranked_targets.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "status.json").write_text(json.dumps(
            {"terminal_status": "completed", "run_id": run_id, "case_id": case_id},
            ensure_ascii=False, indent=2), encoding="utf-8")
        digests = tuple(file_sha256(run_dir / name)
                        for name in ("task_spec.json", "ranked_targets.json", "status.json"))
        manifest_cases.append({
            "case_id": case_id, "disease_group_id": entry["ontology_id"], "run_id": run_id,
            "task_sha256": digests[0], "ranking_sha256": digests[1], "status_sha256": digests[2],
            "bundle_sha256": bundle_sha256(*digests),
        })
    manifest = BlindBenchmarkManifest(
        benchmark_id=BENCHMARK_ID, split_id=SPLIT_ID, k=10,
        thresholds=RankingThresholds(**THRESHOLDS),
        require_expert_adjudication=False, cases=manifest_cases,
    )
    manifest_path = out_root / arm / "blind_manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    report = evaluate_benchmark(manifest, labels, runs_root)
    summary = report["summary"]
    return {
        "arm": arm,
        "category": "baseline" if arm == "baseline" else (
            "all_combined" if arm == "all" else category_of(arm)),
        "ablations": sorted(config.switches) or "none",
        **{key: summary[key] for key in METRIC_KEYS},
        "gates_passed": report["passed"],
        "mismatched_evidence_admitted": mismatched_admitted,
        "manifest_sha256": file_sha256(manifest_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=ROOT / "benchmark" / "results_ablation_blind_library")
    parser.add_argument("--sets", type=str,
                        default=",".join(["baseline", *sorted(VALID), "all"]))
    args = parser.parse_args()
    arms = [part.strip() for part in args.sets.split(",") if part.strip()]
    for arm in arms:
        if arm not in ("baseline", "all"):
            AblationConfig(frozenset({arm}))

    diseases = load_library()
    labels_payload = build_labels(diseases)
    args.out.mkdir(parents=True, exist_ok=True)
    labels_path = args.out / "synthetic_labels.json"
    labels_path.write_text(json.dumps(labels_payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    labels = BlindLabelSet.model_validate(labels_payload)
    if labels.adjudication.status != "synthetic_fixture":
        raise ValueError("library blind matrix is synthetic-only by construction")

    rows = []
    for arm in arms:
        print(f"[ablation-blind-library] {arm} ...", flush=True)
        rows.append(run_arm(arm, diseases, labels, args.out))
        print(f"[ablation-blind-library] {arm} -> nDCG {rows[-1]['disease_macro_ndcg_at_k']}",
              flush=True)

    baseline = next((row for row in rows if row["arm"] == "baseline"), None)
    for row in rows:
        if baseline:
            row["delta_ndcg_vs_baseline"] = round(
                (baseline["disease_macro_ndcg_at_k"] or 0) - (row["disease_macro_ndcg_at_k"] or 0), 6)

    payload = {
        "fixture": "synthetic labels derived from the public disease library (train/dev sanity "
                   "data per benchmark/rubric.md); NOT an external blind biological result",
        "diseases": len(diseases),
        "method": "Each arm re-ranks identical synthetic per-disease evidence with the real "
                  "rank_targets scorer under an evaluation-only AblationConfig; artifacts are "
                  "digest-frozen per arm and scored with the BM-14 blind-ranking protocol.",
        "rows": rows,
    }
    (args.out / "ablation_blind_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Ablation × Blind-Ranking Matrix — disease library (synthetic fixture)", "",
        f"{len(diseases)} diseases from configs/disease_library.yaml; synthetic labels derived "
        "from curated reference_targets (train/dev sanity data per rubric; not an external "
        "blind result). `no_context_gate` readout is the mismatched-evidence admission flag; "
        "model-component arms do not fire in this in-process scorer fixture.", "",
        "| Arm | Category | nDCG@K | Δ nDCG | Recall@K | MRR@K | Trap | Safety recall | Unsafe GO | Gates | Mismatch admitted |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['arm']} | {row['category']} | {row['disease_macro_ndcg_at_k']} "
            f"| {row.get('delta_ndcg_vs_baseline', 0)} | {row['disease_macro_recall_at_k']} "
            f"| {row['disease_macro_mrr_at_k']} | {row['trap_case_rate']} "
            f"| {row['disease_macro_safety_blocker_recall']} | {row['disease_macro_unsafe_go_rate']} "
            f"| {'PASS' if row['gates_passed'] else 'FAIL'} | {row['mismatched_evidence_admitted']} |"
        )
    (args.out / "ablation_blind_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({row["arm"]: row["disease_macro_ndcg_at_k"] for row in rows},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
