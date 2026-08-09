"""Export JSON Schemas from the canonical Pydantic 2.0 models."""
from __future__ import annotations

import json
from pathlib import Path

from .contracts import (
    AnalysisEvidenceArtifact, AnalysisRecipe, CaseRecord, CausalGraph, Claim, DatasetCandidate,
    DatasetSelectionConstraint, EqtlColocalizationResultInput, EvidenceItem,
    ExecutionPlan, ExperimentPlan, FineMappingResultInput, GeneticEvidencePayload,
    GeneticsAnalysisConstraints, GwasSummaryStatsInput, HarmonizedVariantManifest,
    LDReferenceSpec, OmicsResult,
    ReviewerFinding, TargetCard, TargetGeneticEvidenceSummary, TaskSpec, ToolDescriptor,
    ToolResult, TraceEvent,
)
from .research_contracts import (
    ArtifactHead, ArtifactRecord, ArtifactVersion, AssessmentRecord, DataContract, DecisionEvent,
    DomainActivityPage, DomainActivityRecord, ForkDirective, PlanBranch, ProjectEvent,
    ProjectState, RepairDirective, RepairQueueSnapshot, RepairRequest, RepairResolution,
    ResearchGoal, ResearchPlan, ResearchPlanRevision, ResearchProjectControl,
    ResearchProjectSnapshot, ResearchProjectSpec, ReviewTarget, WorkAttempt, WorkItemHead,
    WorkItemResult, WorkItemSpec, WorkerLease,
)
from .blind_benchmark import BlindBenchmarkManifest, BlindLabelSet


# Hand-maintained schemas that the exporter must never delete; they describe
# benchmark fixtures that are not Pydantic product contracts.
PRESERVED_SCHEMAS = frozenset({"context_relation_case.schema.json"})


MODELS = {
    "task_spec": TaskSpec,
    "execution_plan": ExecutionPlan,
    "tool_result": ToolResult,
    "evidence_item": EvidenceItem,
    "claim": Claim,
    "reviewer_finding": ReviewerFinding,
    "causal_graph": CausalGraph,
    "experiment_plan": ExperimentPlan,
    "target_card": TargetCard,
    "trace_event": TraceEvent,
    "case_record": CaseRecord,
    "dataset_candidate": DatasetCandidate,
    "dataset_selection_constraint": DatasetSelectionConstraint,
    "genetics_analysis_constraints": GeneticsAnalysisConstraints,
    "gwas_summary_statistics_input": GwasSummaryStatsInput,
    "fine_mapping_result_input": FineMappingResultInput,
    "eqtl_colocalization_result_input": EqtlColocalizationResultInput,
    "harmonized_variant_manifest": HarmonizedVariantManifest,
    "analysis_evidence_artifact": AnalysisEvidenceArtifact,
    "ld_reference_spec": LDReferenceSpec,
    "genetic_evidence_payload": GeneticEvidencePayload,
    "target_genetic_evidence_summary": TargetGeneticEvidenceSummary,
    "analysis_recipe": AnalysisRecipe,
    "omics_result": OmicsResult,
    "tool_descriptor": ToolDescriptor,
    "research_project_spec": ResearchProjectSpec,
    "research_goal": ResearchGoal,
    "research_plan": ResearchPlan,
    "research_work_item": WorkItemSpec,
    "research_work_item_result": WorkItemResult,
    "research_data_contract": DataContract,
    "research_artifact": ArtifactRecord,
    "research_assessment": AssessmentRecord,
    "research_decision": DecisionEvent,
    "research_project_event": ProjectEvent,
    "research_domain_activity": DomainActivityRecord,
    "research_domain_activity_page": DomainActivityPage,
    "research_repair_request": RepairRequest,
    "research_repair_directive": RepairDirective,
    "research_plan_revision": ResearchPlanRevision,
    "research_work_attempt": WorkAttempt,
    "research_fork_directive": ForkDirective,
    "research_plan_branch": PlanBranch,
    "research_artifact_version": ArtifactVersion,
    "research_review_target": ReviewTarget,
    "research_worker_lease": WorkerLease,
    "research_work_item_head": WorkItemHead,
    "research_artifact_head": ArtifactHead,
    "research_repair_resolution": RepairResolution,
    "research_repair_queue": RepairQueueSnapshot,
    "research_project_snapshot": ResearchProjectSnapshot,
    "research_project_state": ProjectState,
    "research_project_control": ResearchProjectControl,
    "blind_benchmark_manifest": BlindBenchmarkManifest,
    "blind_benchmark_labels": BlindLabelSet,
}


def export_schemas(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected = {f"{name}.schema.json" for name in MODELS}
    for stale in output_dir.glob("*.schema.json"):
        if stale.name in PRESERVED_SCHEMAS:
            continue
        if stale.name not in expected:
            stale.unlink()
    written = []
    for name, model in MODELS.items():
        path = output_dir / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    for path in export_schemas(root / "schemas"):
        print(path)


if __name__ == "__main__":
    main()
