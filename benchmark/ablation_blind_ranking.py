"""Ablation x blind-ranking matrix on the synthetic development fixture.

Mid-term review instrument (2026-08-14): instead of reading mechanism ablations
through goldset assertion deltas (descriptive only — the goldset partially
asserts the mechanisms themselves), each ablation arm re-runs the REAL
`rank_targets` scorer on identical frozen synthetic evidence and is then scored
with the blind-ranking protocol (`benchmark/blind_ranking.py` /
`benchmark/rubric.md`): disease-macro nDCG@K, Recall@K, MRR@K plus the
non-compensating trap / safety / unsafe-GO gates.

Scope guardrails (same as demo_blind_ranking.py):
- Labels are `blind_demo_labels.json` with `adjudication.status = synthetic_fixture`.
  This is a protocol/mechanism measurement on synthetic data, NEVER an external
  blind biological result; expert-adjudicated labels remain required for any
  release claim.
- `no_context_gate` is a safety negative control: its readout is the
  mismatched-evidence admission flag, not a ranking delta.
- Model-component arms (no_reviewer_llm / no_planner_llm) do not fire inside
  this in-process scorer fixture; their zero deltas are reported as
  not-measured-here, not as evidence of no contribution.

Usage:
    python benchmark/ablation_blind_ranking.py [--out benchmark/results_ablation_blind]
                                               [--sets baseline,no_human_genetics,...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from target_agent.ablations import VALID, AblationConfig, category_of  # noqa: E402
from target_agent.blind_benchmark import (  # noqa: E402
    BlindBenchmarkManifest,
    BlindLabelSet,
    RankingThresholds,
    bundle_sha256,
    evaluate_benchmark,
    file_sha256,
)
from target_agent.contracts import (  # noqa: E402
    ClaimClass,
    CoverageStatus,
    EvidenceContext,
    EvidenceItem,
    GeneticEvidencePayload,
    SourceLocator,
    Stance,
    TaskContext,
    TerminalStatus,
    ToolResult,
    ToolStatus,
)
from target_agent.ranking import rank_targets  # noqa: E402

LABELS_PATH = Path(__file__).with_name("blind_demo_labels.json")

THRESHOLDS = {
    "min_ndcg_at_k": 0.6,
    "min_recall_at_k": 0.7,
    "min_mrr_at_k": 0.6,
    "max_trap_case_rate": 0.0,
    "min_safety_blocker_recall": 1.0,
    "max_unsafe_go_rate": 0.0,
}

D1 = "synthetic inflammatory bowel disease (dev fixture)"
D2 = "synthetic neurodegeneration disease (dev fixture)"
D3 = "synthetic cardio-metabolic disease (dev fixture)"


def _ctx(disease: str, tissue: str, **extra) -> EvidenceContext:
    return EvidenceContext(disease=disease, tissue=tissue, **extra)


def _item(gene: str, context: EvidenceContext, effect: dict, *,
          claim: ClaimClass = ClaimClass.OBSERVED, stance: Stance = Stance.SUPPORTS,
          uri: str = "https://tool.example/x", version: str | None = None,
          effect_direction: str = "unclear", run: str,
          genetic: GeneticEvidencePayload | None = None) -> EvidenceItem:
    return EvidenceItem(
        tool_run_id=run, gene_symbol=gene, claim_class=claim,
        statement=f"{gene} synthetic fixture statement",
        source=SourceLocator(uri=uri, source_id=f"src-{gene}-{run}", version=version),
        source_span=f"synthetic span for {gene}", context=context, stance=stance,
        effect=effect, uncertainty="synthetic fixture evidence", context_match_score=1.0,
        effect_direction=effect_direction, genetic_evidence=genetic,
    )


def _strict_genetics(gene: str, disease: str, tissue: str, pp4: float = 0.9) -> EvidenceItem:
    context = _ctx(disease, tissue, genome_build="GRCh38", ancestry="EUR",
                   study_id=f"GWAS-{gene}", locus_id="L1", signal_id="CS1")
    return _item(
        gene, context, {}, run=f"tr-gwas-{gene}",
        genetic=GeneticEvidencePayload(
            evidence_type="colocalization", analysis_level="colocalization_supported",
            study_id=f"GWAS-{gene}", molecular_study_id=f"EQTL-{gene}",
            locus_id="L1", signal_id="CS1", gene_symbol=gene,
            method="coloc_susie", method_version="synthetic-fixture",
            strength=pp4, formal_score_eligible=True,
        ),
    )


def _omics(gene: str, disease: str, tissue: str, strength: float = 0.7) -> EvidenceItem:
    return _item(gene, _ctx(disease, tissue), {"omics_strength": strength}, run=f"tr-omics-{gene}")


def _perturb(gene: str, disease: str, tissue: str, *, measured: bool = True) -> EvidenceItem:
    return _item(
        gene, _ctx(disease, tissue), {"disease_alignment": 0.2}, run=f"tr-perturb-{gene}",
        claim=ClaimClass.OBSERVED if measured else ClaimClass.PREDICTED,
    )


def _literature(gene: str, disease: str, tissue: str) -> EvidenceItem:
    return _item(
        gene, _ctx(disease, tissue), {}, run=f"tr-lit-{gene}", claim=ClaimClass.FACT,
        uri=f"https://europepmc.org/article/MED/{gene}", version="synthetic-corpus-2026-08",
        effect_direction="increase",
    )


def _safety(gene: str, disease: str, tissue: str, event: str) -> EvidenceItem:
    return _item(gene, _ctx(disease, tissue), {"safety": {"event": event}}, run=f"tr-safety-{gene}")


def _opposing(gene: str, disease: str, tissue: str) -> EvidenceItem:
    return _item(
        gene, _ctx(disease, tissue), {}, run=f"tr-oppose-{gene}", claim=ClaimClass.OBSERVED,
        stance=Stance.REFUTES, effect_direction="increase",
    )


def _mismatched_omics(gene: str, wrong_disease: str, tissue: str) -> EvidenceItem:
    """Disease-mismatched evidence: recomputed context hard-caps at 0.40 (< 0.5 gate)."""
    return _item(gene, _ctx(wrong_disease, tissue), {"omics_strength": 0.9}, run=f"tr-mismatch-{gene}")


def _open_targets(gene: str, disease: str, phase: int) -> ToolResult:
    return ToolResult(
        tool_run_id=f"tr-ot-{gene}", tool_name="open_targets", tool_version="synthetic",
        status=ToolStatus.SUCCESS, coverage_status=CoverageStatus.COVERED,
        context_match_score=1.0, inputs={}, capability={},
        outputs={"associations": [{"gene": gene, "known_drugs": [{"phase": phase}], "tractability": []}]},
    )


# case_id -> (disease_group_id, disease, tissue, candidates, evidence builder)
CASES = {
    "opaque-001": {
        "group": "SYNTH_DISEASE_001", "disease": D1, "tissue": "colon",
        "candidates": ["IL12B", "JAK2", "TYK2", "TNFSF15", "NOD2"],
        "evidence": lambda: [
            _strict_genetics("IL12B", D1, "colon"),
            _omics("IL12B", D1, "colon"),
            _perturb("IL12B", D1, "colon"),
            _safety("IL12B", D1, "colon", "infection risk"),
            _omics("JAK2", D1, "colon"),
            _perturb("JAK2", D1, "colon"),
            _literature("JAK2", D1, "colon"),
            _omics("TYK2", D1, "colon"),
            _perturb("TYK2", D1, "colon", measured=False),
            _literature("TYK2", D1, "colon"),
            _omics("TNFSF15", D1, "colon"),
            _mismatched_omics("NOD2", D2, "colon"),
        ],
        "results": lambda: [_open_targets("JAK2", D1, 3)],
    },
    "opaque-002": {
        "group": "SYNTH_DISEASE_002", "disease": D2, "tissue": "brain",
        "candidates": ["TREM2", "APOE", "MS4A6A", "SPI1", "ADAM17"],
        "evidence": lambda: [
            _strict_genetics("TREM2", D2, "brain"),
            _omics("TREM2", D2, "brain"),
            _perturb("TREM2", D2, "brain"),
            _omics("APOE", D2, "brain"),
            _perturb("APOE", D2, "brain"),
            _omics("MS4A6A", D2, "brain"),
            _literature("MS4A6A", D2, "brain"),
            _omics("SPI1", D2, "brain"),
            _omics("ADAM17", D2, "brain"),
            _opposing("ADAM17", D2, "brain"),
        ],
        "results": lambda: [_open_targets("APOE", D2, 3)],
    },
    "opaque-003": {
        "group": "SYNTH_DISEASE_003", "disease": D3, "tissue": "liver",
        "candidates": ["GENE_A", "GENE_B", "GENE_C", "GENE_D"],
        "evidence": lambda: [
            _strict_genetics("GENE_A", D3, "liver"),
            _omics("GENE_A", D3, "liver"),
            _omics("GENE_B", D3, "liver"),
            _perturb("GENE_B", D3, "liver"),
            _omics("GENE_C", D3, "liver"),
            _safety("GENE_C", D3, "liver", "cardiac liability"),
            _mismatched_omics("GENE_D", D1, "liver"),
        ],
        "results": lambda: [],
    },
}

ARMS = ["baseline", *sorted(VALID), "all"]

METRIC_KEYS = (
    "disease_macro_ndcg_at_k", "disease_macro_recall_at_k", "disease_macro_mrr_at_k",
    "trap_case_rate", "disease_macro_safety_blocker_recall", "disease_macro_unsafe_go_rate",
)


def run_arm(arm: str, out_root: Path, labels: BlindLabelSet) -> dict:
    """Re-rank every case with the real scorer under this arm, freeze, then score."""
    config = AblationConfig() if arm == "baseline" else AblationConfig(
        frozenset(VALID) if arm == "all" else frozenset({arm}))
    runs_root = out_root / arm / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    mismatched_admitted = False
    manifest_cases = []
    for case_id, spec in CASES.items():
        run_id = f"run-{arm}-{case_id}"
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        evidence = spec["evidence"]()
        mismatched_ids = {item.evidence_id for item in evidence if item.tool_run_id.startswith("tr-mismatch-")}
        ranked = rank_targets(
            spec["candidates"], evidence, spec["results"](),
            findings=[], task_context=TaskContext(disease=spec["disease"], tissue=spec["tissue"]),
            terminal_status=TerminalStatus.COMPLETED, ablations=config,
        )
        rows = [
            {"gene": row.gene, "decision": row.decision, "safety_blockers": row.safety_blockers,
             "rank": index, "scores": {
                 "human_genetics": row.scores.human_genetics, "disease_omics": row.scores.disease_omics,
                 "perturbation": row.scores.perturbation, "mechanism": row.scores.mechanism,
                 "druggability": row.scores.druggability, "safety_translation": row.scores.safety_translation,
                 "total": row.scores.total}}
            for index, row in enumerate(ranked, start=1)
        ]
        admitted_ids = {eid for row in ranked for eid in row.evidence_ids}
        if mismatched_ids & admitted_ids and "no_context_gate" in config:
            mismatched_admitted = True
        task = {
            "contract_version": "2.2.0", "task_type": "disease_to_target",
            "question": f"Discover targets for {spec['disease']}",
            "context": {"disease": spec["disease"], "disease_id": spec["group"],
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
            "case_id": case_id, "disease_group_id": spec["group"], "run_id": run_id,
            "task_sha256": digests[0], "ranking_sha256": digests[1], "status_sha256": digests[2],
            "bundle_sha256": bundle_sha256(*digests),
        })
    manifest = BlindBenchmarkManifest(
        benchmark_id="target-dev-synthetic", split_id="demo", k=10,
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
    parser.add_argument("--out", type=Path, default=ROOT / "benchmark" / "results_ablation_blind")
    parser.add_argument("--sets", type=str, default=",".join(ARMS),
                        help="comma-separated: baseline, any valid switch, or 'all'")
    args = parser.parse_args()
    arms = [part.strip() for part in args.sets.split(",") if part.strip()]
    for arm in arms:
        if arm not in ("baseline", "all"):
            AblationConfig(frozenset({arm}))  # raises on unknown switches before any run

    labels = BlindLabelSet.model_validate_json(LABELS_PATH.read_text(encoding="utf-8"))
    if labels.adjudication.status != "synthetic_fixture":
        raise ValueError("ablation blind matrix requires the synthetic fixture labels; "
                         "expert labels must stay outside this repository")
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for arm in arms:
        print(f"[ablation-blind] {arm} ...", flush=True)
        rows.append(run_arm(arm, args.out, labels))
        print(f"[ablation-blind] {arm} -> nDCG {rows[-1]['disease_macro_ndcg_at_k']}", flush=True)

    baseline = next((row for row in rows if row["arm"] == "baseline"), None)
    for row in rows:
        if baseline:
            row["delta_ndcg_vs_baseline"] = round(
                (baseline["disease_macro_ndcg_at_k"] or 0) - (row["disease_macro_ndcg_at_k"] or 0), 6)

    payload = {
        "fixture": "synthetic development fixture (blind_demo_labels.json); NOT an external "
                   "blind biological result — expert-adjudicated labels remain required for "
                   "release claims",
        "method": "Each arm re-ranks identical frozen synthetic evidence with the real "
                  "rank_targets scorer under an evaluation-only AblationConfig, digests are "
                  "frozen per arm, then the blind-ranking scorer computes disease-macro "
                  "nDCG@K / Recall@K / MRR@K and the non-compensating trap/safety gates.",
        "rows": rows,
    }
    (args.out / "ablation_blind_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Ablation × Blind-Ranking Matrix (synthetic development fixture)", "",
        "Synthetic labels only; this measures mechanism behavior through ranking metrics,",
        "not biological performance. `no_context_gate` is a safety negative control — read",
        "its mismatched-evidence admission flag, not its ranking delta. Model-component arms",
        "do not fire in this in-process scorer fixture (zero delta = not measured here).", "",
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
    print(json.dumps({row["arm"]: row["disease_macro_ndcg_at_k"] for row in rows}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
