from __future__ import annotations

import json
from collections import Counter
from types import SimpleNamespace

import pytest
import requests

from target_agent.contracts import TaskContext, TaskSpec, ToolDescriptor, TraceEvent
from target_agent.research_contracts import (
    AssessmentDimension, AssessmentLevel, AssessmentRecord, AssessmentResult, AutonomyMode,
    DecisionAction, DecisionEvent, FailureClass, ProjectState, ProjectStatus,
    RepairResolutionStatus, ResearchGoal,
    ResearchPlan, ResearchPlanRevision, ResearchProjectSpec,
    WorkAttempt, WorkAttemptStatus, WorkItemHead, WorkItemResult, WorkItemSpec, WorkItemStatus,
    WorkerLease,
)
from target_agent.research_modules import (
    ModuleDescriptor,
    ModuleExecution,
    PendingArtifact,
    ResearchModuleRegistry,
    default_research_registry,
)
from target_agent.research_planner import ResearchPlanner
from target_agent.research_repair import (
    active_item_ids, build_plan_revision, classify_exception, effective_plan,
    work_item_result_digest,
)
from target_agent.research_runtime import ResearchProjectRuntime
from target_agent.research_service import ResearchDecisionError, ResearchProjectService
from target_agent.research_store import ResearchProjectStore
from target_agent.store import EvidenceStore
from target_agent.settings import Settings


BASELINE_MODULES = (
    "project_brief",
    "literature_search",
    "hypothesis_generation",
    "independent_review",
    "research_report",
)


def test_exception_classification_does_not_retry_http_4xx_or_local_permission_errors():
    bad_request = requests.HTTPError(response=requests.Response())
    bad_request.response.status_code = 400
    unavailable = requests.HTTPError(response=requests.Response())
    unavailable.response.status_code = 503

    assert classify_exception(bad_request).value == "permanent"
    assert classify_exception(unavailable).value == "transient"
    assert classify_exception(PermissionError(13, "denied")).value == "permanent"


def research_project(
    project_id: str = "project-runtime",
    *,
    domain: str = "life_science",
    context: dict | None = None,
    autonomy_mode: AutonomyMode = AutonomyMode.AUTONOMOUS,
) -> ResearchProjectSpec:
    return ResearchProjectSpec(
        project_id=project_id,
        title="Traceable disease mechanism project",
        domain=domain,
        goal=ResearchGoal(
            question="Which disease mechanisms and targets are supported by public evidence?",
            success_criteria=["Every released conclusion is traceable to a durable artifact."],
            deliverables=["A reviewed research report with explicit evidence gaps."],
        ),
        context=context or {},
        autonomy_mode=autonomy_mode,
    )


class FakeResearchModule:
    """Deterministic module used to exercise orchestration, never a network adapter."""

    def __init__(
        self,
        name: str,
        calls: Counter,
        *,
        fail_module: str | None = None,
        transient_fail_once: str | None = None,
        bad_contract_module: str | None = None,
        domain_gap_module: str | None = None,
    ):
        self.descriptor = ModuleDescriptor(
            name=name,
            description=f"Deterministic fake module for {name}",
            input_types=("object",),
            output_types=("object",),
            execution_policy="deterministic_test",
            side_effect_free=True,
            replay_safe=True,
            repair_modes=("same_input_retry",) if name in {"literature_search", "target_discovery"} else (),
        )
        self.calls = calls
        self.fail_module = fail_module
        self.transient_fail_once = transient_fail_once
        self.bad_contract_module = bad_contract_module
        self.domain_gap_module = domain_gap_module

    def execute(self, context):
        name = self.descriptor.name
        assessments = []
        self.calls[name] += 1
        if name == self.fail_module:
            raise RuntimeError("synthetic module failure")
        if name == self.transient_fail_once and self.calls[name] == 1:
            raise ConnectionError("synthetic transient connector failure")

        if name == "project_brief":
            outputs = {
                "question": context.project.goal.question,
                "deliverables": context.project.goal.deliverables,
                "success_criteria": context.project.goal.success_criteria,
            }
            status = WorkItemStatus.COMPLETED
        elif name == "literature_search":
            outputs = {
                "query": context.project.goal.question,
                "record_count": 1,
                "source_ids": ["PMID:FAKE-1"],
                "retrieval_hits_are_claims": False,
            }
            status = WorkItemStatus.COMPLETED
        elif name == "hypothesis_generation":
            outputs = {
                "hypothesis_count": 1,
                "hypotheses": [{
                    "statement": "A falsifiable test hypothesis",
                    "source_ids": ["PMID:FAKE-1"],
                    "falsification_test": "Reject if the prespecified perturbation has no effect.",
                }],
            }
            status = WorkItemStatus.COMPLETED
        elif name == "target_discovery":
            if "target_task_spec" not in context.project.context:
                return ModuleExecution(result=WorkItemResult(
                    item_id=context.item.item_id,
                    module=name,
                    status=WorkItemStatus.NEEDS_INPUT,
                    summary="A target_task_spec is required for the vertical workflow.",
                    limitations=["No disease-target input contract was supplied."],
                ))
            outputs = {
                "child_run_id": "run-fake-target", "terminal_status": "completed",
                "ranked_target_count": 10, "target_card_count": 5,
                "experiment_plan_count": 5, "deliverables_complete": True,
                "domain_activity_projection_complete": True,
            }
            status = WorkItemStatus.COMPLETED
        elif name == "independent_review":
            blocking = [
                item_id for item_id, result in context.prior_results.items()
                if result.status in {WorkItemStatus.FAILED, WorkItemStatus.NEEDS_INPUT}
            ]
            outputs = {"assessment_count": len(context.prior_results), "blocking_failures": blocking}
            status = WorkItemStatus.COMPLETED_WITH_GAPS if blocking else WorkItemStatus.COMPLETED
            assessments = [
                AssessmentRecord(
                    project_id=context.project.project_id,
                    target_id=item_id,
                    target_digest=work_item_result_digest(result),
                    dimension=AssessmentDimension.METHODOLOGY,
                    level=AssessmentLevel.A0,
                    result=(AssessmentResult.FAIL if item_id in blocking else AssessmentResult.PASS),
                    actor="independent_review",
                    method="typed_status_gate",
                    rationale=f"Observed {result.status.value}.",
                    blocking=item_id in blocking,
                )
                for item_id, result in context.prior_results.items()
            ]
            if self.domain_gap_module is not None:
                gap_result = context.prior_results.get(self.domain_gap_module)
                if gap_result is not None and gap_result.status == WorkItemStatus.COMPLETED_WITH_GAPS:
                    gap_candidates = gap_result.outputs.get("dataset_candidates") or []
                    if any(
                        isinstance(row, dict) and str(row.get("status") or "") in {
                            "rejected", "ineligible", "unqualified",
                        }
                        for row in gap_candidates
                    ):
                        assessments.append(AssessmentRecord(
                            project_id=context.project.project_id,
                            target_id=self.domain_gap_module,
                            target_digest=work_item_result_digest(gap_result),
                            dimension=AssessmentDimension.METHODOLOGY,
                            level=AssessmentLevel.A0,
                            result=AssessmentResult.FAIL,
                            actor="independent_review",
                            method="typed_dataset_gate",
                            rationale="Preferred dataset rejected; same-context repair is eligible.",
                            blocking=True,
                        ))
        elif name == "research_report":
            gaps = [
                item_id for item_id, result in context.prior_results.items()
                if result.status != WorkItemStatus.COMPLETED
            ]
            outputs = {"reported_items": len(context.prior_results), "gap_count": len(gaps)}
            status = WorkItemStatus.COMPLETED_WITH_GAPS if gaps else WorkItemStatus.COMPLETED
        else:  # pragma: no cover - registry construction fixes the module set
            raise AssertionError(f"unexpected fake module {name}")

        if name == self.bad_contract_module:
            outputs = {"wrong_field": True}

        suffix = ".md" if name == "research_report" else ".json"
        artifact_path = context.output_dir / f"{name}{suffix}"
        if suffix == ".md":
            artifact_path.write_text(
                "# Fake research report\n\nAll content comes from typed fake results.\n",
                encoding="utf-8",
            )
            media_type = "text/markdown"
        else:
            artifact_path.write_text(json.dumps(outputs, ensure_ascii=False), encoding="utf-8")
            media_type = "application/json"
        logical_name = "research_report" if name == "research_report" else f"{name}_output"
        return ModuleExecution(
            result=WorkItemResult(
                item_id=context.item.item_id,
                module=name,
                status=status,
                summary=f"Fake {name} completed.",
                outputs=outputs,
            ),
            artifacts=[PendingArtifact(artifact_path, logical_name, media_type)],
            assessments=assessments,
        )



class FakeDomainGapModule:
    """target_discovery stand-in that reports a rejected preferred dataset
    with a same-context qualified alternative, then succeeds on the rerun."""

    def __init__(self, name: str, calls: Counter):
        self.descriptor = ModuleDescriptor(
            name=name,
            description="Deterministic fake domain module exercising dataset-switch repair",
            input_types=("object",),
            output_types=("object",),
            execution_policy="deterministic_test",
            side_effect_free=True,
            replay_safe=True,
            repair_modes=("same_input_retry", "alternate_dataset"),
        )
        self.calls = calls

    def execute(self, context):
        name = self.descriptor.name
        self.calls[name] += 1
        is_rerun = context.item.rerun_of_item_id is not None
        outputs = {
            "child_run_id": f"run-fake-target-{context.item.item_id}",
            "terminal_status": "completed" if is_rerun else "completed_with_gaps",
            "ranked_target_count": 10,
            "target_card_count": 5,
            "experiment_plan_count": 5,
            "deliverables_complete": is_rerun,
            "domain_activity_projection_complete": True,
        }
        if is_rerun:
            override = context.item.inputs.get("dataset_override") or {}
            outputs["selected_accession"] = (override.get("preferred_dataset_accessions") or [None])[0]
            outputs["dataset_candidates"] = [
                {"accession": "GSE99999", "status": "qualified"},
            ]
            status = WorkItemStatus.COMPLETED
            summary = "Fake target discovery succeeded with the replacement dataset."
        else:
            outputs["dataset_candidates"] = [
                {"accession": "GSE11111", "status": "rejected", "reason": "metadata audit failed"},
                {"accession": "GSE99999", "status": "qualified", "tissue": "colon"},
            ]
            status = WorkItemStatus.COMPLETED_WITH_GAPS
            summary = "Fake target discovery gap: preferred dataset rejected."
        result = WorkItemResult(
            item_id=context.item.item_id,
            module=name,
            status=status,
            summary=summary,
            outputs=outputs,
            failure_class=FailureClass.SCIENTIFIC_GAP if not is_rerun else None,
        )
        artifact_path = context.output_dir / f"{name}.json"
        artifact_path.write_text(json.dumps(outputs, ensure_ascii=False), encoding="utf-8")
        return ModuleExecution(
            result=result,
            artifacts=[PendingArtifact(artifact_path, f"{name}_output", "application/json")],
        )


def fake_research_runtime(
    tmp_path,
    *,
    fail_module: str | None = None,
    transient_fail_once: str | None = None,
    bad_contract_module: str | None = None,
):
    calls: Counter = Counter()
    modules = [
        FakeResearchModule(
            name,
            calls,
            fail_module=fail_module,
            transient_fail_once=transient_fail_once,
            bad_contract_module=bad_contract_module,
        )
        for name in (*BASELINE_MODULES, "target_discovery")
    ]
    registry = ResearchModuleRegistry(modules)
    settings = Settings(
        _env_file=None,
        STEP_API_KEY=None,
        TARGET_AGENT_RUN_DIR=tmp_path / "runs",
        RESEARCH_AGENT_PROJECT_DIR=tmp_path / "projects",
        TARGET_AGENT_CACHE_DIR=tmp_path / "cache",
        TARGET_AGENT_CACHE_ONLY=True,
        TARGET_AGENT_WEB_WORKERS=1,
        TARGET_AGENT_WEB_QUEUE_SIZE=2,
    )
    runtime = ResearchProjectRuntime(
        projects_dir=settings.projects_dir,
        cache_dir=settings.cache_dir,
        registry=registry,
        planner=ResearchPlanner(registry, client=None),
        settings=settings,
    )
    return runtime, calls



def fake_domain_repair_runtime(tmp_path):
    calls: Counter = Counter()
    modules = [
        FakeDomainGapModule("target_discovery", calls),
        *(
            FakeResearchModule(name, calls, domain_gap_module="target_discovery")
            for name in BASELINE_MODULES
        ),
    ]
    registry = ResearchModuleRegistry(modules)
    settings = Settings(
        _env_file=None,
        STEP_API_KEY=None,
        TARGET_AGENT_RUN_DIR=tmp_path / "runs",
        RESEARCH_AGENT_PROJECT_DIR=tmp_path / "projects",
        TARGET_AGENT_CACHE_DIR=tmp_path / "cache",
        TARGET_AGENT_CACHE_ONLY=True,
        TARGET_AGENT_WEB_WORKERS=1,
        TARGET_AGENT_WEB_QUEUE_SIZE=2,
    )
    runtime = ResearchProjectRuntime(
        projects_dir=settings.projects_dir,
        cache_dir=settings.cache_dir,
        registry=registry,
        planner=ResearchPlanner(registry, client=None),
        settings=settings,
    )
    return runtime, calls


def test_langgraph_project_runs_end_to_end_and_persists_report(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path)
    project = research_project()

    terminal = runtime.run(project)

    assert terminal["status"] == ProjectStatus.COMPLETED.value
    assert calls == Counter({name: 1 for name in BASELINE_MODULES})
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    assert [event.sequence for event in store.read_events()] == list(range(1, len(store.read_events()) + 1))
    assert set(store.load_work_item_results()) == set(BASELINE_MODULES)
    report = next(row for row in store.read_artifacts() if row.logical_name == "research_report")
    assert store.artifact_path(report).read_text(encoding="utf-8").startswith("# Fake research report")
    assert store.read_decisions()[-1].action.value == "release"


def test_output_contract_failure_degrades_release_and_is_a_blocking_assessment(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path, bad_contract_module="literature_search")
    project = research_project("project-contract-gap")

    terminal = runtime.run(project)

    assert terminal["status"] == ProjectStatus.COMPLETED_WITH_GAPS.value
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    literature = store.load_work_item_results()["literature_search"]
    assert literature.status == WorkItemStatus.COMPLETED_WITH_GAPS
    assert any("missing required output" in limitation for limitation in literature.limitations)
    assert any(
        assessment.target_id == "literature_search"
        and assessment.dimension.value == "schema_alignment"
        and assessment.result.value == "fail"
        and assessment.blocking
        for assessment in store.read_assessments()
    )


def test_module_exception_still_produces_a_durable_gap_report(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path, fail_module="literature_search")
    project = research_project("project-module-failure")

    terminal = runtime.run(project)

    assert terminal["status"] == ProjectStatus.COMPLETED_WITH_GAPS.value
    assert calls["research_report"] == 1
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    assert store.load_work_item_results()["literature_search"].status == WorkItemStatus.FAILED
    report_result = store.load_work_item_results()["research_report"]
    assert report_result.status == WorkItemStatus.COMPLETED_WITH_GAPS
    assert report_result.outputs["gap_count"] >= 1
    assert any(row.logical_name == "research_report" for row in store.read_artifacts())


def test_autonomous_transient_failure_reruns_affected_subgraph_and_rebinds_release(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path, transient_fail_once="literature_search")
    project = research_project("project-transient-repair")

    terminal = runtime.run(project)

    assert terminal["status"] == ProjectStatus.COMPLETED.value
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    requests = store.read_repair_requests()
    revisions = store.read_plan_revisions()
    resolutions = store.read_repair_resolutions()
    assert len(requests) == len(revisions) == len(resolutions) == 1
    assert resolutions[0].status == RepairResolutionStatus.RESOLVED
    assert calls["literature_search"] == 2
    assert calls["hypothesis_generation"] == 1
    assert calls["independent_review"] == 2
    assert calls["research_report"] == 2
    results = store.load_work_item_results()
    assert results["literature_search"].status == WorkItemStatus.FAILED
    assert results["literature_search__repair_1"].status == WorkItemStatus.COMPLETED
    assert results["literature_search__repair_1"].input_digest == requests[0].input_digest
    effective = store.load_effective_plan()
    assert effective is not None
    active = active_item_ids(effective, revisions)
    assert "literature_search" not in active
    assert "literature_search__repair_1" in active
    assert any(row.action == DecisionAction.REPLAN for row in store.read_decisions())
    release = [row for row in store.read_decisions() if row.action == DecisionAction.RELEASE]
    assert len(release) == 1 and release[0].evidence_snapshot_digest
    store.assert_integrity()



def test_checkpointed_domain_repair_switches_dataset_without_changing_context(tmp_path):
    runtime, calls = fake_domain_repair_runtime(tmp_path)
    project = research_project(
        "project-domain-dataset-switch",
        domain="disease_target_discovery",
        context={
            "target_task_spec": {
                "task_type": "disease_to_target",
                "question": "Which targets are supported by public evidence?",
                "context": {"disease": "ulcerative colitis", "tissue": "colon"},
            }
        },
        autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    service = ResearchProjectService(runtime)

    first = runtime.run(project)
    assert first["status"] == ProjectStatus.NEEDS_INPUT.value
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    plan = store.load_plan()
    assert plan is not None
    service.accept_checkpoint(
        project_id=project.project_id, target_id=plan.plan_id,
        actor="reviewer", rationale="Plan is in scope.", resume=True,
    )
    assert store.load_state().status == ProjectStatus.WAITING_REVIEW

    request = store.read_repair_requests()[0]
    assert request.action.value == "switch_dataset_same_context"
    assert request.directive_payload["preferred_dataset_accessions"] == ["GSE99999"]
    assert request.directive_payload["excluded_dataset_accessions"] == ["GSE11111"]
    assert request.no_scope_change is True

    service.decide_repair(
        project_id=project.project_id,
        repair_request_id=request.repair_request_id,
        trigger_snapshot_digest=request.trigger_snapshot_digest,
        approve=True,
        actor="reviewer",
        rationale="Approve same-context dataset replacement.",
        resume=True,
    )
    assert calls["target_discovery"] == 2
    results = store.load_work_item_results()
    repaired = results["target_discovery__repair_1"]
    assert repaired.status == WorkItemStatus.COMPLETED
    assert repaired.outputs["selected_accession"] == "GSE99999"
    assert repaired.input_digest != results["target_discovery"].input_digest

    revision = store.read_plan_revisions()[0]
    added = next(
        item for item in revision.added_items
        if item.rerun_of_item_id == "target_discovery"
    )
    override = added.inputs["dataset_override"]
    assert override["preferred_dataset_accessions"] == ["GSE99999"]
    assert override["excluded_dataset_accessions"] == ["GSE11111"]

    state = store.load_state()
    assert state.status == ProjectStatus.WAITING_REVIEW
    release_target = service.snapshot(project.project_id)["next_actions"][0]["target_id"]
    service.accept_checkpoint(
        project_id=project.project_id, target_id=release_target,
        actor="reviewer", rationale="Release after verified dataset replacement.", resume=True,
    )
    assert store.load_state().status == ProjectStatus.COMPLETED.value
    store.assert_integrity()


def test_integrity_rejects_tampered_plan_revision_digest(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path, transient_fail_once="literature_search")
    project = research_project("project-revision-tamper")
    runtime.run(project)
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    revision_path = next((store.project_dir / "plan_revisions").glob("*.json"))
    payload = json.loads(revision_path.read_text(encoding="utf-8"))
    payload["revision_digest"] = "0" * 64
    revision_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="plan revision digest mismatch"):
        store.assert_integrity()


def test_release_snapshot_ignores_orphan_artifact_not_referenced_by_active_result(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project("project-orphan-artifact")
    service = ResearchProjectService(runtime)
    runtime.run(project)
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    before = service.snapshot(project.project_id)["release_snapshot_digest"]
    orphan = store.project_dir / "work_items" / "literature_search" / "orphan.txt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("not referenced by the active result", encoding="utf-8")
    store.register_artifact(orphan, "literature_search", "orphan", "text/plain")

    assert service.snapshot(project.project_id)["release_snapshot_digest"] == before


def test_checkpointed_repair_requires_exact_snapshot_approval(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path, transient_fail_once="literature_search")
    project = research_project(
        "project-checkpointed-repair", autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    service = ResearchProjectService(runtime)

    first = runtime.run(project)
    assert first["status"] == ProjectStatus.NEEDS_INPUT.value
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    base_plan = store.load_plan()
    assert base_plan is not None
    service.accept_checkpoint(
        project_id=project.project_id,
        target_id=base_plan.plan_id,
        actor="reviewer",
        rationale="Initial plan is in scope.",
        resume=True,
    )
    waiting = store.load_state()
    assert waiting is not None and waiting.status == ProjectStatus.WAITING_REVIEW
    request = store.read_repair_requests()[0]
    assert not store.read_plan_revisions()
    with pytest.raises(ResearchDecisionError, match="stale"):
        service.decide_repair(
            project_id=project.project_id,
            repair_request_id=request.repair_request_id,
            trigger_snapshot_digest="0" * 64,
            approve=True,
            actor="reviewer",
            rationale="stale approval",
        )
    service.decide_repair(
        project_id=project.project_id,
        repair_request_id=request.repair_request_id,
        trigger_snapshot_digest=request.trigger_snapshot_digest,
        approve=True,
        actor="reviewer",
        rationale="Approve bounded same-input retry.",
        resume=True,
    )
    with pytest.raises(ResearchDecisionError, match="opposite decision"):
        service.decide_repair(
            project_id=project.project_id,
            repair_request_id=request.repair_request_id,
            trigger_snapshot_digest=request.trigger_snapshot_digest,
            approve=False,
            actor="reviewer",
            rationale="A non-reversible approval cannot later become rejection.",
        )
    assert calls["literature_search"] == 2
    assert len(store.read_plan_revisions()) == 1
    assert store.load_state().status == ProjectStatus.WAITING_REVIEW
    next_action = service.snapshot(project.project_id)["next_actions"][0]
    assert next_action["target_id"].startswith("release:")
    assert len(next_action["target_id"].removeprefix("release:")) == 64


def test_checkpointed_repair_rejection_resumes_to_explicit_gap_terminal(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path, transient_fail_once="literature_search")
    project = research_project(
        "project-checkpointed-repair-reject", autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    service = ResearchProjectService(runtime)
    runtime.run(project)
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    plan = store.load_plan()
    assert plan is not None
    service.accept_checkpoint(
        project_id=project.project_id, target_id=plan.plan_id,
        actor="reviewer", rationale="Accept initial plan.", resume=True,
    )
    request = store.read_repair_requests()[0]

    terminal = service.decide_repair(
        project_id=project.project_id,
        repair_request_id=request.repair_request_id,
        trigger_snapshot_digest=request.trigger_snapshot_digest,
        approve=False,
        actor="reviewer",
        rationale="Do not retry; retain the failure as a gap.",
        resume=True,
    )["project"]

    assert terminal["state"]["status"] == ProjectStatus.COMPLETED_WITH_GAPS.value
    assert terminal["next_actions"][0]["action"] == "inspect_gaps"
    assert calls["literature_search"] == 1
    assert not store.read_plan_revisions()


def test_checkpointed_revision_without_exact_approval_is_rejected_on_resume(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path, transient_fail_once="literature_search")
    project = research_project(
        "project-unapproved-revision", autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    service = ResearchProjectService(runtime)
    runtime.run(project)
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    base = store.load_plan()
    assert base is not None
    service.accept_checkpoint(
        project_id=project.project_id, target_id=base.plan_id,
        actor="reviewer", rationale="Accept initial plan.", resume=True,
    )
    request = store.read_repair_requests()[0]
    revision = build_plan_revision(
        request=request,
        base_plan=base,
        plan=effective_plan(base, []),
        assessments=store.read_assessments(),
        artifacts=store.read_artifacts(),
        revisions=[],
    )
    store.append_plan_revision(revision)

    with pytest.raises(ValueError, match="lacks exact-snapshot approval"):
        runtime.run(project, resume=True)


def test_resume_of_terminal_project_is_idempotent_and_does_not_rerun_modules(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path)
    project = research_project("project-resume")
    first = runtime.run(project)
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    events_before = [row.model_dump(mode="json") for row in store.read_events()]
    calls_before = calls.copy()

    second = runtime.run(project, resume=True)

    assert second == first
    assert calls == calls_before
    assert [row.model_dump(mode="json") for row in store.read_events()] == events_before


def test_vertical_project_without_target_spec_fails_closed_with_reported_gap(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path)
    project = research_project("project-vertical-missing", domain="disease_target_discovery")

    terminal = runtime.run(project)

    assert terminal["status"] == ProjectStatus.NEEDS_INPUT.value
    assert calls["target_discovery"] == 0
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    target = store.load_work_item_results()["target_discovery"]
    assert target.status == WorkItemStatus.NEEDS_INPUT
    assert any("target_task_spec" in limitation for limitation in target.limitations)
    assert store.load_work_item_results()["research_report"].status == WorkItemStatus.COMPLETED_WITH_GAPS


def test_checkpointed_project_requires_plan_and_release_acceptance(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path)
    project = research_project(
        "project-checkpointed", autonomy_mode=AutonomyMode.CHECKPOINTED,
    )

    first = runtime.run(project)
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    plan = store.load_plan()
    assert first["status"] == ProjectStatus.NEEDS_INPUT.value
    assert not calls

    store.append_decision(DecisionEvent(
        project_id=project.project_id, action=DecisionAction.ACCEPT,
        target_ids=[plan.plan_id], actor="test-reviewer", rationale="Plan is in scope.",
    ))
    second = runtime.run(project, resume=True)
    assert second["status"] == ProjectStatus.WAITING_REVIEW.value
    assert calls["research_report"] == 1

    service = ResearchProjectService(runtime)
    release_target = service.snapshot(project.project_id)["next_actions"][0]["target_id"]
    service.accept_checkpoint(
        project_id=project.project_id,
        target_id=release_target,
        actor="test-reviewer",
        rationale="Release artifacts passed review.",
        resume=True,
    )
    third = store.load_state().model_dump(mode="json")
    assert third["status"] == ProjectStatus.COMPLETED.value


def _target_project(project_id: str) -> ResearchProjectSpec:
    task = TaskSpec(
        task_type="disease_to_target",
        question="Which targets should be prioritized?",
        context=TaskContext(disease="lung adenocarcinoma", tissue="lung"),
    )
    return research_project(
        project_id,
        domain="disease_target_discovery",
        context={"target_task_spec": task.model_dump(mode="json")},
    )


def _real_target_project_runtime(tmp_path):
    settings = Settings(
        _env_file=None,
        STEP_API_KEY=None,
        TARGET_AGENT_RUN_DIR=tmp_path / "runs",
        RESEARCH_AGENT_PROJECT_DIR=tmp_path / "projects",
        TARGET_AGENT_CACHE_DIR=tmp_path / "cache",
        TARGET_AGENT_CACHE_ONLY=True,
    )
    registry = default_research_registry(settings)
    runtime = ResearchProjectRuntime(
        projects_dir=settings.projects_dir,
        cache_dir=settings.cache_dir,
        registry=registry,
        planner=ResearchPlanner(registry, client=None),
        settings=settings,
    )
    return runtime


def test_child_runtime_exception_preserves_trace_artifact_and_gap_report(tmp_path, monkeypatch):
    class CrashingChildRuntime:
        def __init__(self, *, runs_dir, cache_dir, settings, trace_observer):
            self.runs_dir = runs_dir
            self.trace_observer = trace_observer
            self.trace_observer_errors = []
            self.registry = SimpleNamespace(descriptors=[ToolDescriptor(
                tool_id="geo_search", evidence_dimension="dataset_discovery",
                description="GEO search connector.",
            )])

        def run(self, task, run_id, resume=False):
            store = EvidenceStore(self.runs_dir / run_id)
            event = TraceEvent(
                run_id=run_id, task_id=task.task_id, event_type="tool_call",
                state="tool_execution", detail={"tool": "geo_search", "step_id": "geo"},
            )
            store.add_trace(event)
            self.trace_observer(event)
            raise RuntimeError("synthetic child interruption")

    monkeypatch.setattr("target_agent.runtime_langgraph.LangGraphRuntime", CrashingChildRuntime)
    runtime = _real_target_project_runtime(tmp_path)
    project = _target_project("project-child-error")

    terminal = runtime.run(project)

    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    assert terminal["status"] == ProjectStatus.COMPLETED_WITH_GAPS.value
    assert store.load_work_item_results()["target_discovery"].error == "RuntimeError"
    assert len(store.read_domain_activities()) == 1
    assert any(row.logical_name == "target_discovery_trace" for row in store.read_artifacts())
    assert any(row.logical_name == "research_report" for row in store.read_artifacts())
    store.assert_integrity()


def test_interrupted_first_attempt_resumes_once_and_backfills_without_duplicates(tmp_path, monkeypatch):
    class TerminalChildRuntime:
        def __init__(self, *, runs_dir, cache_dir, settings, trace_observer):
            self.runs_dir = runs_dir
            self.trace_observer = trace_observer
            self.trace_observer_errors = []
            self.registry = SimpleNamespace(descriptors=[ToolDescriptor(
                tool_id="open_targets", evidence_dimension="multi_evidence",
                description="Open Targets connector.",
            )])

        def run(self, task, run_id, resume=False):
            assert resume is True
            return {"terminal_status": "completed"}

    monkeypatch.setattr("target_agent.runtime_langgraph.LangGraphRuntime", TerminalChildRuntime)
    runtime = _real_target_project_runtime(tmp_path)
    project = _target_project("project-child-resume")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    store.create(project)
    plan = runtime.planner.deterministic(project)
    store.save_plan(plan)
    store.save_state(ProjectState(
        project_id=project.project_id,
        status=ProjectStatus.RUNNING,
        attempts={"target_discovery": 1},
    ))
    child_run_id = f"target-{project.project_id}"
    child_dir = store.project_dir / "domain_runs" / child_run_id
    child_store = EvidenceStore(child_dir)
    event = TraceEvent(
        run_id=child_run_id,
        task_id=project.context["target_task_spec"]["task_id"],
        event_type="tool_result",
        state="tool_execution",
        detail={
            "tool": "open_targets", "status": "success",
            "coverage_status": "covered", "context_match_score": 0.8,
        },
    )
    child_store.add_trace(event)
    (child_dir / "report.md").write_text("# Child report\n", encoding="utf-8")
    (child_dir / "ranked_targets.json").write_text('[{"gene":"GENE1"}]\n', encoding="utf-8")
    (child_dir / "target_cards.json").write_text(
        '[{"gene_symbol":"GENE1","experiment_plan":{"hypothesis":"test"}}]\n',
        encoding="utf-8",
    )

    terminal = runtime.run(project, resume=True)

    assert terminal["status"] == ProjectStatus.COMPLETED.value
    state = store.load_state()
    assert state.attempts["target_discovery"] == 2
    assert [row.source_trace_id for row in store.read_domain_activities()] == [event.event_id]
    assert len([row for row in store.read_artifacts() if row.logical_name == "target_discovery_trace"]) == 1
    store.assert_integrity()


def test_successful_run_records_terminal_attempts_and_releases_leases(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project("project-attempts")

    terminal = runtime.run(project)

    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    assert terminal["status"] == ProjectStatus.COMPLETED.value
    attempts = store.read_attempts()
    by_item = {row.work_item_id: row for row in attempts}
    assert set(by_item) == set(store.load_work_item_results())
    assert all(row.output_digest for row in attempts)
    assert all(row.completed_at for row in attempts)
    assert all(row.worker_lease_id for row in attempts)
    leases = store.read_leases()
    assert leases
    assert all(row.released_at is not None for row in leases)
    events = store.read_events()
    assert any(row.event_type == "lease_acquired" for row in events)
    assert any(row.event_type == "work_attempt_recorded" for row in events)
    store.assert_integrity()


def test_orphan_lease_is_reclaimed_and_attempts_backfill_on_resume(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project("project-orphan-lease")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    store.create(project)
    plan = runtime.planner.deterministic(project)
    store.save_plan(plan)
    store.save_state(ProjectState(
        project_id=project.project_id, status=ProjectStatus.RUNNING,
    ))
    orphan = WorkerLease(
        lease_id=ResearchProjectRuntime._new_contract_id("lease"),
        project_id=project.project_id,
        work_item_id="literature_search",
        attempt_id=ResearchProjectRuntime._new_contract_id("attempt"),
        worker_id="crashed-worker",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    store.append_lease(orphan)

    terminal = runtime.run(project, resume=True)

    assert terminal["status"] == ProjectStatus.COMPLETED.value
    assert any(row.event_type == "lease_reclaimed" for row in store.read_events())
    attempts = store.read_attempts()
    assert attempts
    assert all(row.status.value in {"completed", "completed_with_gaps"} for row in attempts)
    assert all(row.released_at is not None for row in store.read_leases())
    store.assert_integrity()


def test_read_dataset_candidates_prefers_tool_results_and_normalizes_status(tmp_path):
    import json as _json

    from target_agent.research_modules import TargetDiscoveryModule

    run_dir = tmp_path / "child-run"
    run_dir.mkdir()
    (run_dir / "tool_results.jsonl").write_text(
        _json.dumps({
            "tool_name": "geo_metadata_audit",
            "outputs": {
                "audited_datasets": [
                    {"candidate": {"accession": "GSE1", "eligibility": "eligible", "context_match_score": 0.8}},
                    {"candidate": {"accession": "GSE2", "eligibility": "ineligible",
                                   "exclusion_reasons": ["metadata_confidence_below_gate"]}},
                ],
            },
        }) + "\n",
        encoding="utf-8",
    )
    rows = TargetDiscoveryModule._read_dataset_candidates(run_dir)
    by_accession = {row["accession"]: row for row in rows}
    assert set(by_accession) == {"GSE1", "GSE2"}
    assert by_accession["GSE1"]["status"] == "candidate"
    assert by_accession["GSE2"]["status"] == "rejected"
    assert by_accession["GSE1"]["context_match_score"] == 0.8


def test_read_dataset_candidates_dedupes_across_report_and_trace(tmp_path):
    import json as _json

    from target_agent.research_modules import TargetDiscoveryModule

    run_dir = tmp_path / "child-run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        _json.dumps({"dataset_selection_trace": [
            {"accession": "GSE1", "decision": "rejected", "reasons": ["missing_case_controls"]},
        ]}),
        encoding="utf-8",
    )
    (run_dir / "tool_results.jsonl").write_text(
        _json.dumps({
            "tool_name": "geo_metadata_audit",
            "outputs": {
                "selection_trace": [
                    {"accession": "GSE1", "decision": "selected", "reasons": []},
                    {"accession": "GSE3", "decision": "eligible_not_selected_limit"},
                ],
            },
        }) + "\n",
        encoding="utf-8",
    )
    rows = TargetDiscoveryModule._read_dataset_candidates(run_dir)
    assert len(rows) == 2
    by_accession = {row["accession"]: row for row in rows}
    # tool_results take precedence for the duplicate accession
    assert by_accession["GSE1"]["decision"] == "selected"
    assert by_accession["GSE3"]["status"] == "candidate"

def test_committed_head_recovers_missing_result_mirror_without_reexecution(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path)
    project = research_project("project-head-recovery")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    terminal = runtime.run(project)
    assert terminal["status"] == ProjectStatus.COMPLETED.value
    heads = store.read_work_item_heads()
    assert {head.work_item_id for head in heads} == set(BASELINE_MODULES)
    assert calls == Counter({name: 1 for name in BASELINE_MODULES})

    # Simulate the narrow crash window: attempt/head committed, result.json
    # mirror lost before the next durable write.
    for item_id in BASELINE_MODULES:
        (store.project_dir / "work_items" / item_id / "result.json").unlink()

    terminal = runtime.run(project, resume=True)
    assert terminal["status"] == ProjectStatus.COMPLETED.value
    # No completed step is re-executed; recovery repairs the mirror from the head.
    assert calls == Counter({name: 1 for name in BASELINE_MODULES})
    results = store.load_work_item_results()
    assert set(results) == set(BASELINE_MODULES)
    for head in store.read_work_item_heads():
        assert work_item_result_digest(results[head.work_item_id]) == head.result_digest
    store.assert_integrity()


def test_expired_lease_with_running_attempt_is_reclaimed_on_resume(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project("project-expired-lease")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    store.create(project)
    plan = runtime.planner.deterministic(project)
    store.save_plan(plan)
    store.save_state(ProjectState(
        project_id=project.project_id, status=ProjectStatus.RUNNING,
    ))
    orphan_attempt_id = ResearchProjectRuntime._new_contract_id("attempt")
    orphan = WorkerLease(
        lease_id=ResearchProjectRuntime._new_contract_id("lease"),
        project_id=project.project_id,
        work_item_id="literature_search",
        attempt_id=orphan_attempt_id,
        worker_id="crashed-worker",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    store.append_lease(orphan)
    store.append_attempt(WorkAttempt(
        attempt_id=orphan_attempt_id,
        project_id=project.project_id,
        work_item_id="literature_search",
        attempt_number=1,
        status=WorkAttemptStatus.RUNNING,
        input_digest="0" * 64,
        started_at="2026-01-01T00:00:00+00:00",
    ))

    terminal = runtime.run(project, resume=True)
    assert terminal["status"] == ProjectStatus.COMPLETED.value
    assert any(row.event_type == "lease_reclaimed" for row in store.read_events())
    attempts = store.read_attempts("literature_search")
    assert [row.attempt_number for row in attempts] == [1, 2]
    # the interrupted running attempt stays auditable; the retry commits a result
    assert attempts[0].status.value == "running" and attempts[0].output_digest is None
    assert attempts[1].output_digest is not None
    assert all(row.released_at is not None for row in store.read_leases())
    store.assert_integrity()


def test_review_target_recorded_and_reconciled_after_interruption(tmp_path):
    runtime, calls = fake_research_runtime(tmp_path)
    project = research_project("project-review-target")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    terminal = runtime.run(project)
    assert terminal["status"] == ProjectStatus.COMPLETED.value
    review_item = "independent_review"
    targets = store.read_review_targets()
    assert len(targets) == 1
    assert targets[0].work_item_id == review_item
    assert len(targets[0].snapshot_digest) == 64
    expected_digest = targets[0].snapshot_digest

    # Simulate a crash after the review head committed but before the target row.
    (store.project_dir / "review_targets.jsonl").unlink()
    terminal = runtime.run(project, resume=True)
    assert terminal["status"] == ProjectStatus.COMPLETED.value
    targets = store.read_review_targets()
    assert len(targets) == 1
    assert targets[0].snapshot_digest == expected_digest
    assert calls == Counter({name: 1 for name in BASELINE_MODULES})
    store.assert_integrity()

def test_run_records_structured_worker_failure_and_continues(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project("project-run-crash")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    store.create(project)

    original_invoke = runtime._graph.invoke

    def crashing_invoke(state, **kwargs):
        raise RuntimeError("synthetic worker crash")

    runtime._graph.invoke = crashing_invoke  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="synthetic worker crash"):
        runtime.run(project)

    terminal = [row for row in store.read_events() if row.event_type == "project_terminal"]
    assert terminal
    detail = terminal[-1].detail
    assert detail["task_id"] == project.project_id
    assert detail["error_type"] == "RuntimeError"
    assert "synthetic worker crash" in detail["error_message"]
    assert detail["at"]
    assert store.load_state().status == ProjectStatus.FAILED

    # The executor remains usable: the next task runs to completion after the
    # recorded failure.
    runtime._graph.invoke = original_invoke
    second = research_project("project-run-next")
    runtime.run(second)
    second_store = ResearchProjectStore(runtime.projects_dir, second.project_id)
    assert second_store.load_state().status == ProjectStatus.COMPLETED


def test_repair_revision_missing_request_raises_value_error_with_id(tmp_path):
    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project("project-repair-missing-request")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    store.create(project)
    plan = ResearchPlan(
        project_id=project.project_id,
        items=[WorkItemSpec(
            item_id="literature_search",
            title="Collect literature",
            module="literature_search",
            objective="Gather evidence.",
            acceptance_criteria=["done"],
        )],
        planner_backend="deterministic-test",
        rationale="P3-23 regression fixture.",
    )
    store.save_plan(plan)
    missing_request_id = "repair-" + "a" * 24
    store.append_plan_revision(ResearchPlanRevision(
        revision_id="revision-" + "a" * 24,
        project_id=project.project_id,
        base_plan_id=plan.plan_id,
        revision_number=1,
        repair_request_id=missing_request_id,
        operation="rerun_subgraph_same_inputs",
        added_items=[WorkItemSpec(
            item_id="literature_search__repair_1",
            title="Repair literature",
            module="literature_search",
            objective="Rerun the search.",
            acceptance_criteria=["done"],
            rerun_of_item_id="literature_search",
            repair_request_id=missing_request_id,
        )],
        superseded_item_ids=["literature_search"],
        trigger_snapshot_digest="0" * 64,
        revision_digest="0" * 64,
        approval_required=False,
    ))
    with pytest.raises(ValueError, match=missing_request_id):
        runtime._repair({"store": store, "project": project, "results": {}})
