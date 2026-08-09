"""P1-6: repair resolution requires an original-finding substance recheck.

A typed_status_gate PASS cannot close the loop by itself, and
``fake_independent_review`` is not accepted as the formal independent reviewer.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from target_agent.research_contracts import (
    AssessmentDimension, AssessmentLevel, AssessmentRecord, AssessmentResult,
    AutonomyMode, FailureClass, ResearchGoal, ResearchPlan, ResearchProjectSpec,
    RepairAction, RepairResolutionStatus, WorkItemResult, WorkItemSpec,
    WorkItemStatus,
)
from target_agent.research_modules import (
    DomainOverlayModule, ModuleDescriptor, ModuleExecution, PendingArtifact,
    ResearchModuleRegistry,
)
from target_agent.research_repair import (
    build_plan_revision, build_repair_resolution, effective_plan,
    propose_domain_repair, work_item_result_digest,
)
from target_agent.settings import Settings


def _project(project_id: str = "project-recheck") -> ResearchProjectSpec:
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
        autonomy_mode=AutonomyMode.AUTONOMOUS,
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
        TARGET_AGENT_RUN_DIR=tmp_path / "runs",
        RESEARCH_AGENT_PROJECT_DIR=tmp_path / "projects",
        TARGET_AGENT_CACHE_DIR=tmp_path / "cache",
        TARGET_AGENT_CACHE_ONLY=True,
    )


def _finding(category: str, related_ids: list[str], finding_id: str | None = None,
             message: str = "typed finding", severity: str = "blocking",
             subject: dict | None = None) -> dict:
    return {
        "finding_id": finding_id or f"finding-{category}",
        "category": category,
        "severity": severity,
        "related_ids": related_ids,
        "subject": subject or {},
        "message": message,
    }


class FakeFindingTargetModule:
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
            "derived_claims": [
                {
                    "claim_id": "claim-overstated",
                    "claim_class": "OBSERVED",
                    "statement": "Gene X drives disease Y.",
                    "evidence_ids": ["ev-genetics"],
                },
            ],
            "evidence_items": [
                {"evidence_id": "ev-genetics", "lane": "genetics", "context_match_score": 0.9},
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
                evidence_refs=["ev-genetics"],
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
            name=self.name, description="Emit typed status assessments",
            input_types=("object",), output_types=("object",),
            execution_policy="deterministic_test", side_effect_free=True, replay_safe=True,
        )

    def execute(self, context):
        return ModuleExecution(result=WorkItemResult(
            item_id=context.item.item_id, module=self.name, status=WorkItemStatus.COMPLETED,
            summary="Review completed.", outputs={"assessment_count": 0, "blocking_failures": []},
        ))


class FakeFindingReportModule:
    name = "research_report"

    def __init__(self):
        self.descriptor = ModuleDescriptor(
            name=self.name, description="Render report", input_types=("object",),
            output_types=("object",), execution_policy="deterministic_test",
            side_effect_free=True, replay_safe=True,
        )

    def execute(self, context):
        path = context.output_dir / "research_report.md"
        path.write_text("# Report\n", encoding="utf-8")
        return ModuleExecution(
            result=WorkItemResult(
                item_id=context.item.item_id, module=self.name,
                status=WorkItemStatus.COMPLETED, summary="Report rendered.",
                outputs={"reported_items": len(context.prior_results), "gap_count": 0},
            ),
            artifacts=[PendingArtifact(path, "research_report", "text/markdown")],
        )


def _registry() -> ResearchModuleRegistry:
    return ResearchModuleRegistry([
        FakeFindingTargetModule([]),
        FakeFindingBriefModule(),
        FakeFindingReviewModule(),
        FakeFindingReportModule(),
        DomainOverlayModule(),
    ])


def _plan(project: ResearchProjectSpec) -> ResearchPlan:
    return ResearchPlan(
        project_id=project.project_id, planner_backend="deterministic_test",
        rationale="repair recheck unit test",
        items=[
            _work_item("target_discovery", "target_discovery"),
            _work_item("independent_review", "independent_review", ["target_discovery"]),
            _work_item("research_report", "research_report", ["independent_review"]),
        ],
    )


def _propose_downgrade(tmp_path, registry, project, plan):
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
            result=AssessmentResult.FAIL, actor="fake_independent_review",
            method="typed_domain_review", rationale="blocking domain finding", blocking=True,
        ),
    ]
    request = propose_domain_repair(
        project=project, base_plan=plan, plan=plan,
        results={"target_discovery": source}, assessments=assessments,
        artifacts=[], revisions=[], registry=registry,
    )
    assert request is not None and request.action == RepairAction.DOWNGRADE_CLAIM
    return request, source


def _resolve_overlay(tmp_path, registry, project, plan, request, source, *, actor, sabotage=False):
    blocking = [
        AssessmentRecord(
            project_id=project.project_id, target_id="target_discovery",
            target_digest=work_item_result_digest(source),
            dimension=AssessmentDimension.ENTAILMENT, level=AssessmentLevel.A0,
            result=AssessmentResult.FAIL, actor="fake_independent_review",
            method="typed_domain_review", rationale="blocking domain finding", blocking=True,
        ),
    ]
    revision = build_plan_revision(
        request=request, base_plan=plan, plan=plan,
        assessments=blocking, artifacts=[], revisions=[],
    )
    revised_plan = effective_plan(plan, [revision])
    added = next(row for row in revision.added_items if row.rerun_of_item_id == request.target_work_item_id)
    overlay_id = added.item_id
    overlay_module = registry.get("domain_overlay")
    overlay_result = overlay_module.execute(type("Ctx", (), {
        "item": added, "output_dir": tmp_path, "project": project, "project_dir": tmp_path,
        "cache_dir": tmp_path / "cache", "settings": _settings(tmp_path),
        "prior_results": {"target_discovery": source}, "artifacts": [],
    })()).result
    assert overlay_result.status == WorkItemStatus.COMPLETED
    if sabotage:
        for row in overlay_result.outputs["derived_claims"]:
            if row["claim_id"] == "claim-overstated":
                row["claim_class"] = "OBSERVED"
                row.pop("causal_interpretation_removed", None)
    verification = [
        AssessmentRecord(
            project_id=project.project_id, target_id=overlay_id,
            target_digest=work_item_result_digest(overlay_result),
            dimension=AssessmentDimension.METHODOLOGY, level=AssessmentLevel.A0,
            result=AssessmentResult.PASS, actor=actor,
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
    return build_repair_resolution(
        request=request, revision=revision, project=project, plan=revised_plan,
        results=results, assessments=verification, artifacts=[], revisions=[revision],
        exhausted=False,
    )


def test_fake_independent_review_alone_cannot_close_the_repair_loop(tmp_path):
    project = _project()
    plan = _plan(project)
    registry = _registry()
    request, source = _propose_downgrade(tmp_path, registry, project, plan)
    resolution = _resolve_overlay(
        tmp_path, registry, project, plan, request, source,
        actor="fake_independent_review",
    )
    assert resolution is not None
    assert resolution.status == RepairResolutionStatus.UNRESOLVED


def test_typed_status_gate_alone_cannot_resolve_when_original_charge_is_unrepaired(tmp_path):
    project = _project()
    plan = _plan(project)
    registry = _registry()
    request, source = _propose_downgrade(tmp_path, registry, project, plan)
    resolution = _resolve_overlay(
        tmp_path, registry, project, plan, request, source,
        actor="independent_review", sabotage=True,
    )
    assert resolution is not None
    assert resolution.status == RepairResolutionStatus.UNRESOLVED
    assert "original-finding recheck" in resolution.rationale


def test_independent_review_with_substantive_recheck_resolves(tmp_path):
    project = _project()
    plan = _plan(project)
    registry = _registry()
    request, source = _propose_downgrade(tmp_path, registry, project, plan)
    resolution = _resolve_overlay(
        tmp_path, registry, project, plan, request, source,
        actor="independent_review",
    )
    assert resolution is not None
    assert resolution.status == RepairResolutionStatus.RESOLVED
    assert '"finding_id": "finding-causal"' in resolution.rationale
    payload = json.loads(resolution.rationale.split(": ", 1)[1])
    assert payload["finding_rechecks"]["finding-causal"]["passed"] is True