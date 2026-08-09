"""Reference contracts and metrics for post-hoc target-ranking evaluation.

This module never invokes an Agent runtime. It is still participant-owned code,
so an official final benchmark must execute an independently controlled scorer
and must not mount private labels in the Agent environment.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator


BLIND_BENCHMARK_CONTRACT_VERSION = "1.0.0"
_FORBIDDEN_TASK_KEYS = {
    "expected_targets", "gold", "gold_labels", "labels", "positives",
    "reference_targets", "relevance", "safety_expectations", "trap_targets",
}
_TERMINAL_STATUSES = {"completed", "completed_with_gaps"}


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RankingThresholds(BenchmarkModel):
    min_ndcg_at_k: float = Field(ge=0.0, le=1.0)
    min_recall_at_k: float = Field(ge=0.0, le=1.0)
    min_mrr_at_k: float = Field(ge=0.0, le=1.0)
    max_trap_case_rate: float = Field(ge=0.0, le=1.0)
    min_safety_blocker_recall: float = Field(ge=0.0, le=1.0)
    max_unsafe_go_rate: float = Field(ge=0.0, le=1.0)


class BlindCaseManifest(BenchmarkModel):
    case_id: str = Field(min_length=1)
    disease_group_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ranking_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BlindBenchmarkManifest(BenchmarkModel):
    contract_version: Literal[BLIND_BENCHMARK_CONTRACT_VERSION] = BLIND_BENCHMARK_CONTRACT_VERSION
    benchmark_id: str = Field(min_length=1)
    split_id: str = Field(min_length=1)
    k: int = Field(default=10, ge=1, le=100)
    require_expert_adjudication: bool = True
    thresholds: RankingThresholds
    cases: list[BlindCaseManifest] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases_and_runs(self) -> "BlindBenchmarkManifest":
        for values, name in (([row.case_id for row in self.cases], "case_id"),
                             ([row.run_id for row in self.cases], "run_id")):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {name} in benchmark manifest")
        return self


class TargetRelevance(BenchmarkModel):
    gene: str = Field(min_length=1)
    grade: int = Field(ge=0, le=3)
    source_ids: list[str] = Field(min_length=1)


class TrapExpectation(BenchmarkModel):
    gene: str = Field(min_length=1)
    expected_behavior: Literal["do_not_go"] = "do_not_go"


class SafetyExpectation(BenchmarkModel):
    gene: str = Field(min_length=1)
    allowed_decisions: list[Literal["CONDITIONAL_GO", "NO_GO", "INSUFFICIENT_EVIDENCE"]] = Field(
        min_length=1
    )
    required_blocker_terms: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class BlindCaseLabels(BenchmarkModel):
    case_id: str = Field(min_length=1)
    judgment_pool_id: str = Field(min_length=1)
    unjudged_policy: Literal["treat_as_nonrelevant"]
    relevance: list[TargetRelevance] = Field(min_length=1)
    trap_targets: list[TrapExpectation] = Field(default_factory=list)
    safety_expectations: list[SafetyExpectation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_genes(self) -> "BlindCaseLabels":
        groups = (
            ([_gene(row.gene) for row in self.relevance], "relevance"),
            ([_gene(row.gene) for row in self.trap_targets], "trap_targets"),
            ([_gene(row.gene) for row in self.safety_expectations], "safety_expectations"),
        )
        for values, name in groups:
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate gene in {name}")
        if not any(row.grade >= 2 for row in self.relevance):
            raise ValueError("each case requires at least one relevance grade >= 2 target")
        return self


class LabelAdjudication(BenchmarkModel):
    status: Literal["synthetic_fixture", "expert_adjudicated"]
    reviewer_count: int = Field(ge=0)
    reviewers_blinded: bool
    evidence_cutoff: date
    source_snapshot_ids: list[str] = Field(default_factory=list)


class BlindLabelSet(BenchmarkModel):
    contract_version: Literal[BLIND_BENCHMARK_CONTRACT_VERSION] = BLIND_BENCHMARK_CONTRACT_VERSION
    benchmark_id: str = Field(min_length=1)
    split_id: str = Field(min_length=1)
    adjudication: LabelAdjudication
    cases: list[BlindCaseLabels] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_adjudication(self) -> "BlindLabelSet":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("duplicate case_id in label set")
        if self.adjudication.status == "expert_adjudicated":
            if self.adjudication.reviewer_count < 2:
                raise ValueError("expert-adjudicated labels require at least two reviewers")
            if not self.adjudication.reviewers_blinded:
                raise ValueError("expert-adjudicated labels require blinded reviewers")
            if not self.adjudication.source_snapshot_ids:
                raise ValueError("expert-adjudicated labels require frozen source snapshot IDs")
        return self


class RankingOutputRow(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    gene: StrictStr = Field(min_length=1)
    decision: Literal["GO", "CONDITIONAL_GO", "NO_GO", "INSUFFICIENT_EVIDENCE"]
    safety_blockers: list[StrictStr]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_sha256(task_digest: str, ranking_digest: str, status_digest: str) -> str:
    return hashlib.sha256(f"{task_digest}:{ranking_digest}:{status_digest}".encode()).hexdigest()


def _gene(value: str) -> str:
    gene = value.strip().upper()
    if not gene:
        raise ValueError("gene symbol cannot be empty")
    return gene


def _find_forbidden_keys(value: Any, path: str = "task") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{path}.{key}"
            if str(key).casefold() in _FORBIDDEN_TASK_KEYS:
                found.append(current)
            found.extend(_find_forbidden_keys(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_find_forbidden_keys(item, f"{path}[{index}]"))
    return found


def _dcg(grades: list[int]) -> float:
    return sum(((2 ** grade) - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades))


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 6) if present else None


def _safe_run_dir(runs_root: Path, run_id: str) -> Path:
    root = runs_root.resolve()
    candidate = (root / run_id).resolve()
    if candidate.parent != root:
        raise ValueError(f"run_id escapes runs root: {run_id!r}")
    return candidate


def evaluate_case(case: BlindCaseManifest, labels: BlindCaseLabels, runs_root: Path, k: int) -> dict[str, Any]:
    run_dir = _safe_run_dir(runs_root, case.run_id)
    task_path = run_dir / "task_spec.json"
    ranking_path = run_dir / "ranked_targets.json"
    status_path = run_dir / "status.json"
    if not all(path.is_file() for path in (task_path, ranking_path, status_path)):
        raise ValueError(f"case {case.case_id}: missing frozen task, ranking or status artifact")
    digests = (file_sha256(task_path), file_sha256(ranking_path), file_sha256(status_path))
    expected = (case.task_sha256, case.ranking_sha256, case.status_sha256)
    if digests != expected or bundle_sha256(*digests) != case.bundle_sha256:
        raise ValueError(f"case {case.case_id}: frozen run bundle digest mismatch")

    status = json.loads(status_path.read_text(encoding="utf-8"))
    if status.get("terminal_status") not in _TERMINAL_STATUSES:
        raise ValueError(f"case {case.case_id}: run is not in a scoreable terminal state")
    task = json.loads(task_path.read_text(encoding="utf-8"))
    leaked = _find_forbidden_keys(task)
    if leaked:
        raise ValueError(f"case {case.case_id}: evaluation labels leaked into TaskSpec at {leaked}")
    if task.get("contract_version") != "2.2.0" or task.get("task_type") != "disease_to_target":
        raise ValueError(f"case {case.case_id}: blind discovery requires TaskSpec 2.2.0 disease_to_target")
    if task.get("candidate_genes"):
        raise ValueError(f"case {case.case_id}: candidate_genes must be empty in blind discovery")
    disease_id = str((task.get("context") or {}).get("disease_id") or "").replace(":", "_")
    if not disease_id or disease_id.casefold() != case.disease_group_id.replace(":", "_").casefold():
        raise ValueError(f"case {case.case_id}: TaskSpec disease_id does not match disease group")

    payload = json.loads(ranking_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"case {case.case_id}: ranked_targets.json must contain a list")
    ranking = [RankingOutputRow.model_validate(row) for row in payload]
    predicted = [_gene(row.gene) for row in ranking]
    if len(predicted) != len(set(predicted)):
        raise ValueError(f"case {case.case_id}: ranking contains duplicate genes")
    top = predicted[:k]
    rows_by_gene = {_gene(row.gene): row for row in ranking[:k]}

    relevance = {_gene(row.gene): row.grade for row in labels.relevance}
    observed = [relevance.get(gene, 0) for gene in top]
    ideal = _dcg(sorted(relevance.values(), reverse=True)[:k])
    ndcg = _dcg(observed) / ideal if ideal else 0.0
    relevant = {gene for gene, grade in relevance.items() if grade >= 2}
    recall = len(set(top) & relevant) / len(relevant)
    first = next((rank for rank, gene in enumerate(top, start=1) if gene in relevant), None)

    traps = {_gene(row.gene) for row in labels.trap_targets}
    trap_hits = [gene for gene in top if gene in traps]
    trap_violations = [gene for gene in trap_hits if rows_by_gene[gene].decision == "GO"]

    safety_by_gene = {_gene(row.gene): row for row in labels.safety_expectations}
    retrieved_safety = [gene for gene in top if gene in safety_by_gene]
    correctly_blocked = 0
    unsafe_go = 0
    for gene in retrieved_safety:
        row = rows_by_gene[gene]
        expectation = safety_by_gene[gene]
        blocker_text = " ".join(row.safety_blockers).casefold()
        terms_match = all(term.casefold() in blocker_text for term in expectation.required_blocker_terms)
        if row.decision in expectation.allowed_decisions and terms_match:
            correctly_blocked += 1
        if row.decision == "GO" and "GO" not in expectation.allowed_decisions:
            unsafe_go += 1

    return {
        "case_id": case.case_id, "disease_group_id": case.disease_group_id, "run_id": case.run_id,
        "valid": True, "error_code": "no_candidates" if not predicted else None,
        "no_candidates": not predicted, "ranking_size": len(predicted),
        "ndcg_at_k": round(ndcg, 6), "recall_at_k": round(recall, 6),
        "mrr_at_k": round(1.0 / first if first else 0.0, 6),
        "trap_applicable": bool(traps), "trap_violation": bool(trap_violations),
        "trap_hits": trap_hits, "trap_violations": trap_violations,
        "safety_applicable": bool(retrieved_safety),
        "safety_blocker_recall": round(correctly_blocked / len(retrieved_safety), 6)
        if retrieved_safety else None,
        "unsafe_go_rate": round(unsafe_go / len(retrieved_safety), 6) if retrieved_safety else None,
    }


def _failed_case(case: BlindCaseManifest, labels: BlindCaseLabels, exc: Exception) -> dict[str, Any]:
    message = str(exc)
    if any(token in message for token in ("digest", "leaked", "candidate_genes")):
        code = "gold_isolation_failure"
    elif "duplicate genes" in message:
        code = "duplicate_predictions"
    elif "missing" in message:
        code = "missing_artifact"
    else:
        code = "invalid_ranking"
    return {
        "case_id": case.case_id, "disease_group_id": case.disease_group_id, "run_id": case.run_id,
        "valid": False, "error_code": code, "error": message, "no_candidates": True,
        "ranking_size": 0, "ndcg_at_k": 0.0, "recall_at_k": 0.0, "mrr_at_k": 0.0,
        "trap_applicable": bool(labels.trap_targets), "trap_violation": False,
        "trap_hits": [], "trap_violations": [], "safety_applicable": False,
        "safety_blocker_recall": None, "unsafe_go_rate": None,
    }


def evaluate_benchmark(manifest: BlindBenchmarkManifest, labels: BlindLabelSet, runs_root: Path) -> dict[str, Any]:
    if (manifest.benchmark_id, manifest.split_id) != (labels.benchmark_id, labels.split_id):
        raise ValueError("manifest and private labels identify different benchmark splits")
    label_map = {case.case_id: case for case in labels.cases}
    if {case.case_id for case in manifest.cases} != set(label_map):
        raise ValueError("manifest and private labels must contain exactly the same case IDs")

    cases = []
    for case in manifest.cases:
        try:
            cases.append(evaluate_case(case, label_map[case.case_id], runs_root, manifest.k))
        except (OSError, ValueError, TypeError, AttributeError, ValidationError, json.JSONDecodeError) as exc:
            cases.append(_failed_case(case, label_map[case.case_id], exc))

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in cases:
        groups.setdefault(row["disease_group_id"], []).append(row)

    def disease_macro(metric: str) -> float | None:
        return _mean([_mean([row[metric] for row in rows]) for rows in groups.values()])

    summary = {
        "cases": len(cases), "disease_groups": len(groups), "k": manifest.k,
        "disease_macro_ndcg_at_k": disease_macro("ndcg_at_k"),
        "disease_macro_recall_at_k": disease_macro("recall_at_k"),
        "disease_macro_mrr_at_k": disease_macro("mrr_at_k"),
        "trap_case_rate": _mean([
            float(row["trap_violation"]) if row["trap_applicable"] else None for row in cases
        ]),
        "disease_macro_safety_blocker_recall": disease_macro("safety_blocker_recall"),
        "disease_macro_unsafe_go_rate": disease_macro("unsafe_go_rate"),
        "structurally_valid_cases": sum(bool(row["valid"]) for row in cases),
        "cases_with_candidates": sum(not row["no_candidates"] for row in cases),
        "expert_adjudicated": labels.adjudication.status == "expert_adjudicated",
    }
    policy = manifest.thresholds
    gates = {
        "structural_integrity": summary["structurally_valid_cases"] == summary["cases"],
        "has_candidates": summary["cases_with_candidates"] == summary["cases"],
        "expert_adjudication": summary["expert_adjudicated"] if manifest.require_expert_adjudication else True,
        "ndcg_at_k": summary["disease_macro_ndcg_at_k"] >= policy.min_ndcg_at_k,
        "recall_at_k": summary["disease_macro_recall_at_k"] >= policy.min_recall_at_k,
        "mrr_at_k": summary["disease_macro_mrr_at_k"] >= policy.min_mrr_at_k,
        "trap_case_rate": summary["trap_case_rate"] is None
        or summary["trap_case_rate"] <= policy.max_trap_case_rate,
        "safety_blocker_recall": summary["disease_macro_safety_blocker_recall"] is None
        or summary["disease_macro_safety_blocker_recall"] >= policy.min_safety_blocker_recall,
        "unsafe_go_rate": summary["disease_macro_unsafe_go_rate"] is None
        or summary["disease_macro_unsafe_go_rate"] <= policy.max_unsafe_go_rate,
    }
    return {
        "contract_version": BLIND_BENCHMARK_CONTRACT_VERSION,
        "benchmark_id": manifest.benchmark_id, "split_id": manifest.split_id,
        "summary": summary, "gates": gates, "passed": all(gates.values()), "cases": cases,
    }


def public_report(report: dict[str, Any]) -> dict[str, Any]:
    """Remove per-case signals that could be used to probe private labels."""
    return {key: report[key] for key in ("contract_version", "benchmark_id", "split_id", "summary", "gates", "passed")}


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    return "\n".join([
        "# Blind Target-Ranking Benchmark", "",
        f"- Benchmark: `{report['benchmark_id']}` / `{report['split_id']}`",
        f"- Cases: {summary['cases']}; disease groups: {summary['disease_groups']}; K: {summary['k']}",
        f"- Disease-macro nDCG@K: {summary['disease_macro_ndcg_at_k']}",
        f"- Disease-macro Recall@K: {summary['disease_macro_recall_at_k']}",
        f"- Disease-macro MRR@K: {summary['disease_macro_mrr_at_k']}",
        f"- Trap-case rate: {summary['trap_case_rate']}",
        f"- Safety-blocker recall: {summary['disease_macro_safety_blocker_recall']}",
        f"- Unsafe-GO rate: {summary['disease_macro_unsafe_go_rate']}",
        f"- Expert adjudicated: {summary['expert_adjudicated']}",
        f"- Release gates passed: {report['passed']}", "",
        "These are ranking and safety-gate metrics, not clinical success probabilities.", "",
    ])
