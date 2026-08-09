"""Domain finding-driven repair policy: claim downgrade (R0), evidence
supplement (R1), evidence exclusion (R2) and scope-change rejection (R3).

These tests exercise the deterministic policy layer, the derived-layer overlay
module and the project runtime end to end. They never touch network tools.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from target_agent.research_contracts import (
    AssessmentDimension, AssessmentLevel, AssessmentRecord, AssessmentResult,
    AutonomyMode, DecisionAction, FailureClass, ProjectStatus, ResearchGoal,
    ResearchPlan, ResearchProjectSpec, RepairAction, RepairAuthorization,
    RepairResolutionStatus, RepairRisk, WorkItemResult, WorkItemSpec, WorkItemStatus,
)
from target_agent.research_modules import (
    DomainOverlayModule, ModuleDescriptor, ModuleExecution, PendingArtifact,
    ResearchModuleRegistry, TargetDiscoveryModule,
)
from target_agent.research_planner import ResearchPlanner
from target_agent.research_repair import (
    active_item_ids, build_plan_revision, build_repair_resolution,
    effective_plan, propose_domain_repair, work_item_result_digest,
)
from target_agent.research_runtime import ResearchProjectRuntime
from target_agent.research_service import ResearchProjectService
from target_agent.research_store import ResearchProjectStore
from target_agent.settings import Settings


def _project(project_id: str = "project-domain-policy", autonomy_mode: AutonomyMode = AutonomyMode.AUTONOMOUS):
    return ResearchProjectSpec(
        project_id=project_id,
        title="Traceable disease target project",
        domain="disease_target_discovery",
        goal=ResearchGoal(
            question="Which targets are supported by public evidence?",
            success_criteria=["Every released conclusion is traceable to a durable artifact."],
            deliverables=["A reviewed research report with explicit evidence gaps."],
        ),
        context={
            "target_task_spec": {
                "task_type": "disease_to_target",
                "question": "Which targets are supported by public evidence?",
                "context": {"disease": "ulcerative colitis", "tissue": "colon"},
            }
        },
        autonomy_mode=autonomy_mode,
    )


def _work_item(item_id: str, module: str, deps: list[str] | None = None, **kwargs) -> WorkItemSpec:
    return WorkItemSpec(
        item_id=item_id,
        title=item_id.replace("_", " "),
        module=module,
        objective=f"Run {module} for {item_id}.",
        dependencies=deps or [],
        acceptance_criteria=["Typed outputs and durable artifacts."],
        **kwargs,
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        STEP_API_KEY=None,
        TARGET_AGENT_RUN_DIR=tmp_path / "runs",
        RESEARCH_AGENT_PROJECT_DIR=tmp_path / "projects",
        TARGET_AGENT_CACHE_DIR=tmp_path / "cache",
        TARGET_AGENT_CACHE_ONLY=True,
        TARGET_AGENT_WEB_WORKERS=1,
        TARGET_AGENT_WEB_QUEUE_SIZE=2,
    )


class FakeFindingTargetModule:
    """target_discovery stand-in that emits typed derived claims/evidence/findings."""

    def __init__(self, findings: list[dict], calls: Counter | None = None):
        self.descriptor = ModuleDescriptor(
            name="target_discovery",
            description="Deterministic fake domain module emitting typed findings",
            input_types=("object",), output_types=("object",),
            execution_policy="deterministic_test", side_effect_free=True, replay_safe=True,
            repair_modes=(
                "same_input_retry", "alternate_dataset",
                "supplement_evidence", "exclude_evidence", "downgrade_claim",
                "split_context_same_scope",
            ),
        )
        self.findings = findings
        self.calls = calls or Counter()

    def execute(self, context):
        self.calls[self.descriptor.name] += 1
        outputs = {
            "child_run_id": f"run-fake-{context.item.item_id}",
            "terminal_status": "completed",
            "ranked_target_count": 10,
            "target_card_count": 5,
            "experiment_plan_count": 5,
            "deliverables_complete": True,
            "domain_activity_projection_complete": True,
            "derived_claims": [
                {
                    "claim_id": "claim-overstated",
                    "claim_class": "OBSERVED",
                    "statement": "Gene X drives disease Y.",
                    "evidence_ids": ["ev-genetics"],
                },
                {
                    "claim_id": "claim-rank",
                    "claim_class": "INFERRED",
                    "statement": "Gene Z is ranked 1 for prioritization.",
                    "evidence_ids": ["ev-omics"],
                },
            ],
            "evidence_items": [
                {"evidence_id": "ev-genetics", "lane": "genetics", "context_match_score": 0.9},
                {"evidence_id": "ev-omics", "lane": "omics", "context_match_score": 0.8},
                {"evidence_id": "ev-literature", "lane": "literature", "context_match_score": 0.7},
            ],
            "domain_findings": [dict(row) for row in self.findings],
        }
        return ModuleExecution(
            result=WorkItemResult(
                item_id=context.item.item_id,
                module=self.descriptor.name,
                status=WorkItemStatus.COMPLETED_WITH_GAPS,
                summary="Fake finding target completed with scientific gaps.",
                outputs=outputs,
                evidence_refs=["ev-genetics", "ev-omics"],
                failure_class=FailureClass.SCIENTIFIC_GAP,
            ),
        )


class FakeFindingBriefModule:
    name = "project_brief"

    def __init__(self):
        self.descriptor = ModuleDescriptor(
            name=self.name, description="Freeze the question", input_types=("object",),
            output_types=("object",), execution_policy="deterministic_test",
            side_effect_free=True, replay_safe=True,
        )

    def execute(self, context):
        return ModuleExecution(result=WorkItemResult(
            item_id=context.item.item_id, module=self.name, status=WorkItemStatus.COMPLETED,
            summary="Brief frozen.", outputs={
                "question": context.project.goal.question,
                "deliverables": context.project.goal.deliverables,
                "success_criteria": context.project.goal.success_criteria,
            },
        ))


class FakeFindingReviewModule:
    name = "independent_review"

    def __init__(self):
        self.descriptor = ModuleDescriptor(
            name=self.name, description="Emit typed status and domain-review assessments",
            input_types=("object",), output_types=("object",),
            execution_policy="deterministic_test", side_effect_free=True, replay_safe=True,
        )

    def execute(self, context):
        assessments: list[AssessmentRecord] = []
        failures: list[str] = []
        for item_id, result in context.prior_results.items():
            problematic = result.status in {
                WorkItemStatus.FAILED, WorkItemStatus.BLOCKED, WorkItemStatus.NEEDS_INPUT,
            }
            assessments.append(AssessmentRecord(
                project_id=context.project.project_id, target_id=item_id,
                target_digest=work_item_result_digest(result),
                dimension=AssessmentDimension.METHODOLOGY, level=AssessmentLevel.A0,
                result=AssessmentResult.FAIL if problematic else AssessmentResult.PASS,
                actor="independent_review", method="typed_status_gate",
                rationale=f"Status is {result.status.value}.", blocking=problematic,
            ))
            if problematic:
                failures.append(item_id)
            for finding in (result.outputs.get("domain_findings") or []):
                if not isinstance(finding, dict) or finding.get("severity") != "blocking":
                    continue
                if finding.get("finding_status") == "resolved":
                    continue
                assessments.append(AssessmentRecord(
                    project_id=context.project.project_id, target_id=item_id,
                    target_digest=work_item_result_digest(result),
                    dimension=AssessmentDimension.ENTAILMENT, level=AssessmentLevel.A0,
                    result=AssessmentResult.FAIL, actor="independent_review",
                    method="typed_domain_review",
                    rationale=str(finding.get("message") or finding.get("category") or "domain finding"),
                    blocking=True,
                ))
                failures.append(item_id)
        path = context.output_dir / "review_summary.json"
        path.write_text(
            json.dumps({"assessment_count": len(assessments), "blocking_failures": failures}),
            encoding="utf-8",
        )
        return ModuleExecution(
            result=WorkItemResult(
                item_id=context.item.item_id, module=self.name,
                status=WorkItemStatus.COMPLETED_WITH_GAPS if failures else WorkItemStatus.COMPLETED,
                summary="Review completed.", outputs={
                    "assessment_count": len(assessments), "blocking_failures": failures,
                },
            ),
            artifacts=[PendingArtifact(path, "review_summary", "application/json")],
            assessments=assessments,
        )


class FakeFindingReportModule:
    name = "research_report"

    def __init__(self):
        self.descriptor = ModuleDescriptor(
            name=self.name, description="Render report", input_types=("object",),
            output_types=("object",), execution_policy="deterministic_test",
            side_effect_free=True, replay_safe=True,
        )

    def execute(self, context):
        gaps = [
            item_id for item_id, result in context.prior_results.items()
            if result.status != WorkItemStatus.COMPLETED
        ]
        path = context.output_dir / "research_report.md"
        path.write_text("# Report\n", encoding="utf-8")
        return ModuleExecution(
            result=WorkItemResult(
                item_id=context.item.item_id, module=self.name,
                status=WorkItemStatus.COMPLETED_WITH_GAPS if gaps else WorkItemStatus.COMPLETED,
                summary="Report rendered.", outputs={
                    "reported_items": len(context.prior_results), "gap_count": len(gaps),
                },
            ),
            artifacts=[PendingArtifact(path, "research_report", "text/markdown")],
        )


def _finding(category: str, related_ids: list[str], finding_id: str | None = None,
             message: str = "typed finding", severity: str = "blocking",
             subject: dict | None = None) -> dict:
    return {
        "finding_id": finding_id or f"finding-{category}-{len(related_ids)}",
        "category": category,
        "severity": severity,
        "related_ids": related_ids,
        "subject": subject or {},
        "message": message,
    }


def _registry(findings: list[dict]) -> ResearchModuleRegistry:
    return ResearchModuleRegistry([
        FakeFindingTargetModule(findings),
        FakeFindingBriefModule(),
        FakeFindingReviewModule(),
        FakeFindingReportModule(),
        DomainOverlayModule(),
    ])


def _runtime(tmp_path: Path, findings: list[dict], autonomy_mode: AutonomyMode):
    registry = _registry(findings)
    settings = _settings(tmp_path)
    runtime = ResearchProjectRuntime(
        projects_dir=settings.projects_dir, cache_dir=settings.cache_dir,
        registry=registry, planner=ResearchPlanner(registry, client=None), settings=settings,
    )
    return runtime, settings


# ---------------------------------------------------------------- unit policy

def _propose_one(tmp_path, registry, project, plan, finding,
                 autonomy_mode: AutonomyMode = AutonomyMode.AUTONOMOUS):
    module = registry.get("target_discovery")
    module.findings = [finding]
    context = _work_item("target_discovery", "target_discovery")
    result = module.execute(type("Ctx", (), {
        "item": context, "output_dir": tmp_path, "project": project, "project_dir": tmp_path,
        "cache_dir": tmp_path / "cache", "settings": _settings(tmp_path),
        "prior_results": {}, "artifacts": [],
    })()).result
    digest = work_item_result_digest(result)
    assessments = [
        AssessmentRecord(
            project_id=project.project_id, target_id="target_discovery", target_digest=digest,
            dimension=AssessmentDimension.ENTAILMENT, level=AssessmentLevel.A0,
            result=AssessmentResult.FAIL, actor="independent_review",
            method="typed_domain_review", rationale="blocking domain finding", blocking=True,
        ),
        AssessmentRecord(
            project_id=project.project_id, target_id="target_discovery", target_digest=digest,
            dimension=AssessmentDimension.METHODOLOGY, level=AssessmentLevel.A0,
            result=AssessmentResult.PASS, actor="independent_review",
            method="typed_status_gate", rationale="status ok", blocking=False,
        ),
    ]
    return propose_domain_repair(
        project=_project(autonomy_mode=autonomy_mode), base_plan=plan, plan=plan,
        results={"target_discovery": result}, assessments=assessments,
        artifacts=[], revisions=[], registry=registry,
    )


def test_policy_maps_blocking_findings_to_typed_requests(tmp_path):
    registry = _registry([])
    project = _project()
    plan = ResearchPlan(
        project_id=project.project_id, planner_backend="deterministic_test",
        rationale="policy unit test",
        items=[
            _work_item("target_discovery", "target_discovery"),
            _work_item("independent_review", "independent_review", ["target_discovery"]),
            _work_item("research_report", "research_report", ["independent_review"]),
        ],
    )

    downgrade = _propose_one(
        tmp_path, registry, project, plan,
        _finding("causal_overreach", ["claim-overstated"], finding_id="finding-causal"),
    )
    assert downgrade is not None
    assert downgrade.action == RepairAction.DOWNGRADE_CLAIM
    assert downgrade.risk == RepairRisk.R0_DERIVATION_ONLY
    assert downgrade.authorization == RepairAuthorization.AUTOMATIC
    assert downgrade.directive_payload["claim_id"] == "claim-overstated"
    assert downgrade.directive_payload["to_class"] == "INFERRED"

    supplement = _propose_one(
        tmp_path, registry, project, plan,
        _finding("coverage_gap", ["ev-literature"], finding_id="finding-coverage"),
    )
    assert supplement is not None
    assert supplement.action == RepairAction.SUPPLEMENT_EVIDENCE
    assert supplement.risk == RepairRisk.R1_SAME_SCOPE_READ_ONLY
    assert supplement.authorization == RepairAuthorization.AUTOMATIC
    assert supplement.directive_payload["evidence_ids"] == ["ev-literature"]

    exclusion = _propose_one(
        tmp_path, registry, project, plan,
        _finding("context_mismatch", ["ev-genetics"], finding_id="finding-context"),
        autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    assert exclusion is not None
    assert exclusion.action == RepairAction.EXCLUDE_EVIDENCE
    assert exclusion.risk == RepairRisk.R2_SCIENTIFIC_METHOD_CHANGE
    assert exclusion.authorization == RepairAuthorization.CHECKPOINT_REQUIRED
    assert exclusion.directive_payload["evidence_refs"] == ["ev-genetics"]

    dependence = _propose_one(
        tmp_path, registry, project, plan,
        _finding("evidence_dependence", ["claim-overstated"], finding_id="finding-dependence"),
    )
    assert dependence is not None
    assert dependence.action == RepairAction.DOWNGRADE_CLAIM
    assert dependence.risk == RepairRisk.R0_DERIVATION_ONLY
    assert dependence.authorization == RepairAuthorization.AUTOMATIC
    assert dependence.directive_payload["claim_id"] == "claim-overstated"
    assert dependence.directive_payload["to_class"] == "INFERRED"


def _execute_overlay(tmp_path, registry, project, source, overlay_input):
    item = _work_item(
        "domain_overlay_replacement", "domain_overlay",
        rerun_of_item_id="target_discovery",
        repair_request_id="repair-000000000000000000000001",
        inputs={"source_item_id": "target_discovery", "domain_overlay": overlay_input},
    )
    module = registry.get("domain_overlay")
    return module.execute(type("Ctx", (), {
        "item": item, "output_dir": tmp_path, "project": project, "project_dir": tmp_path,
        "cache_dir": tmp_path / "cache", "settings": _settings(tmp_path),
        "prior_results": {"target_discovery": source}, "artifacts": [],
    })()).result


def test_supplement_and_exclusion_require_reason_and_existing_evidence(tmp_path):
    registry = _registry([])
    project = _project()
    plan = ResearchPlan(
        project_id=project.project_id, planner_backend="deterministic_test",
        rationale="policy unit test",
        items=[
            _work_item("target_discovery", "target_discovery"),
            _work_item("independent_review", "independent_review", ["target_discovery"]),
            _work_item("research_report", "research_report", ["independent_review"]),
        ],
    )
    no_reason = _propose_one(
        tmp_path, registry, project, plan,
        _finding("coverage_gap", ["ev-literature"], finding_id="finding-no-reason",
                 message="   ", subject={"reason": "  "}),
    )
    assert no_reason is None
    missing_source = _propose_one(
        tmp_path, registry, project, plan,
        _finding("context_mismatch", ["ev-missing"], finding_id="finding-missing"),
        autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    assert missing_source is None


def test_autonomous_mode_never_proposes_checkpoint_required_repairs(tmp_path):
    registry = _registry([])
    project = _project()
    plan = ResearchPlan(
        project_id=project.project_id, planner_backend="deterministic_test",
        rationale="policy unit test",
        items=[
            _work_item("target_discovery", "target_discovery"),
            _work_item("independent_review", "independent_review", ["target_discovery"]),
            _work_item("research_report", "research_report", ["independent_review"]),
        ],
    )
    finding = _finding("context_mismatch", ["ev-genetics"], finding_id="finding-context")
    assert _propose_one(
        tmp_path, registry, project, plan, finding,
        autonomy_mode=AutonomyMode.AUTONOMOUS,
    ) is None
    assert _propose_one(
        tmp_path, registry, project, plan, finding,
        autonomy_mode=AutonomyMode.CHECKPOINTED,
    ) is not None


def test_context_split_proposal_validates_same_scope_subcontexts(tmp_path):
    registry = _registry([])
    project = _project()
    plan = ResearchPlan(
        project_id=project.project_id, planner_backend="deterministic_test",
        rationale="policy unit test",
        items=[
            _work_item("target_discovery", "target_discovery"),
            _work_item("independent_review", "independent_review", ["target_discovery"]),
            _work_item("research_report", "research_report", ["independent_review"]),
        ],
    )
    split = _propose_one(
        tmp_path, registry, project, plan,
        _finding(
            "context_split_needed", ["ev-genetics", "ev-omics"],
            finding_id="finding-split",
            subject={
                "context_dimension": "tissue",
                "evidence_contexts": {
                    "ev-genetics": {"tissue": "colon mucosa"},
                    "ev-omics": {"tissue": "colon epithelium"},
                },
                "reason": "distinct same-scope tissues",
            },
        ),
        autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    assert split is not None
    assert split.action == RepairAction.SPLIT_CONTEXT_SAME_SCOPE
    assert split.risk == RepairRisk.R2_SCIENTIFIC_METHOD_CHANGE
    assert split.authorization == RepairAuthorization.CHECKPOINT_REQUIRED
    assert split.directive_payload["context_dimension"] == "tissue"
    assert split.directive_payload["evidence_contexts"]["ev-genetics"] == {"tissue": "colon mucosa"}

    broader = _propose_one(
        tmp_path, registry, project, plan,
        _finding(
            "context_split_needed", ["ev-genetics"],
            finding_id="finding-split-broad",
            subject={
                "context_dimension": "tissue",
                "evidence_contexts": {"ev-genetics": {"tissue": "gut"}},
                "reason": "broader tissue must be refused",
            },
        ),
        autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    assert broader is None


def test_overlay_refuses_upgrade_noop_and_missing_evidence(tmp_path):
    registry = _registry([])
    project = _project()
    module = registry.get("target_discovery")
    module.findings = []
    context = _work_item("target_discovery", "target_discovery")
    source = module.execute(type("Ctx", (), {
        "item": context, "output_dir": tmp_path, "project": project, "project_dir": tmp_path,
        "cache_dir": tmp_path / "cache", "settings": _settings(tmp_path),
        "prior_results": {}, "artifacts": [],
    })()).result

    upgrade = _execute_overlay(
        tmp_path, registry, project, source,
        {"operation": "downgrade_claim", "claim_id": "claim-overstated",
         "to_class": "OBSERVED", "finding_id": "finding-upgrade"},
    )
    assert upgrade.outputs["domain_overlay_applied"] is False
    assert any("never upgrades" in problem for problem in upgrade.outputs["domain_overlay_problems"])

    noop = _execute_overlay(
        tmp_path, registry, project, source,
        {"operation": "downgrade_claim", "claim_id": "claim-rank",
         "to_class": "INFERRED", "finding_id": "finding-noop"},
    )
    assert noop.outputs["domain_overlay_applied"] is False
    assert any("already INFERRED" in problem for problem in noop.outputs["domain_overlay_problems"])

    missing = _execute_overlay(
        tmp_path, registry, project, source,
        {"operation": "exclude_evidence", "evidence_refs": ["ev-genetics", "ev-missing"],
         "reason": "flagged", "finding_id": "finding-missing"},
    )
    assert missing.outputs["domain_overlay_applied"] is False
    assert any("missing from the derived evidence set" in problem for problem in missing.outputs["domain_overlay_problems"])
    assert "ev-genetics" in missing.evidence_refs


def test_conflict_with_distinct_subcontexts_emits_split_finding():
    evidence = [
        {"evidence_id": "ev-up", "gene_symbol": "GENE1", "effect_direction": "increase",
         "context": {"tissue": "colon mucosa"}},
        {"evidence_id": "ev-down", "gene_symbol": "GENE1", "effect_direction": "decrease",
         "context": {"tissue": "colon epithelium"}},
    ]
    findings = TargetDiscoveryModule._deterministic_evidence_findings([], evidence)
    split = next(row for row in findings if row["category"] == "context_split_needed")
    assert split["finding_id"] == "finding-context-split-ev-up-ev-down"
    assert split["subject"]["context_dimension"] == "tissue"
    assert split["subject"]["evidence_contexts"]["ev-up"] == {"tissue": "colon mucosa"}
    assert split["subject"]["evidence_contexts"]["ev-down"] == {"tissue": "colon epithelium"}


def test_checkpointed_context_split_applies_and_releases(tmp_path):
    findings = [
        _finding(
            "context_split_needed", ["ev-genetics", "ev-omics"],
            finding_id="finding-split",
            subject={
                "context_dimension": "tissue",
                "evidence_contexts": {
                    "ev-genetics": {"tissue": "colon mucosa"},
                    "ev-omics": {"tissue": "colon epithelium"},
                },
                "reason": "distinct same-scope tissues",
            },
        ),
    ]
    runtime, settings = _runtime(tmp_path, findings, AutonomyMode.CHECKPOINTED)
    project = _project("project-split", autonomy_mode=AutonomyMode.CHECKPOINTED)
    service = ResearchProjectService(runtime)

    first = runtime.run(project)
    assert first["status"] == ProjectStatus.NEEDS_INPUT.value
    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    plan = store.load_plan()
    service.accept_checkpoint(
        project_id=project.project_id, target_id=plan.plan_id,
        actor="reviewer", rationale="Plan is in scope.", resume=True,
    )
    request = store.read_repair_requests()[0]
    assert request.action == RepairAction.SPLIT_CONTEXT_SAME_SCOPE

    service.decide_repair(
        project_id=project.project_id,
        repair_request_id=request.repair_request_id,
        trigger_snapshot_digest=request.trigger_snapshot_digest,
        approve=True, actor="reviewer",
        rationale="Approve same-scope context split.", resume=True,
    )
    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    state = store.load_state()
    assert state.status == ProjectStatus.WAITING_REVIEW
    release_target = service.snapshot(project.project_id)["next_actions"][0]["target_id"]
    service.accept_checkpoint(
        project_id=project.project_id, target_id=release_target,
        actor="reviewer", rationale="Release after verified split.", resume=True,
    )
    assert store.load_state().status == ProjectStatus.COMPLETED.value

    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    resolutions = store.read_repair_resolutions()
    assert len(resolutions) == 1
    assert resolutions[0].status == RepairResolutionStatus.RESOLVED
    results = store.load_work_item_results()
    overlay = next(row for row in results.values() if row.module == "domain_overlay")
    assert overlay.outputs["domain_overlay_applied"] is True
    assert len(overlay.outputs["context_splits"]) == 1
    evidence = {row["evidence_id"]: row for row in overlay.outputs["evidence_items"]}
    assert evidence["ev-genetics"]["context"]["tissue"] == "colon mucosa"
    assert evidence["ev-omics"]["context"]["tissue"] == "colon epithelium"
    assert "ev-genetics" in overlay.evidence_refs and "ev-omics" in overlay.evidence_refs
    store.assert_integrity()


def test_deterministic_evidence_gates_direction_and_independence():
    claims = [
        {
            "claim_id": "claim-directional",
            "claim_class": "OBSERVED",
            "evidence_ids": ["ev-up", "ev-down"],
        },
        {
            "claim_id": "claim-single-lineage",
            "claim_class": "INFERRED",
            "evidence_ids": ["ev-x1", "ev-x2"],
        },
        {
            "claim_id": "claim-independent",
            "claim_class": "OBSERVED",
            "evidence_ids": ["ev-a", "ev-b"],
        },
    ]
    evidence = [
        {"evidence_id": "ev-up", "gene_symbol": "GENE1", "effect_direction": "increase",
         "source_id": "study-up"},
        {"evidence_id": "ev-down", "gene_symbol": "GENE1", "effect_direction": "decrease",
         "source_id": "study-down"},
        {"evidence_id": "ev-x1", "gene_symbol": "GENE2", "effect_direction": "increase",
         "source_id": "study-shared", "dataset_id": "GSE-shared"},
        {"evidence_id": "ev-x2", "gene_symbol": "GENE2", "effect_direction": "increase",
         "source_id": "study-shared", "dataset_id": "GSE-shared"},
        {"evidence_id": "ev-a", "gene_symbol": "GENE3", "effect_direction": "increase",
         "source_id": "study-a"},
        {"evidence_id": "ev-b", "gene_symbol": "GENE3", "effect_direction": "decrease",
         "source_id": "study-b", "tool_run_id": "run-b"},
    ]
    findings = TargetDiscoveryModule._deterministic_evidence_findings(claims, evidence)
    by_id = {row["finding_id"]: row for row in findings}
    assert "finding-direction-gene1" in by_id
    assert by_id["finding-direction-gene1"]["category"] == "conflicting_evidence"
    assert by_id["finding-direction-gene1"]["severity"] == "blocking"
    assert set(by_id["finding-direction-gene1"]["related_ids"]) == {"ev-up", "ev-down"}
    assert "finding-dependence-claim-single-lineage" in by_id
    assert by_id["finding-dependence-claim-single-lineage"]["category"] == "evidence_dependence"
    assert by_id["finding-dependence-claim-single-lineage"]["subject"]["claim_id"] == "claim-single-lineage"
    assert not any(row["finding_id"].startswith("finding-dependence-claim-independent")
                   for row in findings)
    assert "finding-direction-gene3" in by_id
    assert set(by_id["finding-direction-gene3"]["related_ids"]) == {"ev-a", "ev-b"}


def test_policy_never_proposes_scope_or_truth_changes(tmp_path):
    registry = _registry([])
    project = _project()
    plan = ResearchPlan(
        project_id=project.project_id, planner_backend="deterministic_test",
        rationale="policy unit test",
        items=[
            _work_item("target_discovery", "target_discovery"),
            _work_item("independent_review", "independent_review", ["target_discovery"]),
            _work_item("research_report", "research_report", ["independent_review"]),
        ],
    )
    module = registry.get("target_discovery")
    module.findings = [
        _finding("unsupported_claim", ["ev-genetics"], finding_id="finding-scope"),
        _finding("causal_overreach", ["claim-rank"], finding_id="finding-noop"),
    ]
    context = _work_item("target_discovery", "target_discovery")
    result = module.execute(type("Ctx", (), {
        "item": context, "output_dir": tmp_path, "project": project, "project_dir": tmp_path,
        "cache_dir": tmp_path / "cache", "settings": _settings(tmp_path),
        "prior_results": {}, "artifacts": [],
    })()).result
    digest = work_item_result_digest(result)
    assessments = [
        AssessmentRecord(
            project_id=project.project_id, target_id="target_discovery", target_digest=digest,
            dimension=AssessmentDimension.ENTAILMENT, level=AssessmentLevel.A0,
            result=AssessmentResult.FAIL, actor="independent_review",
            method="typed_domain_review", rationale="blocking", blocking=True,
        )
    ]
    # unsupported_claim is not in FINDING_TO_ACTION; claim-rank is already INFERRED,
    # so no downgrade may be proposed either.
    assert propose_domain_repair(
        project=project, base_plan=plan, plan=plan, results={"target_discovery": result},
        assessments=assessments, artifacts=[], revisions=[], registry=registry,
    ) is None


def test_overlay_revision_uses_domain_overlay_module_and_resolution_gate(tmp_path):
    registry = _registry([])
    project = _project()
    plan = ResearchPlan(
        project_id=project.project_id, planner_backend="deterministic_test",
        rationale="policy unit test",
        items=[
            _work_item("target_discovery", "target_discovery"),
            _work_item("independent_review", "independent_review", ["target_discovery"]),
            _work_item("research_report", "research_report", ["independent_review"]),
        ],
    )
    module = registry.get("target_discovery")
    module.findings = [_finding("causal_overreach", ["claim-overstated"], finding_id="finding-causal")]
    context = _work_item("target_discovery", "target_discovery")
    source = module.execute(type("Ctx", (), {
        "item": context, "output_dir": tmp_path, "project": project, "project_dir": tmp_path,
        "cache_dir": tmp_path / "cache", "settings": _settings(tmp_path),
        "prior_results": {}, "artifacts": [],
    })()).result
    digest = work_item_result_digest(source)
    assessments = [
        AssessmentRecord(
            project_id=project.project_id, target_id="target_discovery", target_digest=digest,
            dimension=AssessmentDimension.ENTAILMENT, level=AssessmentLevel.A0,
            result=AssessmentResult.FAIL, actor="independent_review",
            method="typed_domain_review", rationale="blocking", blocking=True,
        )
    ]
    request = propose_domain_repair(
        project=project, base_plan=plan, plan=plan, results={"target_discovery": source},
        assessments=assessments, artifacts=[], revisions=[], registry=registry,
    )
    assert request is not None and request.action == RepairAction.DOWNGRADE_CLAIM

    revision = build_plan_revision(
        request=request, base_plan=plan, plan=plan, assessments=assessments,
        artifacts=[], revisions=[],
    )
    added = next(row for row in revision.added_items if row.rerun_of_item_id == "target_discovery")
    assert added.module == "domain_overlay"
    assert added.inputs["source_item_id"] == "target_discovery"
    assert added.inputs["domain_overlay"]["claim_id"] == "claim-overstated"

    revised_plan = effective_plan(plan, [revision])
    revised_ids = active_item_ids(revised_plan, [revision])
    assert "target_discovery" not in revised_ids
    overlay_id = added.item_id
    assert overlay_id in revised_ids

    # Execute the overlay and resolve the repair.
    overlay_module = registry.get("domain_overlay")
    overlay_result = overlay_module.execute(type("Ctx", (), {
        "item": added, "output_dir": tmp_path, "project": project, "project_dir": tmp_path,
        "cache_dir": tmp_path / "cache", "settings": _settings(tmp_path),
        "prior_results": {"target_discovery": source}, "artifacts": [],
    })()).result
    assert overlay_result.status == WorkItemStatus.COMPLETED
    assert overlay_result.outputs["domain_overlay_applied"] is True
    claims = {row["claim_id"]: row for row in overlay_result.outputs["derived_claims"]}
    assert claims["claim-overstated"]["claim_class"] == "INFERRED"

    verification = [
        AssessmentRecord(
            project_id=project.project_id, target_id=overlay_id,
            target_digest=work_item_result_digest(overlay_result),
            dimension=AssessmentDimension.METHODOLOGY, level=AssessmentLevel.A0,
            result=AssessmentResult.PASS, actor="independent_review",
            method="typed_status_gate", rationale="overlay passed", blocking=False,
        )
    ]
    results = {overlay_id: overlay_result}
    for row in revision.added_items:
        if row.item_id != overlay_id:
            results[row.item_id] = WorkItemResult(
                item_id=row.item_id, module=row.module,
                status=WorkItemStatus.COMPLETED, summary="ok", outputs={},
            )
    resolution = build_repair_resolution(
        request=request, revision=revision, project=project, plan=revised_plan,
        results=results, assessments=verification, artifacts=[], revisions=[revision],
        exhausted=False,
    )
    assert resolution is not None
    assert resolution.status == RepairResolutionStatus.RESOLVED


# ---------------------------------------------------------------- integration

def test_autonomous_r0_and_r1_findings_auto_apply_and_release(tmp_path):
    findings = [
        _finding("causal_overreach", ["claim-overstated"], finding_id="finding-causal"),
        _finding("coverage_gap", ["ev-literature"], finding_id="finding-coverage"),
    ]
    runtime, settings = _runtime(tmp_path, findings, AutonomyMode.AUTONOMOUS)
    project = _project("project-auto-overlay", autonomy_mode=AutonomyMode.AUTONOMOUS)

    terminal = runtime.run(project)

    assert terminal["status"] == ProjectStatus.COMPLETED.value
    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    requests = store.read_repair_requests()
    actions = sorted(row.action.value for row in requests)
    assert actions == ["downgrade_claim", "supplement_evidence"]
    assert all(row.authorization == RepairAuthorization.AUTOMATIC for row in requests)
    revisions = store.read_plan_revisions()
    resolutions = store.read_repair_resolutions()
    assert len(revisions) == len(resolutions) == 2
    assert all(row.status == RepairResolutionStatus.RESOLVED for row in resolutions)
    results = store.load_work_item_results()
    overlay_rows = [row for row in results.values() if row.module == "domain_overlay"]
    assert len(overlay_rows) == 2
    final = max(overlay_rows, key=lambda row: row.item_id)
    assert final.outputs["domain_overlay_applied"] is True
    claims = {row["claim_id"]: row for row in final.outputs["derived_claims"]}
    assert claims["claim-overstated"]["claim_class"] == "INFERRED"
    assert "ev-literature" in final.evidence_refs
    assert store.load_state().status == ProjectStatus.COMPLETED.value
    store.assert_integrity()


def test_checkpointed_r2_exclusion_requires_approval_then_applies(tmp_path):
    findings = [
        _finding("context_mismatch", ["ev-genetics"], finding_id="finding-context"),
    ]
    runtime, settings = _runtime(tmp_path, findings, AutonomyMode.CHECKPOINTED)
    project = _project("project-exclusion", autonomy_mode=AutonomyMode.CHECKPOINTED)
    service = ResearchProjectService(runtime)

    first = runtime.run(project)
    assert first["status"] == ProjectStatus.NEEDS_INPUT.value
    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    plan = store.load_plan()
    service.accept_checkpoint(
        project_id=project.project_id, target_id=plan.plan_id,
        actor="reviewer", rationale="Plan is in scope.", resume=True,
    )
    request = store.read_repair_requests()[0]
    assert request.action == RepairAction.EXCLUDE_EVIDENCE
    assert request.risk == RepairRisk.R2_SCIENTIFIC_METHOD_CHANGE
    assert request.authorization == RepairAuthorization.CHECKPOINT_REQUIRED

    service.decide_repair(
        project_id=project.project_id,
        repair_request_id=request.repair_request_id,
        trigger_snapshot_digest=request.trigger_snapshot_digest,
        approve=True, actor="reviewer",
        rationale="Approve context-mismatch evidence exclusion.", resume=True,
    )
    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    state = store.load_state()
    assert state.status == ProjectStatus.WAITING_REVIEW
    release_target = service.snapshot(project.project_id)["next_actions"][0]["target_id"]
    service.accept_checkpoint(
        project_id=project.project_id, target_id=release_target,
        actor="reviewer", rationale="Release after verified exclusion.", resume=True,
    )
    assert store.load_state().status == ProjectStatus.COMPLETED.value

    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    resolutions = store.read_repair_resolutions()
    assert len(resolutions) == 1
    assert resolutions[0].status == RepairResolutionStatus.RESOLVED
    results = store.load_work_item_results()
    overlay = next(row for row in results.values() if row.module == "domain_overlay")
    assert overlay.outputs["domain_overlay_applied"] is True
    assert overlay.outputs["domain_overlay_operations"][0]["operation"] == "exclude_evidence"
    assert "ev-genetics" not in overlay.evidence_refs
    store.assert_integrity()


def test_integrity_rejects_domain_request_that_declares_scope_change(tmp_path):
    runtime, settings = _runtime(tmp_path, [
        _finding("causal_overreach", ["claim-overstated"], finding_id="finding-scope-gate"),
    ], AutonomyMode.AUTONOMOUS)
    project = _project("project-scope-gate", autonomy_mode=AutonomyMode.AUTONOMOUS)
    runtime.run(project)
    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    request = store.read_repair_requests()[0]
    request_path = store.project_dir / "repair_requests" / f"{request.repair_request_id}.json"
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    payload["no_scope_change"] = False
    request_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must declare no_scope_change"):
        store.assert_integrity()


def test_integrity_rejects_resolved_repair_with_active_blocking_assessment(tmp_path):
    runtime, settings = _runtime(tmp_path, [
        _finding("causal_overreach", ["claim-overstated"], finding_id="finding-blocker-gate"),
    ], AutonomyMode.AUTONOMOUS)
    project = _project("project-blocker-gate", autonomy_mode=AutonomyMode.AUTONOMOUS)
    runtime.run(project)
    store = ResearchProjectStore(settings.projects_dir, project.project_id)
    assert store.load_state().status == ProjectStatus.COMPLETED.value
    assert all(
        row.status == RepairResolutionStatus.RESOLVED
        for row in store.read_repair_resolutions()
    )
    overlay = next(
        row for row in store.load_work_item_results().values()
        if row.module == "domain_overlay"
    )
    store.append_assessment(AssessmentRecord(
        project_id=project.project_id,
        target_id=overlay.item_id,
        target_digest=work_item_result_digest(overlay),
        dimension=AssessmentDimension.METHODOLOGY,
        level=AssessmentLevel.A0,
        result=AssessmentResult.FAIL,
        actor="independent_review",
        method="typed_status_gate",
        rationale="post-resolution blocker injected by integrity test",
        blocking=True,
    ))

    with pytest.raises(ValueError, match="leaves a blocking assessment active"):
        store.assert_integrity()
