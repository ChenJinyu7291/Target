from __future__ import annotations

import json
from collections import Counter

import pytest

from target_agent.research_contracts import (
    AutonomyMode,
    DecisionAction,
    ForkDirective,
    ForkMode,
    PlanBranch,
    PlanBranchStatus,
    ProjectState,
    ProjectStatus,
    ResearchGoal,
    ResearchPlanRevision,
    ResearchProjectSpec,
    WorkItemSpec,
)
from target_agent.research_modules import ModuleExecution, ResearchModuleRegistry
from target_agent.research_planner import ResearchPlanner
from target_agent.research_repair import work_item_result_digest
from target_agent.research_runtime import ResearchProjectRuntime
from target_agent.research_service import ResearchDecisionError, ResearchProjectService
from target_agent.research_store import ResearchProjectStore
from target_agent.settings import Settings
from target_agent.webapp import create_app

from .test_research_runtime import (
    BASELINE_MODULES,
    FakeResearchModule,
    fake_research_runtime,
    research_project,
)
from .test_runtime import fake_runtime as fake_target_runtime


def _new_id(prefix: str) -> str:
    return f"{prefix}-{ResearchProjectRuntime._new_contract_id(prefix).split('-', 1)[1]}"


def fork_runtime(tmp_path, *, record_count_aware: bool = False):
    """Deterministic runtime whose literature module can vary its output by input."""
    calls: Counter = Counter()
    modules = []
    for name in (*BASELINE_MODULES, "target_discovery"):
        if name == "literature_search" and record_count_aware:
            modules.append(_RecordCountLiteratureModule(name, calls))
        else:
            modules.append(FakeResearchModule(name, calls))
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


class _RecordCountLiteratureModule(FakeResearchModule):
    """Literature module whose output record_count follows the work item input."""

    def execute(self, context):
        execution = super().execute(context)
        if self.descriptor.name != "literature_search":
            return execution
        outputs = dict(execution.result.outputs)
        outputs["record_count"] = int(context.item.inputs.get("record_count", 1))
        result = execution.result.model_copy(update={"outputs": outputs})
        artifact = execution.artifacts[0]
        artifact.path.write_text(json.dumps(outputs, ensure_ascii=False), encoding="utf-8")
        return ModuleExecution(
            result=result,
            artifacts=execution.artifacts,
            assessments=execution.assessments,
        )


def _branch(store: ResearchProjectStore, branch_id: str) -> PlanBranch:
    branch = store.current_branch(branch_id)
    assert branch is not None
    return branch


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------

def test_fork_contracts_reject_invalid_bindings_and_transitions():
    with pytest.raises(ValueError, match="exactly one"):
        WorkItemSpec(
            item_id="rerun",
            title="Rerun item",
            module="literature_search",
            objective="Rerun the search.",
            acceptance_criteria=["done"],
            rerun_of_item_id="literature_search",
        )
    with pytest.raises(ValueError, match="rollback_to_attempt_id"):
        ForkDirective(
            fork_directive_id=_new_id("fork"),
            project_id="project-test",
            branch_id=_new_id("branch"),
            target_work_item_id="literature_search",
            mode=ForkMode.RESTORE,
            snapshot_digest="0" * 64,
            rationale="Restore the earlier result.",
            actor="user",
        )
    with pytest.raises(ValueError, match="exactly one"):
        ResearchPlanRevision(
            revision_id=_new_id("revision"),
            project_id="project-test",
            base_plan_id="plan-test",
            revision_number=1,
            operation="fork_rollback",
            added_items=[
                WorkItemSpec(
                    item_id="literature_search__fork_1",
                    title="Rerun literature",
                    module="literature_search",
                    objective="Rerun the search.",
                    acceptance_criteria=["done"],
                    rerun_of_item_id="literature_search",
                )
            ],
            superseded_item_ids=["literature_search"],
            trigger_snapshot_digest="0" * 64,
            revision_digest="0" * 64,
            approval_required=False,
        )
    branch = PlanBranch(
        branch_id=_new_id("branch"),
        project_id="project-test",
        base_plan_id="plan-test",
        fork_directive_id=_new_id("fork"),
        fork_point_item_id="literature_search",
        mode=ForkMode.REDO,
        fork_count=1,
        superseded_item_ids=["literature_search"],
        added_item_ids=["literature_search__fork_1"],
        before_snapshot_digest="0" * 64,
        status=PlanBranchStatus.PROPOSED,
    )
    with pytest.raises(ValueError, match="applied branch requires"):
        PlanBranch.model_validate({**branch.model_dump(), "status": PlanBranchStatus.APPLIED})


# ---------------------------------------------------------------------------
# Autonomous redo fork
# ---------------------------------------------------------------------------

def test_autonomous_redo_fork_applies_and_reruns_descendant_closure(tmp_path):
    runtime, calls = fork_runtime(tmp_path)
    service = ResearchProjectService(runtime)
    project = research_project("project-fork-autonomous")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)

    first = runtime.run(project)
    assert first["status"] == ProjectStatus.COMPLETED.value
    assert calls["literature_search"] == 1

    proposed = service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="redo",
        rationale="The literature evidence step should be re-run after review.",
        actor="scientist",
    )
    assert proposed["state"]["status"] == ProjectStatus.WAITING_REVIEW.value
    branches = store.read_branches()
    assert len(branches) == 1
    assert branches[0].status == PlanBranchStatus.PROPOSED
    assert branches[0].superseded_item_ids == ["literature_search", "hypothesis_generation",
                                                "independent_review", "research_report"]

    terminal = runtime.run(project, resume=True)
    assert terminal["status"] == ProjectStatus.COMPLETED.value
    assert calls["literature_search"] == 2
    assert calls["hypothesis_generation"] == 2
    assert calls["independent_review"] == 2
    assert calls["research_report"] == 2
    assert calls["project_brief"] == 1

    branch = _branch(store, branches[0].branch_id)
    assert branch.status == PlanBranchStatus.RESOLVED
    assert branch.revision_id is not None
    revisions = store.read_plan_revisions()
    assert len(revisions) == 1
    assert revisions[0].operation == "fork_rollback"
    assert revisions[0].fork_branch_id == branch.branch_id
    results = store.load_work_item_results()
    assert results["literature_search__fork_1"].fork_branch_id == branch.branch_id
    assert results["literature_search"].supersedes_result_digest is None
    assert results["literature_search__fork_1"].supersedes_result_digest is not None
    store.assert_integrity()


def test_checkpointed_redo_fork_waits_for_approval_then_release(tmp_path):
    runtime, calls = fork_runtime(tmp_path)
    service = ResearchProjectService(runtime)
    project = research_project(
        "project-fork-checkpointed", autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)

    first = runtime.run(project)
    assert first["status"] == ProjectStatus.NEEDS_INPUT.value
    plan = store.load_plan()
    assert plan is not None
    service.accept_checkpoint(
        project_id=project.project_id,
        target_id=plan.plan_id,
        actor="reviewer",
        rationale="Plan is in scope.",
        resume=True,
    )
    assert store.load_state().status == ProjectStatus.WAITING_REVIEW

    proposed = service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="redo",
        rationale="Reviewer requested a re-run of the literature evidence step.",
        actor="scientist",
    )
    assert proposed["state"]["checkpoint_kind"] == "fork"
    assert proposed["next_actions"][0]["action"] == "decide_fork"
    branch = store.read_branches()[0]

    decided = service.decide_fork(
        project_id=project.project_id,
        branch_id=branch.branch_id,
        approve=True,
        actor="reviewer",
        rationale="Approve the bounded redo fork.",
        resume=True,
    )
    assert decided["project"]["state"]["status"] == ProjectStatus.WAITING_REVIEW.value
    assert _branch(store, branch.branch_id).status == PlanBranchStatus.RESOLVED
    assert calls["literature_search"] == 2

    release_target = decided["project"]["next_actions"][0]["target_id"]
    assert release_target.startswith("release:")
    service.accept_checkpoint(
        project_id=project.project_id,
        target_id=release_target,
        actor="reviewer",
        rationale="Release the re-run report.",
        resume=True,
    )
    assert store.load_state().status == ProjectStatus.COMPLETED
    assert _branch(store, branch.branch_id).status == PlanBranchStatus.RESOLVED
    store.assert_integrity()


def test_rejected_fork_keeps_the_project_moving(tmp_path):
    runtime, calls = fork_runtime(tmp_path)
    service = ResearchProjectService(runtime)
    project = research_project("project-fork-rejected")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    runtime.run(project)

    service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="redo",
        rationale="A redo that should be declined.",
        actor="scientist",
    )
    branch = store.read_branches()[0]
    service.decide_fork(
        project_id=project.project_id,
        branch_id=branch.branch_id,
        approve=False,
        actor="reviewer",
        rationale="Decline: the current evidence is adequate.",
        resume=True,
    )
    assert _branch(store, branch.branch_id).status == PlanBranchStatus.REJECTED
    assert store.read_plan_revisions() == []
    assert store.load_state().status == ProjectStatus.COMPLETED
    assert calls["literature_search"] == 1
    store.assert_integrity()


# ---------------------------------------------------------------------------
# Restore fork
# ---------------------------------------------------------------------------

def test_restore_fork_restores_historical_attempt_and_invalidates_descendants(tmp_path):
    runtime, calls = fork_runtime(tmp_path, record_count_aware=True)
    service = ResearchProjectService(runtime)
    project = research_project("project-fork-restore")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)

    runtime.run(project)
    attempt_one = store.current_attempt("literature_search")
    assert attempt_one is not None
    assert store.load_work_item_results()["literature_search"].outputs["record_count"] == 1

    # A redo with an input override produces a newer, different attempt.
    service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="redo",
        rationale="Increase the literature record count.",
        actor="scientist",
        input_overrides={"literature_search": {"record_count": 2}},
    )
    runtime.run(project, resume=True)
    attempt_two = store.current_attempt("literature_search__fork_1")
    assert attempt_two is not None and attempt_two.attempt_id != attempt_one.attempt_id
    assert store.load_work_item_results()["literature_search__fork_1"].outputs["record_count"] == 2
    assert calls["literature_search"] == 2

    # Restore always requires approval, even in autonomous mode.
    service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="restore",
        rollback_to_attempt_id=attempt_one.attempt_id,
        rationale="Restore the first literature pass.",
        actor="scientist",
    )
    restore_branch = store.read_branches()[-1]
    assert restore_branch.mode == ForkMode.RESTORE
    assert store.load_state().status == ProjectStatus.WAITING_REVIEW
    service.decide_fork(
        project_id=project.project_id,
        branch_id=restore_branch.branch_id,
        approve=True,
        actor="reviewer",
        rationale="Approve restoring attempt one.",
        resume=True,
    )

    assert store.load_state().status == ProjectStatus.COMPLETED
    results = store.load_work_item_results()
    assert results["literature_search"].outputs["record_count"] == 1
    assert results["literature_search"].fork_branch_id is None
    # The active fork point now carries the re-bound original payload.
    assert results["literature_search__fork_1"].outputs["record_count"] == 1
    assert results["literature_search__fork_1"].fork_branch_id == restore_branch.branch_id
    assert results["literature_search__fork_1"].supersedes_result_digest is not None
    # The restored result is the original attempt payload, not a new execution:
    # the base item keeps its single historical attempt, and the active fork
    # point gains one head-backed restore bookkeeping attempt on top of its
    # superseded redo attempt.
    assert len(store.read_attempts("literature_search")) == 1
    assert len(store.read_attempts("literature_search__fork_1")) == 2
    assert calls["literature_search"] == 2
    # Descendants were re-run on the restored base.
    assert results["hypothesis_generation__fork_1__fork_2"].status.value == "completed"
    assert results["independent_review__fork_1__fork_2"].status.value == "completed"
    assert results["research_report__fork_1__fork_2"].status.value == "completed"
    assert _branch(store, restore_branch.branch_id).status == PlanBranchStatus.RESOLVED
    store.assert_integrity()


def test_restore_fork_rejects_missing_or_failed_attempt(tmp_path):
    runtime, _ = fork_runtime(tmp_path)
    service = ResearchProjectService(runtime)
    project = research_project("project-fork-bad-restore")
    runtime.run(project)

    with pytest.raises(ResearchDecisionError, match="not a terminal completed result"):
        service.propose_fork(
            project_id=project.project_id,
            target_work_item_id="literature_search",
            mode="restore",
            rollback_to_attempt_id="attempt-" + "0" * 24,
            rationale="Restore a missing attempt.",
            actor="scientist",
        )


# ---------------------------------------------------------------------------
# Budget and integrity
# ---------------------------------------------------------------------------

def test_fork_budget_is_enforced(tmp_path):
    runtime, _ = fork_runtime(tmp_path)
    service = ResearchProjectService(runtime)
    project = research_project("project-fork-budget").model_copy(
        update={"max_forks": 1}
    )
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    runtime.run(project)
    service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="redo",
        rationale="First fork.",
        actor="scientist",
    )
    with pytest.raises(ResearchDecisionError, match="budget exhausted"):
        service.propose_fork(
            project_id=project.project_id,
            target_work_item_id="hypothesis_generation",
            mode="redo",
            rationale="Second fork must be refused.",
            actor="scientist",
        )


def test_integrity_detects_fork_revision_tampering(tmp_path):
    runtime, _ = fork_runtime(tmp_path)
    service = ResearchProjectService(runtime)
    project = research_project("project-fork-tamper")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    runtime.run(project)
    service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="redo",
        rationale="Fork to tamper with.",
        actor="scientist",
    )
    runtime.run(project, resume=True)

    revision_path = next((store.project_dir / "plan_revisions").glob("*.json"))
    payload = json.loads(revision_path.read_text(encoding="utf-8"))
    payload["revision_digest"] = "0" * 64
    revision_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="plan revision digest mismatch"):
        store.assert_integrity()


def test_attempt_result_snapshot_is_immutable_and_required(tmp_path):
    runtime, _ = fork_runtime(tmp_path)
    project = research_project("project-attempt-snapshot")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    runtime.run(project)
    attempt = store.current_attempt("literature_search")
    assert attempt is not None
    snapshot = store.load_attempt_result(attempt.attempt_id)
    assert snapshot is not None
    snapshot_path = (
        store.project_dir / "work_items" / "literature_search"
        / "attempts" / f"{attempt.attempt_id}.result.json"
    )
    snapshot_path.unlink()
    with pytest.raises(ValueError, match="result snapshot is missing"):
        store.assert_integrity()


# ---------------------------------------------------------------------------
# Web API
# ---------------------------------------------------------------------------

def test_web_api_proposes_decides_and_lists_forks(tmp_path):
    research_runtime, _ = fake_research_runtime(tmp_path)
    app = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    )
    client = app.test_client()
    project = research_project("project-web-fork")

    created = client.post("/api/projects", json=project.model_dump(mode="json"))
    assert created.status_code == 202
    deadline = 3.0
    import time
    start = time.monotonic()
    status = None
    while time.monotonic() - start < deadline:
        payload = client.get(f"/api/projects/{project.project_id}").get_json() or {}
        state = payload.get("state") or {}
        status = state.get("status")
        if status == "completed":
            break
        time.sleep(0.01)
    assert status == "completed"

    proposed = client.post(
        f"/api/projects/{project.project_id}/forks",
        json={
            "target_work_item_id": "literature_search",
            "mode": "redo",
            "rationale": "Re-run literature from the web console.",
            "actor": "scientist",
        },
    )
    assert proposed.status_code == 202
    branch = proposed.get_json()["fork"]["plan_branches"][0]

    branches = client.get(f"/api/projects/{project.project_id}/branches")
    assert branches.status_code == 200
    assert branches.get_json()["branches"][0]["branch_id"] == branch["branch_id"]

    decided = client.post(
        f"/api/projects/{project.project_id}/forks/{branch['branch_id']}/decision",
        json={
            "approve": True,
            "actor": "reviewer",
            "rationale": "Approve the web-issued fork.",
        },
    )
    assert decided.status_code == 202
    assert decided.get_json()["resume_queued"] is True

    start = time.monotonic()
    status = None
    while time.monotonic() - start < deadline:
        payload = client.get(f"/api/projects/{project.project_id}").get_json() or {}
        state = payload.get("state") or {}
        status = state.get("status")
        if status == "completed":
            break
        time.sleep(0.01)
    assert status == "completed"
    final_branches = client.get(f"/api/projects/{project.project_id}/branches").get_json()
    assert final_branches["branches"][0]["status"] == "resolved"
    assert len(final_branches["fork_directives"]) == 1

def test_restore_fork_rebinds_head_and_attempt_without_rerunning_target(tmp_path):
    runtime, calls = fork_runtime(tmp_path, record_count_aware=True)
    service = ResearchProjectService(runtime)
    project = research_project("project-fork-restore-head")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)

    runtime.run(project)
    attempt_one = store.current_attempt("literature_search")
    assert attempt_one is not None

    service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="redo",
        rationale="Increase the literature record count.",
        actor="scientist",
        input_overrides={"literature_search": {"record_count": 2}},
    )
    runtime.run(project, resume=True)
    assert store.load_work_item_results()["literature_search__fork_1"].outputs["record_count"] == 2

    service.propose_fork(
        project_id=project.project_id,
        target_work_item_id="literature_search",
        mode="restore",
        rollback_to_attempt_id=attempt_one.attempt_id,
        rationale="Restore the first literature pass.",
        actor="scientist",
    )
    restore_branch = store.read_branches()[-1]
    service.decide_fork(
        project_id=project.project_id,
        branch_id=restore_branch.branch_id,
        approve=True,
        actor="reviewer",
        rationale="Approve restoring attempt one.",
        resume=True,
    )

    results = store.load_work_item_results()
    restored = results["literature_search__fork_1"]
    assert restored.outputs["record_count"] == 1
    restored_digest = work_item_result_digest(restored)
    head = store.read_work_item_head("literature_search__fork_1")
    assert head is not None and head.result_digest == restored_digest
    restore_attempt = next(
        row for row in store.read_attempts("literature_search__fork_1")
        if row.attempt_id == head.attempt_id
    )
    assert restore_attempt.supersedes_attempt_id == attempt_one.attempt_id
    assert restore_attempt.output_digest == restored_digest
    snapshot = store.load_attempt_result(head.attempt_id)
    assert snapshot is not None and work_item_result_digest(snapshot) == restored_digest
    # The target was restored, not re-executed.
    assert calls["literature_search"] == 2
    # head/attempt/result are consistent without relying on a descendant rerun.
    store.assert_integrity()
    # A later recovery pass must not discard the restored payload.
    recovered = store.recover_work_item_results()
    assert work_item_result_digest(recovered["literature_search__fork_1"]) == restored_digest
