"""Durable LangGraph runtime for project-level life-science research."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .contracts import utc_now
from .llm import StepClient
from .research_contracts import (
    AssessmentDimension, AssessmentLevel, AssessmentRecord, AssessmentResult, AutonomyMode,
    ControlRequestKind, DataContract,
    DecisionAction, DecisionEvent, FailureClass, ForkDirective, ForkMode, PlanBranch,
    PlanBranchStatus, ProjectState,
    ProjectStatus, RepairAction, RepairAuthorization, RepairResolutionStatus, ResearchPlan,
    ResearchProjectControl, ResearchProjectSpec, ReviewTarget,
    TERMINAL_WORK_ITEM_STATUSES,
    WorkAttempt, WorkAttemptStatus, WorkItemHead, WorkItemResult, WorkItemSpec, WorkItemStatus,
    WorkerLease,
)
from .paper_strategy import pattern_store_from_path
from .paper_rag import paper_rag_store_from_path
from .research_modules import ModuleContext, ResearchModuleRegistry, default_research_registry
from .research_planner import ResearchPlanner
from .workflow_catalog import WorkflowCatalog, WorkflowCatalogError
from .research_projection import DomainActivityProjection
from .research_repair import (
    OVERLAY_ACTIONS,
    active_assessments,
    active_item_ids,
    build_fork_revision,
    build_plan_revision,
    build_repair_resolution,
    canonical_sha256,
    classify_exception,
    effective_plan,
    project_snapshot_digest,
    propose_domain_repair,
    propose_transient_repair,
    review_target_snapshot_digest,
    work_item_result_digest,
)
from .research_store import LEASE_DURATION, ProjectBusyError, ResearchProjectStore
from .skill_catalog import SkillCatalog
from .settings import Settings, load_settings



class ResearchRuntimeState(TypedDict, total=False):
    project: ResearchProjectSpec
    project_id: str
    resume: bool
    store: ResearchProjectStore
    plan: ResearchPlan
    results: dict[str, WorkItemResult]
    early_terminal: bool
    execution_done: bool
    execution_paused: bool
    cancelled: bool
    control: ResearchProjectControl | None


class _InputContractError(ValueError):
    pass


def _effective_input_digest(payload: dict[str, Any]) -> str:
    """Digest of the effective execution context, not just the declared inputs.

    The payload combines the work item's own ``inputs`` with the ``outputs``
    of every dependency, so the digest intentionally binds to downstream
    results; fork/repair replay relies on this binding to detect same-input
    changes.
    """
    return canonical_sha256(payload)


def validate_data_contract(
    payload: dict[str, Any], contract: DataContract | None, boundary: str = "output",
) -> list[str]:
    if contract is None:
        return []
    issues = [f"missing required {boundary}: {name}" for name in contract.required_fields if name not in payload]
    expected_types: dict[str, tuple[type, ...]] = {
        "string": (str,), "number": (int, float), "integer": (int,), "boolean": (bool,),
        "object": (dict,), "array": (list,),
    }
    for name, expected in contract.field_types.items():
        if name not in payload:
            continue
        value = payload[name]
        if expected == "integer" and isinstance(value, bool):
            issues.append(f"{boundary} {name} expected integer, got boolean")
        elif expected == "number" and isinstance(value, bool):
            issues.append(f"{boundary} {name} expected number, got boolean")
        elif not isinstance(value, expected_types[expected]):
            issues.append(f"{boundary} {name} expected {expected}, got {type(value).__name__}")
    return issues


def _completed_item_ids(results: dict[str, WorkItemResult]) -> list[str]:
    return sorted(item_id for item_id, result in results.items() if result.status in {
        WorkItemStatus.COMPLETED, WorkItemStatus.COMPLETED_WITH_GAPS,
    })


class ResearchProjectRuntime:
    """Execute a frozen, allowlisted research plan with durable item boundaries."""

    def __init__(
        self,
        projects_dir: Path | None = None,
        cache_dir: Path | None = None,
        registry: ResearchModuleRegistry | None = None,
        planner: ResearchPlanner | None = None,
        skill_catalog: SkillCatalog | None = None,
        workflow_catalog: WorkflowCatalog | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or load_settings()
        self.projects_dir = projects_dir or self.settings.projects_dir
        self.cache_dir = cache_dir or self.settings.cache_dir
        self.registry = registry or default_research_registry(self.settings)
        self.skill_catalog = skill_catalog or SkillCatalog(self.settings.skill_catalog_path)
        self.workflow_catalog = workflow_catalog or WorkflowCatalog(self.settings.workflow_catalog_path)
        self.planner = planner or ResearchPlanner(
            self.registry,
            StepClient.from_settings(self.settings),
            pattern_store=pattern_store_from_path(self.settings.pattern_store_path),
            few_shot_top_k=self.settings.pattern_few_shot_top_k,
            skill_catalog=self.skill_catalog,
            skill_hint_top_k=self.settings.skill_hint_top_k,
            paper_rag=paper_rag_store_from_path(self.settings.paper_rag_path),
            paper_top_k=self.settings.paper_rag_top_k,
            workflow_catalog=self.workflow_catalog,
        )
        self.worker_id = "research_runtime"
        self._graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(ResearchRuntimeState)
        graph.add_node("intake", self._intake)
        graph.add_node("plan", self._plan)
        graph.add_node("fork", self._fork)
        graph.add_node("execute", self._execute_one)
        graph.add_node("repair", self._repair)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "intake")
        graph.add_conditional_edges(
            "intake", lambda state: "terminal" if state["early_terminal"] else "plan",
            {"terminal": END, "plan": "plan"},
        )
        graph.add_conditional_edges(
            "plan", lambda state: "pause" if state.get("execution_paused") else "fork",
            {"pause": END, "fork": "fork"},
        )
        graph.add_conditional_edges(
            "fork", lambda state: (
                "cancelled" if state.get("cancelled") else
                "pause" if state.get("execution_paused") else
                "execute" if not state.get("execution_done", True) else "finalize"
            ),
            {"cancelled": END, "pause": END, "execute": "execute", "finalize": "finalize"},
        )
        graph.add_conditional_edges(
            "execute", lambda state: (
                "cancelled" if state.get("cancelled") else
                "pause" if state.get("execution_paused") else
                "repair" if state["execution_done"] else "execute"
            ),
            {"cancelled": END, "pause": END, "execute": "execute", "repair": "repair"},
        )
        graph.add_conditional_edges(
            "repair", lambda state: (
                "cancelled" if state.get("cancelled") else
                "pause" if state.get("execution_paused") else
                "execute" if not state.get("execution_done", True) else "fork"
            ),
            {"cancelled": END, "pause": END, "execute": "execute", "fork": "fork"},
        )
        graph.add_edge("finalize", END)
        return graph.compile()

    def _intake(self, state: ResearchRuntimeState) -> dict[str, Any]:
        project = state["project"]
        store = ResearchProjectStore(self.projects_dir, project.project_id)
        existing = store.load_spec()
        if existing is not None:
            incoming = project.model_dump(mode="json", exclude={"created_at"})
            stored = existing.model_dump(mode="json", exclude={"created_at"})
            if incoming != stored:
                raise ValueError("project id already exists with a different immutable research goal")
            project = existing
        else:
            store.create(project)
        prior_state = store.load_state()
        store.assert_integrity()
        recovered = store.recover_work_item_results()
        self._reconcile_artifact_heads(store, recovered)
        self._reconcile_review_targets(store, recovered)
        terminal = prior_state and prior_state.status in {
            ProjectStatus.COMPLETED, ProjectStatus.COMPLETED_WITH_GAPS,
            ProjectStatus.FAILED, ProjectStatus.CANCELLED,
        }
        if terminal and state.get("resume"):
            store.assert_integrity()
            return {"project": project, "store": store, "early_terminal": True,
                    "results": recovered}
        if terminal:
            raise ValueError("project already reached a terminal state; use resume to inspect it")
        if prior_state is not None and not state.get("resume"):
            raise ValueError("project already has durable progress; use resume to continue it")
        control = store.load_control()
        if control is not None and control.request == ControlRequestKind.CANCEL:
            self._apply_cancel(store, project, recovered, control)
            return {"project": project, "store": store, "early_terminal": True,
                    "results": recovered, "cancelled": True}
        if control is not None and control.request == ControlRequestKind.PAUSE:
            self._apply_pause(store, project, recovered, control)
            return {"project": project, "store": store, "early_terminal": True,
                    "results": recovered, "execution_paused": True}
        next_state = ProjectState(
            project_id=project.project_id, status=ProjectStatus.RUNNING,
            completed_items=(prior_state.completed_items if prior_state else []),
            failed_items=(prior_state.failed_items if prior_state else []),
            attempts=(prior_state.attempts if prior_state else {}),
        )
        store.save_state(next_state)
        store.append_event("state_transition", "intake", detail={"resume": bool(state.get("resume"))})
        return {"project": project, "store": store, "early_terminal": False,
                "results": recovered if state.get("resume") else {}}

    def _plan(self, state: ResearchRuntimeState) -> dict[str, Any]:
        store, project = state["store"], state["project"]
        base_plan = store.load_plan()
        new_plan = base_plan is None
        if new_plan:
            base_plan = self.planner.create_plan(project)
            if len(base_plan.items) > project.max_work_items:
                raise ValueError("planner exceeded project max_work_items")
            unknown = sorted({item.module for item in base_plan.items} - set(self.registry.names))
            if unknown:
                raise ValueError(f"plan contains unregistered modules: {unknown}")
            store.save_plan(base_plan)
        assert base_plan is not None
        self._validate_workflow_template(project, base_plan)
        revisions = store.read_plan_revisions()
        self._validate_repair_overlays(project, store, revisions)
        plan = effective_plan(base_plan, revisions)
        prior_state = store.load_state() or ProjectState(project_id=project.project_id)
        if project.autonomy_mode == AutonomyMode.AUTONOMOUS and not self._accepted(store, base_plan.plan_id):
            store.append_decision(DecisionEvent(
                project_id=project.project_id, action=DecisionAction.ACCEPT, target_ids=[base_plan.plan_id],
                rationale="Plan passed DAG, module allowlist and work-item budget validation.",
                actor="research_runtime", reversible=True,
            ))
        plan_accepted = project.autonomy_mode == AutonomyMode.AUTONOMOUS or self._accepted(store, base_plan.plan_id)
        if not plan_accepted:
            store.save_state(ProjectState(
                project_id=project.project_id, status=ProjectStatus.NEEDS_INPUT,
                completed_items=_completed_item_ids(state["results"]), attempts=prior_state.attempts,
                checkpoint_kind="plan", checkpoint_target_id=base_plan.plan_id,
                terminal_reason=f"Human acceptance is required for plan {base_plan.plan_id}.",
            ))
            store.append_event("human_checkpoint", "plan_approval_required", detail={"plan_id": base_plan.plan_id})
            return {"plan": plan, "execution_paused": True}
        store.save_state(ProjectState(
            project_id=project.project_id, status=ProjectStatus.PLANNED,
            completed_items=_completed_item_ids(state["results"]), attempts=prior_state.attempts,
        ))
        store.append_event("plan_frozen" if new_plan else "plan_resumed", "planned", detail={
            "plan_id": base_plan.plan_id, "planner_backend": plan.planner_backend,
            "work_items": [item.item_id for item in plan.items],
        })
        return {"plan": plan, "execution_paused": False}

    def _validate_workflow_template(
        self, project: ResearchProjectSpec, plan: ResearchPlan,
    ) -> None:
        """Fail closed when a project plan violates its frozen workflow template."""
        if not project.workflow_template:
            return
        template = self.workflow_catalog.get(project.workflow_template)
        if project.workflow_template_sha256 and project.workflow_template_sha256 != template.source_sha256:
            raise WorkflowCatalogError(
                f"workflow template {template.template_id} changed after project freeze; "
                "rejecting the project instead of silently executing a different workflow"
            )
        self.workflow_catalog.validate_plan_modules(
            project.workflow_template, [item.module for item in plan.items]
        )

    @staticmethod
    def _accepted(store: ResearchProjectStore, target_id: str) -> bool:
        return any(row.action == DecisionAction.ACCEPT and target_id in row.target_ids
                   for row in store.read_decisions())

    @staticmethod
    def _accepted_snapshot(store: ResearchProjectStore, target_id: str, snapshot_digest: str) -> bool:
        return any(
            row.action == DecisionAction.ACCEPT
            and target_id in row.target_ids
            and row.evidence_snapshot_digest == snapshot_digest
            for row in store.read_decisions()
        )

    @staticmethod
    def _has_action(store: ResearchProjectStore, action: DecisionAction) -> bool:
        return any(row.action == action for row in store.read_decisions())

    def _validate_repair_overlays(
        self,
        project: ResearchProjectSpec,
        store: ResearchProjectStore,
        revisions: list,
    ) -> None:
        requests_by_id = {row.repair_request_id: row for row in store.read_repair_requests()}
        for revision in revisions:
            if revision.repair_request_id is None:
                continue
            request = requests_by_id[revision.repair_request_id]
            root = next(
                item for item in revision.added_items
                if item.rerun_of_item_id == request.target_work_item_id
            )
            descriptor = self.registry.get(root.module).descriptor
            if request.action == RepairAction.SWITCH_DATASET_SAME_CONTEXT:
                allowed_modes = {"same_input_retry", "alternate_dataset"}
            elif request.action in OVERLAY_ACTIONS:
                allowed_modes = {request.action.value}
            else:
                allowed_modes = {"same_input_retry"}
            if not (
                descriptor.side_effect_free
                and descriptor.replay_safe
                and bool(set(descriptor.repair_modes) & allowed_modes)
            ):
                raise ValueError("persisted repair overlay violates current module repair policy")
            if any(
                not self.registry.get(item.module).descriptor.replay_safe
                or not self.registry.get(item.module).descriptor.side_effect_free
                for item in revision.added_items
            ):
                raise ValueError("persisted repair overlay contains an unsafe downstream module")
            expected_checkpoint = project.autonomy_mode != AutonomyMode.AUTONOMOUS
            if revision.approval_required != expected_checkpoint:
                raise ValueError("persisted repair overlay authorization does not match project autonomy")
            if expected_checkpoint and not self._accepted_snapshot(
                store, request.repair_request_id, request.trigger_snapshot_digest,
            ):
                raise ValueError("persisted repair overlay lacks exact-snapshot approval")

    def _execute_one(self, state: ResearchRuntimeState) -> dict[str, Any]:
        store, project, plan = state["store"], state["project"], state["plan"]
        results = dict(state["results"])
        revisions = store.read_plan_revisions()
        active_ids = active_item_ids(plan, revisions)
        pending = [item for item in plan.items if item.item_id in active_ids and item.item_id not in results]
        control = store.load_control()
        if control is not None and control.request == ControlRequestKind.CANCEL:
            self._apply_cancel(store, project, results, control)
            return {"results": results, "execution_done": True, "execution_paused": False,
                    "cancelled": True}
        if control is not None and control.request == ControlRequestKind.PAUSE:
            self._apply_pause(store, project, results, control)
            return {"results": results, "execution_done": False, "execution_paused": True}
        if not pending:
            return {"results": results, "execution_done": True, "execution_paused": False}
        item = next((candidate for candidate in pending
                     if all(dependency in results and results[dependency].status in TERMINAL_WORK_ITEM_STATUSES
                            for dependency in candidate.dependencies)), None)
        if item is None:
            raise RuntimeError("no executable work item remains despite an acyclic validated plan")
        prior_state = store.load_state() or ProjectState(project_id=project.project_id)
        attempts = dict(prior_state.attempts)
        if project.autonomy_mode == AutonomyMode.SUPERVISED and not self._accepted(store, item.item_id):
            store.save_state(ProjectState(
                project_id=project.project_id, status=ProjectStatus.NEEDS_INPUT,
                current_item_id=item.item_id, completed_items=_completed_item_ids(results),
                checkpoint_kind="work_item", checkpoint_target_id=item.item_id,
                attempts=attempts, terminal_reason=f"Human acceptance is required for work item {item.item_id}.",
            ))
            store.append_event("human_checkpoint", "work_item_approval_required", work_item_id=item.item_id)
            return {"results": results, "execution_done": False, "execution_paused": True}
        dependency_failures = [dependency for dependency in item.dependencies if results[dependency].status in {
            WorkItemStatus.FAILED, WorkItemStatus.BLOCKED, WorkItemStatus.NEEDS_INPUT,
        }]
        if dependency_failures and item.module not in {"independent_review", "research_report"}:
            result = WorkItemResult(
                item_id=item.item_id, module=item.module, status=WorkItemStatus.BLOCKED,
                summary="The work item was not executed because a required dependency did not complete.",
                limitations=[f"Blocking dependencies: {', '.join(dependency_failures)}"],
            )
            store.save_work_item_result(result)
            results[item.item_id] = result
            store.append_event("work_item_finished", result.status.value, work_item_id=item.item_id,
                               detail={"blocking_dependencies": dependency_failures})
            return {"results": results, "execution_done": active_ids.issubset(results),
                    "execution_paused": False}
        if attempts.get(item.item_id, 0) >= item.max_attempts:
            result = WorkItemResult(
                item_id=item.item_id, module=item.module, status=WorkItemStatus.BLOCKED,
                summary="The work item attempt budget was exhausted after an interrupted or failed attempt.",
                limitations=["A recorded replan or human override is required before another attempt."],
            )
            store.save_work_item_result(result)
            results[item.item_id] = result
            store.append_event("work_item_finished", result.status.value, work_item_id=item.item_id,
                               detail={"attempts": attempts.get(item.item_id, 0), "max_attempts": item.max_attempts})
            return {"results": results, "execution_done": active_ids.issubset(results),
                    "execution_paused": False}
        lease = self._acquire_lease(store, item, attempts.get(item.item_id, 0))
        attempt_number = len(store.read_attempts(item.item_id)) + 1
        attempt_id = lease.attempt_id
        attempts[item.item_id] = attempt_number
        store.save_state(ProjectState(
            project_id=project.project_id, status=ProjectStatus.RUNNING, current_item_id=item.item_id,
            completed_items=_completed_item_ids(results), attempts=attempts,
        ))
        store.append_event("work_item_started", "running", work_item_id=item.item_id,
                           detail={"module": item.module, "attempt": attempt_number})
        store.heartbeat_lease(lease.lease_id)
        active_results = {item_id: row for item_id, row in results.items() if item_id in active_ids}
        if item.module == "domain_overlay" and item.rerun_of_item_id is not None:
            source_result = results.get(item.rerun_of_item_id)
            if source_result is not None and source_result.item_id not in active_results:
                active_results[source_result.item_id] = source_result
        referenced_artifact_ids = {
            artifact_id for result in active_results.values() for artifact_id in result.artifact_ids
        }
        active_artifacts = [
            row for row in store.read_artifacts()
            if row.work_item_id in active_ids and row.artifact_id in referenced_artifact_ids
        ]
        context = ModuleContext(
            project=project, item=item, project_dir=store.project_dir, cache_dir=self.cache_dir,
            settings=self.settings, prior_results=active_results, artifacts=active_artifacts,
            activity_sink=lambda projection: self._record_domain_activity(store, projection),
        )
        input_payload = {
            **item.inputs,
            "dependencies": {dependency: results[dependency].outputs for dependency in item.dependencies},
        }
        effective_input_digest = _effective_input_digest(input_payload)
        try:
            supersedes_result_digest: str | None = None
            if item.rerun_of_item_id is not None:
                source_result = results.get(item.rerun_of_item_id)
                if source_result is None:
                    raise ValueError("repair item references a missing source result")
                supersedes_result_digest = work_item_result_digest(source_result)
                if item.fork_branch_id is not None:
                    branch = next(
                        (row for row in store.read_branches()
                         if row.branch_id == item.fork_branch_id),
                        None,
                    )
                    if branch is None:
                        raise ValueError("fork item references a missing plan branch")
                else:
                    request = next(
                        (row for row in store.read_repair_requests()
                         if row.repair_request_id == item.repair_request_id),
                        None,
                    )
                    if request is None:
                        raise ValueError("repair item references a missing repair request")
                    if (
                        item.rerun_of_item_id == request.target_work_item_id
                        and request.action == RepairAction.RERUN_SUBGRAPH_SAME_INPUTS
                        and effective_input_digest != request.input_digest
                    ):
                        raise ValueError("same-input repair changed the effective input digest")
            input_issues = validate_data_contract(input_payload, item.input_contract, boundary="input")
            if input_issues:
                raise _InputContractError("; ".join(input_issues))
            execution = self.registry.get(item.module).execute(context)
            result = execution.result
            if result.item_id != item.item_id or result.module != item.module:
                raise ValueError("module result identity does not match the planned work item")
            registered_ids = list(result.artifact_ids)
            for artifact in execution.artifacts:
                record = store.register_artifact(
                    artifact.path, item.item_id, artifact.logical_name, artifact.media_type,
                )
                registered_ids.append(record.artifact_id)
            issues = validate_data_contract(result.outputs, item.output_contract)
            contract_checkable = result.status in {
                WorkItemStatus.COMPLETED, WorkItemStatus.COMPLETED_WITH_GAPS,
            }
            if issues and contract_checkable:
                result = result.model_copy(update={
                    "status": WorkItemStatus.COMPLETED_WITH_GAPS,
                    "limitations": [*result.limitations, *issues],
                })
                execution.assessments.append(AssessmentRecord(
                    project_id=project.project_id, target_id=item.item_id,
                    dimension=AssessmentDimension.SCHEMA_ALIGNMENT, level=AssessmentLevel.A0,
                    result=AssessmentResult.FAIL, actor="research_runtime",
                    method="deterministic_data_contract_validation", rationale="; ".join(issues), blocking=True,
                ))
            elif issues:
                execution.assessments.append(AssessmentRecord(
                    project_id=project.project_id, target_id=item.item_id,
                    dimension=AssessmentDimension.SCHEMA_ALIGNMENT, level=AssessmentLevel.A0,
                    result=AssessmentResult.NOT_ASSESSED, actor="research_runtime",
                    method="deterministic_data_contract_validation",
                    rationale=("Output contract could not be assessed because the module ended as "
                               f"{result.status.value}: {'; '.join(issues)}"), blocking=False,
                ))
            elif item.output_contract is not None and contract_checkable:
                execution.assessments.append(AssessmentRecord(
                    project_id=project.project_id, target_id=item.item_id,
                    dimension=AssessmentDimension.SCHEMA_ALIGNMENT, level=AssessmentLevel.A0,
                    result=AssessmentResult.PASS, actor="research_runtime",
                    method="deterministic_data_contract_validation",
                    rationale=f"Output satisfies {item.output_contract.schema_id}@{item.output_contract.schema_version}.",
                ))
            result = result.model_copy(update={
                "artifact_ids": list(dict.fromkeys(registered_ids)),
                "input_digest": effective_input_digest,
                "supersedes_result_digest": supersedes_result_digest,
                "repair_request_id": item.repair_request_id,
                "fork_branch_id": item.fork_branch_id,
                "completed_at": utc_now(),
            })
            for assessment in execution.assessments:
                store.append_assessment(assessment)
        except ProjectBusyError:
            store.release_lease(lease.lease_id)
            raise
        except _InputContractError as exc:
            result = WorkItemResult(
                item_id=item.item_id, module=item.module, status=WorkItemStatus.NEEDS_INPUT,
                summary="The work item input contract is incomplete or misaligned.",
                error=exc.__class__.__name__, failure_class=FailureClass.MISSING_INPUT,
                input_digest=locals().get("effective_input_digest"),
                repair_request_id=item.repair_request_id,
                fork_branch_id=item.fork_branch_id, limitations=[str(exc)],
            )
            store.append_assessment(AssessmentRecord(
                project_id=project.project_id, target_id=item.item_id,
                dimension=AssessmentDimension.SCHEMA_ALIGNMENT, level=AssessmentLevel.A0,
                result=AssessmentResult.FAIL, actor="research_runtime",
                method="deterministic_input_contract_validation", rationale=str(exc), blocking=True,
            ))
        except Exception as exc:
            result = WorkItemResult(
                item_id=item.item_id, module=item.module, status=WorkItemStatus.FAILED,
                summary="The research module failed without producing an accepted result.",
                error=exc.__class__.__name__, failure_class=classify_exception(exc),
                input_digest=locals().get("effective_input_digest"),
                supersedes_result_digest=locals().get("supersedes_result_digest"),
                repair_request_id=item.repair_request_id,
                fork_branch_id=item.fork_branch_id,
                limitations=["Inspect the project event ledger before retrying."],
            )
        try:
            store.heartbeat_lease(lease.lease_id)
            self._persist_attempt(
                store, item, attempt_id, attempt_number, lease, result, effective_input_digest,
            )
            store.save_work_item_result(result)
            results[item.item_id] = result
            store.append_event("work_item_finished", result.status.value, work_item_id=item.item_id, detail={
                "module": item.module, "artifact_ids": result.artifact_ids,
                "limitations": result.limitations, "error": result.error,
            })
            if item.module == "independent_review":
                head = store.read_work_item_head(item.item_id)
                if head is not None:
                    self._record_review_target(
                        store, plan, revisions, item, head, results, active_ids,
                    )
        finally:
            store.release_lease(lease.lease_id)
        return {"results": results, "execution_done": active_ids.issubset(results),
                "execution_paused": False}

    def _apply_cancel(
        self,
        store: ResearchProjectStore,
        project: ResearchProjectSpec,
        results: dict[str, WorkItemResult],
        control: ResearchProjectControl,
    ) -> None:
        """Persist a durable CANCELLED terminal state at a safe boundary.

        Every active work item that never started is recorded as SKIPPED so
        the ledger stays complete; the graph then exits before finalize, which
        means no ranking, report, release or downstream artifact is produced.
        """
        plan = store.load_plan()
        revisions = store.read_plan_revisions()
        active_ids = active_item_ids(plan, revisions) if plan is not None else set()
        existing = set(results)
        if plan is not None:
            for item in plan.items:
                if item.item_id in active_ids and item.item_id not in existing:
                    store.save_work_item_result(WorkItemResult(
                        item_id=item.item_id, module=item.module, status=WorkItemStatus.SKIPPED,
                        summary="The project was cancelled before this work item started.",
                        limitations=[f"Project cancelled by {control.actor}: {control.rationale}"],
                    ))
        prior = store.load_state() or ProjectState(project_id=project.project_id)
        store.save_state(ProjectState(
            project_id=project.project_id, status=ProjectStatus.CANCELLED,
            current_item_id=prior.current_item_id,
            completed_items=_completed_item_ids(results), failed_items=prior.failed_items,
            attempts=prior.attempts,
            terminal_reason=f"Cancelled by {control.actor}: {control.rationale}",
            updated_at=utc_now(),
        ))
        store.append_event("execution_cancelled", "cancelled", detail={
            "actor": control.actor, "rationale": control.rationale,
        })
        store.append_event("project_terminal", "cancelled", detail={
            "reason": f"Cancelled by {control.actor}: {control.rationale}",
        })
        store.clear_control()

    def _apply_pause(
        self,
        store: ResearchProjectStore,
        project: ResearchProjectSpec,
        results: dict[str, WorkItemResult],
        control: ResearchProjectControl,
    ) -> None:
        """Persist PAUSED at a safe boundary without consuming work or leases.

        A later resume simply runs the graph again; completed items are kept
        and the same control intent is cleared because it has been honored.
        """
        prior = store.load_state() or ProjectState(project_id=project.project_id)
        store.save_state(ProjectState(
            project_id=project.project_id, status=ProjectStatus.PAUSED,
            current_item_id=prior.current_item_id,
            completed_items=_completed_item_ids(results), failed_items=prior.failed_items,
            attempts=prior.attempts,
            terminal_reason=f"Paused by {control.actor}: {control.rationale}",
            updated_at=utc_now(),
        ))
        store.append_event("execution_paused", "paused", detail={
            "actor": control.actor, "rationale": control.rationale,
        })
        store.clear_control()

    @staticmethod
    def _record_domain_activity(
        store: ResearchProjectStore,
        projection: DomainActivityProjection,
    ) -> None:
        """Persist one idempotent project projection of a child TraceEvent."""
        store.append_domain_activity(projection)

    @staticmethod
    def _new_contract_id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:24]}"

    @staticmethod
    def _reconcile_artifact_heads(
        store: ResearchProjectStore,
        results: dict[str, WorkItemResult],
    ) -> None:
        """Backfill active artifact heads for results referenced by committed work."""
        referenced = {
            artifact_id for result in results.values() for artifact_id in result.artifact_ids
        }
        store.reconcile_artifact_heads(referenced)

    def _reconcile_review_targets(
        self,
        store: ResearchProjectStore,
        results: dict[str, WorkItemResult],
    ) -> None:
        """Restore missing ReviewTarget records for already committed reviews.

        A crash between the review attempt/head commit and the ReviewTarget
        write leaves no business ambiguity: the durable head is the anchor and
        the target is appended idempotently from it.
        """
        base_plan = store.load_plan()
        if base_plan is None:
            return
        revisions = store.read_plan_revisions()
        plan = effective_plan(base_plan, revisions)
        active = active_item_ids(plan, revisions)
        pending: list[tuple[Any, WorkItemHead]] = []
        for item in plan.items:
            if item.module != "independent_review" or item.item_id not in active:
                continue
            head = store.read_work_item_head(item.item_id)
            result = results.get(item.item_id)
            if head is None or result is None or head.result_digest != work_item_result_digest(result):
                continue
            pending.append((item, head))
        if not pending:
            return
        assessments = store.read_assessments()
        artifacts = store.read_artifacts()
        for item, head in pending:
            snapshot_digest = review_target_snapshot_digest(
                plan=plan, review_item_id=item.item_id, results=results,
                assessments=assessments, artifacts=artifacts, revisions=revisions,
            )
            if self._has_review_target(store, item.item_id, snapshot_digest):
                continue
            self._append_review_target(store, item, head, active, snapshot_digest)

    def _record_review_target(
        self,
        store: ResearchProjectStore,
        plan: ResearchPlan,
        revisions: list,
        item: WorkItemSpec,
        head: WorkItemHead,
        results: dict[str, WorkItemResult],
        active_ids: set[str],
    ) -> None:
        """Persist the immutable set of result/artifact digests one review assessed."""
        snapshot_digest = review_target_snapshot_digest(
            plan=plan, review_item_id=item.item_id, results=results,
            assessments=store.read_assessments(), artifacts=store.read_artifacts(),
            revisions=revisions,
        )
        if self._has_review_target(store, item.item_id, snapshot_digest):
            return
        self._append_review_target(store, item, head, active_ids, snapshot_digest)

    @staticmethod
    def _append_review_target(
        store: ResearchProjectStore,
        item: WorkItemSpec,
        head: WorkItemHead,
        active_ids: set[str],
        snapshot_digest: str,
    ) -> None:
        store.append_review_target(ReviewTarget(
            review_target_id=ResearchProjectRuntime._new_contract_id("review-target"),
            project_id=store.project_id,
            scope="work_item",
            work_item_id=item.item_id,
            result_digests={item.item_id: head.result_digest},
            artifact_ids=sorted(
                row.artifact_id for row in store.read_artifact_heads()
                if row.work_item_id in active_ids
            ),
            snapshot_digest=snapshot_digest,
            reason=f"Independent review committed by attempt {head.attempt_id}.",
        ))
        store.append_event("review_target_recorded", "recorded", work_item_id=item.item_id,
                           detail={"review_target_digest": snapshot_digest, "attempt_id": head.attempt_id})

    @staticmethod
    def _has_review_target(
        store: ResearchProjectStore,
        work_item_id: str,
        snapshot_digest: str,
    ) -> bool:
        return any(
            row.work_item_id == work_item_id and row.snapshot_digest == snapshot_digest
            for row in store.read_review_targets()
        )

    def _reconcile_attempt_ledger(
        self, store: ResearchProjectStore, item: WorkItemSpec, state_attempts: int,
    ) -> None:
        """Backfill CANCELLED ledger rows for state attempts that never persisted.

        A crash between the state counter increment and the terminal attempt
        write leaves ``ProjectState.attempts`` ahead of the append-only attempt
        ledger. Each missing row becomes an auditable CANCELLED attempt so the
        ledger count and the state counter stay consistent and the next retry
        receives the correct contiguous attempt number.
        """
        ledger_count = len(store.read_attempts(item.item_id))
        while ledger_count < state_attempts:
            number = ledger_count + 1
            attempt_id = self._new_contract_id("attempt")
            store.append_attempt(WorkAttempt(
                attempt_id=attempt_id,
                project_id=store.project_id,
                work_item_id=item.item_id,
                attempt_number=number,
                status=WorkAttemptStatus.CANCELLED,
                input_digest=canonical_sha256(item.inputs),
                error="interrupted_before_durable_result",
                started_at=utc_now(),
                completed_at=utc_now(),
            ))
            store.append_event("work_attempt_recorded", "cancelled", work_item_id=item.item_id,
                               detail={"attempt_id": attempt_id, "attempt_number": number, "reconciled": True})
            ledger_count = len(store.read_attempts(item.item_id))

    def _acquire_lease(
        self, store: ResearchProjectStore, item: WorkItemSpec, state_attempts: int,
    ) -> WorkerLease:
        """Claim the next attempt lease, reclaiming orphan or expired leases."""
        self._reconcile_attempt_ledger(store, item, state_attempts)
        existing = store.read_leases(item.item_id)
        now = datetime.now(timezone.utc)
        for row in existing:
            if row.released_at is not None:
                continue
            expired = False
            try:
                expired = now > datetime.fromisoformat(row.expires_at)
            except ValueError:
                expired = True
            attempt = next(
                (record for record in store.read_attempts(item.item_id)
                 if record.attempt_id == row.attempt_id),
                None,
            )
            if (attempt is not None
                    and attempt.status in {WorkAttemptStatus.PENDING, WorkAttemptStatus.RUNNING}
                    and not expired):
                raise ProjectBusyError(f"work item {item.item_id} is leased by {row.worker_id}")
            store.release_lease(row.lease_id)
            store.append_event("lease_reclaimed", "released", work_item_id=item.item_id,
                               detail={"lease_id": row.lease_id, "worker_id": row.worker_id, "expired": expired})
        attempt_id = self._new_contract_id("attempt")
        lease = WorkerLease(
            lease_id=self._new_contract_id("lease"),
            project_id=store.project_id,
            work_item_id=item.item_id,
            attempt_id=attempt_id,
            worker_id=self.worker_id,
            expires_at=(datetime.now(timezone.utc) + LEASE_DURATION).isoformat(),
        )
        store.append_lease(lease)
        store.append_event("lease_acquired", "running", work_item_id=item.item_id,
                           detail={"lease_id": lease.lease_id, "attempt_id": attempt_id})
        return lease

    @staticmethod
    def _attempt_status(result: WorkItemResult) -> WorkAttemptStatus:
        return {
            WorkItemStatus.COMPLETED: WorkAttemptStatus.COMPLETED,
            WorkItemStatus.COMPLETED_WITH_GAPS: WorkAttemptStatus.COMPLETED_WITH_GAPS,
            WorkItemStatus.FAILED: WorkAttemptStatus.FAILED,
            WorkItemStatus.BLOCKED: WorkAttemptStatus.CANCELLED,
            WorkItemStatus.NEEDS_INPUT: WorkAttemptStatus.CANCELLED,
            WorkItemStatus.SKIPPED: WorkAttemptStatus.CANCELLED,
        }.get(result.status, WorkAttemptStatus.FAILED)

    def _persist_attempt(
        self,
        store: ResearchProjectStore,
        item: WorkItemSpec,
        attempt_id: str,
        attempt_number: int,
        lease: WorkerLease,
        result: WorkItemResult,
        input_digest: str,
    ) -> None:
        """Append the immutable terminal attempt record for a completed execution."""
        prior = store.current_attempt(item.item_id)
        attempt = WorkAttempt(
            attempt_id=attempt_id,
            project_id=store.project_id,
            work_item_id=item.item_id,
            attempt_number=attempt_number,
            status=self._attempt_status(result),
            input_digest=input_digest,
            output_digest=work_item_result_digest(result),
            worker_lease_id=lease.lease_id,
            supersedes_attempt_id=prior.attempt_id if prior is not None else None,
            failure_class=result.failure_class,
            error=result.error,
            started_at=lease.acquired_at,
            completed_at=utc_now(),
        )
        store.save_attempt_result(attempt, result)
        store.append_attempt(attempt)
        current_head = store.read_work_item_head(item.item_id)
        store.update_work_item_head(WorkItemHead(
            project_id=store.project_id,
            work_item_id=item.item_id,
            attempt_id=attempt.attempt_id,
            result_digest=attempt.output_digest,
            status=result.status,
            version=(current_head.version + 1) if current_head is not None else 1,
            supersedes_head_id=current_head.head_id if current_head is not None else None,
        ), expected_version=current_head.version if current_head is not None else None)
        store.append_event("work_attempt_recorded", result.status.value, work_item_id=item.item_id,
                           detail={"attempt_id": attempt_id, "attempt_number": attempt_number})

    def _record_restore_attempt(
        self,
        store: ResearchProjectStore,
        project: ResearchProjectSpec,
        directive: ForkDirective,
        source_attempt: WorkAttempt,
        restored: WorkItemResult,
    ) -> WorkAttempt:
        """Persist a restore-type attempt so the rebound result is head-backed.

        RESTORE re-binds an immutable historical payload to the active fork
        point without executing it. Without a fresh attempt row and head, the
        working result.json mirror would diverge from the committed head and a
        later recovery pass would silently discard the restored payload.
        """
        if restored.status == WorkItemStatus.COMPLETED:
            attempt_status = WorkAttemptStatus.COMPLETED
        elif restored.status == WorkItemStatus.COMPLETED_WITH_GAPS:
            attempt_status = WorkAttemptStatus.COMPLETED_WITH_GAPS
        elif restored.status == WorkItemStatus.FAILED:
            attempt_status = WorkAttemptStatus.FAILED
        else:
            raise ValueError(
                f"cannot restore a non-terminal work item result: {restored.status.value}"
            )
        restored_digest = work_item_result_digest(restored)
        existing = next(
            (
                row for row in store.read_attempts(directive.target_work_item_id)
                if row.supersedes_attempt_id == source_attempt.attempt_id
                and row.output_digest == restored_digest
            ),
            None,
        )
        if existing is not None and store.load_attempt_result(existing.attempt_id) is not None:
            attempt = existing
        else:
            attempt = WorkAttempt(
                attempt_id=self._new_contract_id("attempt"),
                project_id=project.project_id,
                work_item_id=directive.target_work_item_id,
                attempt_number=len(store.read_attempts(directive.target_work_item_id)) + 1,
                status=attempt_status,
                input_digest=source_attempt.input_digest,
                output_digest=restored_digest,
                worker_lease_id=None,
                supersedes_attempt_id=source_attempt.attempt_id,
                started_at=source_attempt.started_at,
                completed_at=utc_now(),
            )
            store.save_attempt_result(attempt, restored)
            store.append_attempt(attempt)
        current_head = store.read_work_item_head(directive.target_work_item_id)
        if current_head is None or current_head.attempt_id != attempt.attempt_id:
            store.update_work_item_head(WorkItemHead(
                project_id=project.project_id,
                work_item_id=directive.target_work_item_id,
                attempt_id=attempt.attempt_id,
                result_digest=restored_digest,
                status=restored.status,
                version=(current_head.version + 1) if current_head is not None else 1,
                supersedes_head_id=current_head.head_id if current_head is not None else None,
            ), expected_version=current_head.version if current_head is not None else None)
        return attempt

    def _fork(self, state: ResearchRuntimeState) -> dict[str, Any]:
        """Apply user-issued rollback branches (redo or restore) with dependency invalidation.

        A proposed branch either waits for exact-snapshot approval (checkpointed
        mode or any restore) or is auto-approved in autonomous redo mode. Once
        approved, it materializes as a ``fork_rollback`` plan revision that
        supersedes the descendant closure and re-runs the replacement items.
        """
        store, project = state["store"], state["project"]
        control = store.load_control()
        if control is not None and control.request == ControlRequestKind.CANCEL:
            self._apply_cancel(store, project, state["results"], control)
            return {"plan": state["plan"], "results": state["results"],
                    "execution_done": True, "execution_paused": False, "cancelled": True}
        if control is not None and control.request == ControlRequestKind.PAUSE:
            self._apply_pause(store, project, state["results"], control)
            return {"plan": state["plan"], "results": state["results"],
                    "execution_done": False, "execution_paused": True}
        base_plan = store.load_plan()
        if base_plan is None:
            return {"plan": state["plan"], "results": state["results"],
                    "execution_done": True, "execution_paused": False}
        revisions = store.read_plan_revisions()
        plan = effective_plan(base_plan, revisions)
        results = dict(state["results"])
        branches = store.read_branches()
        directives = {row.fork_directive_id: row for row in store.read_fork_directives()}
        decisions = store.read_decisions()
        attempts = store.read_attempts()

        # 1) resolve applied branches once all replacement items finished.
        later_superseded = {
            item_id for revision in revisions for item_id in revision.superseded_item_ids
        }
        for branch in branches:
            if branch.status in {PlanBranchStatus.APPLIED, PlanBranchStatus.RESOLVED} and branch.revision_id is not None:
                revision = next((row for row in revisions if row.revision_id == branch.revision_id), None)
                if branch.status == PlanBranchStatus.APPLIED and revision is not None and all(
                    results.get(row.item_id) is not None
                    and results[row.item_id].status in TERMINAL_WORK_ITEM_STATUSES
                    for row in revision.added_items
                ):
                    current_snapshot = project_snapshot_digest(
                        plan=plan, results=results, assessments=store.read_assessments(),
                        artifacts=store.read_artifacts(), revisions=revisions,
                    )
                    resolved = branch.model_copy(update={
                        "status": PlanBranchStatus.RESOLVED,
                        "resolved_snapshot_digest": current_snapshot,
                        "resolved_at": utc_now(),
                    })
                    store.append_branch_snapshot(resolved)
                    store.append_event(
                        "fork_resolved", "resolved", work_item_id=branch.fork_point_item_id,
                        detail={"branch_id": branch.branch_id, "resolved_snapshot_digest": current_snapshot},
                    )
                if branch.status == PlanBranchStatus.RESOLVED and set(branch.added_item_ids) & later_superseded:
                    superseded = branch.model_copy(update={"status": PlanBranchStatus.SUPERSEDED})
                    store.append_branch_snapshot(superseded)
                    store.append_event(
                        "fork_superseded", "superseded", work_item_id=branch.fork_point_item_id,
                        detail={"branch_id": branch.branch_id},
                    )

        # 2) drive the oldest actionable branch.
        for branch in branches:
            if branch.revision_id is not None or branch.status == PlanBranchStatus.REJECTED:
                continue
            directive = directives.get(branch.fork_directive_id)
            if directive is None:
                raise RuntimeError("fork branch references a missing immutable directive")
            if branch.status == PlanBranchStatus.PROPOSED:
                approval_required = (
                    directive.mode == ForkMode.RESTORE or project.autonomy_mode != AutonomyMode.AUTONOMOUS
                )
                if approval_required:
                    prior_state = store.load_state() or ProjectState(project_id=project.project_id)
                    store.save_state(ProjectState(
                        project_id=project.project_id, status=ProjectStatus.WAITING_REVIEW,
                        current_item_id=directive.target_work_item_id,
                        completed_items=_completed_item_ids(results),
                        attempts=prior_state.attempts,
                        checkpoint_kind="fork", checkpoint_target_id=branch.branch_id,
                        checkpoint_snapshot_digest=directive.snapshot_digest,
                        terminal_reason=f"Fork approval is required for {branch.branch_id}.",
                    ))
                    if not any(
                        row.event_type == "human_checkpoint"
                        and row.detail.get("branch_id") == branch.branch_id
                        for row in store.read_events()
                    ):
                        store.append_event(
                            "human_checkpoint", "fork_approval_required",
                            work_item_id=directive.target_work_item_id,
                            detail={
                                "branch_id": branch.branch_id,
                                "fork_directive_id": directive.fork_directive_id,
                                "snapshot_digest": directive.snapshot_digest,
                                "mode": directive.mode.value,
                                "target_work_item_id": directive.target_work_item_id,
                            },
                        )
                    return {"plan": plan, "results": results,
                            "execution_done": True, "execution_paused": True}
                if not any(
                    row.action == DecisionAction.ACCEPT
                    and branch.branch_id in row.target_ids
                    and row.evidence_snapshot_digest == directive.snapshot_digest
                    for row in decisions
                ):
                    store.append_decision(DecisionEvent(
                        project_id=project.project_id, action=DecisionAction.ACCEPT,
                        target_ids=[branch.branch_id],
                        rationale="Autonomous redo fork approval.",
                        actor="research_runtime",
                        evidence_snapshot_digest=directive.snapshot_digest,
                        reversible=False,
                    ))
                approved = branch.model_copy(update={"status": PlanBranchStatus.APPROVED})
                store.append_branch_snapshot(approved)
                store.append_event(
                    "fork_approved", "approved", work_item_id=directive.target_work_item_id,
                    detail={"branch_id": branch.branch_id, "mode": directive.mode.value},
                )
                branch = approved
            if branch.status == PlanBranchStatus.APPROVED:
                current_snapshot = project_snapshot_digest(
                    plan=plan, results=results, assessments=store.read_assessments(),
                    artifacts=store.read_artifacts(), revisions=revisions,
                )
                if directive.snapshot_digest != current_snapshot:
                    prior_state = store.load_state() or ProjectState(project_id=project.project_id)
                    store.save_state(ProjectState(
                        project_id=project.project_id, status=ProjectStatus.NEEDS_INPUT,
                        current_item_id=directive.target_work_item_id,
                        completed_items=_completed_item_ids(results),
                        attempts=prior_state.attempts,
                        terminal_reason=(
                            f"Fork {branch.branch_id} is stale: the project snapshot changed "
                            "after the directive was issued."
                        ),
                    ))
                    store.append_event(
                        "fork_stale", "needs_input", work_item_id=directive.target_work_item_id,
                        detail={"branch_id": branch.branch_id},
                    )
                    return {"plan": plan, "results": results,
                            "execution_done": True, "execution_paused": True}
                revision = build_fork_revision(
                    project=project,
                    base_plan=base_plan,
                    plan=plan,
                    revisions=revisions,
                    branch=branch,
                    directive=directive,
                    assessments=store.read_assessments(),
                    artifacts=store.read_artifacts(),
                )
                store.append_plan_revision(revision)
                revised_revisions = [*revisions, revision]
                revised_plan = effective_plan(base_plan, revised_revisions)
                after_snapshot = project_snapshot_digest(
                    plan=revised_plan, results=results, assessments=store.read_assessments(),
                    artifacts=store.read_artifacts(), revisions=revised_revisions,
                )
                applied = branch.model_copy(update={
                    "status": PlanBranchStatus.APPLIED,
                    "revision_id": revision.revision_id,
                    "after_snapshot_digest": after_snapshot,
                    "applied_at": utc_now(),
                })
                store.append_branch_snapshot(applied)
                store.append_event(
                    "plan_revised", "fork_applied", work_item_id=directive.target_work_item_id,
                    detail={
                        "branch_id": branch.branch_id,
                        "revision_id": revision.revision_id,
                        "revision_digest": revision.revision_digest,
                        "superseded_work_item_ids": revision.superseded_item_ids,
                        "added_work_item_ids": [row.item_id for row in revision.added_items],
                    },
                )
                if directive.mode == ForkMode.RESTORE:
                    attempt = next(
                        (row for row in attempts if row.attempt_id == directive.rollback_to_attempt_id),
                        None,
                    )
                    if attempt is None:
                        raise RuntimeError("restore fork references a missing attempt")
                    restored = store.load_attempt_result(attempt.attempt_id)
                    if restored is None:
                        raise RuntimeError("restore fork attempt result snapshot is missing")
                    if restored.item_id != directive.target_work_item_id:
                        # The attempt belongs to an earlier version of this
                        # logical step; re-bind the immutable payload to the
                        # active fork point while keeping the attempt snapshot
                        # as the source of truth.
                        current_target = results.get(directive.target_work_item_id)
                        restored = restored.model_copy(update={
                            "item_id": directive.target_work_item_id,
                            "fork_branch_id": branch.branch_id,
                            "supersedes_result_digest": (
                                work_item_result_digest(current_target)
                                if current_target is not None else None
                            ),
                        })
                    restore_attempt = self._record_restore_attempt(
                        store, project, directive, attempt, restored,
                    )
                    store.save_work_item_result(restored)
                    results[directive.target_work_item_id] = restored
                    store.append_event(
                        "result_restored", "restored", work_item_id=directive.target_work_item_id,
                        detail={
                            "branch_id": branch.branch_id,
                            "attempt_id": attempt.attempt_id,
                            "restore_attempt_id": restore_attempt.attempt_id,
                        },
                    )
                return {"plan": revised_plan, "results": results,
                        "execution_done": False, "execution_paused": False}
        active_ids = active_item_ids(plan, revisions)
        bound_requests = {
            row.repair_request_id for row in revisions if row.repair_request_id is not None
        }
        pending_repairs = [
            row for row in store.read_repair_requests()
            if row.repair_request_id not in bound_requests
            and not any(
                decision.action == DecisionAction.REJECT
                and row.repair_request_id in decision.target_ids
                for decision in decisions
            )
        ]
        execution_done = active_ids.issubset(results) and not pending_repairs
        return {"plan": plan, "results": results,
                "execution_done": execution_done, "execution_paused": False}

    def _repair(self, state: ResearchRuntimeState) -> dict[str, Any]:
        """Apply one deterministic Reviewer-triggered execution overlay."""
        store, project = state["store"], state["project"]
        control = store.load_control()
        if control is not None and control.request == ControlRequestKind.CANCEL:
            self._apply_cancel(store, project, state["results"], control)
            return {"plan": state["plan"], "results": state["results"],
                    "execution_done": True, "execution_paused": False, "cancelled": True}
        if control is not None and control.request == ControlRequestKind.PAUSE:
            self._apply_pause(store, project, state["results"], control)
            return {"plan": state["plan"], "results": state["results"],
                    "execution_done": False, "execution_paused": True}
        base_plan = store.load_plan()
        if base_plan is None:
            raise RuntimeError("repair gate requires an immutable base plan")
        revisions = store.read_plan_revisions()
        plan = effective_plan(base_plan, revisions)
        results = dict(state["results"])
        assessments = store.read_assessments()
        artifacts = store.read_artifacts()
        requests = store.read_repair_requests()
        resolutions = store.read_repair_resolutions()
        resolved_request_ids = {
            row.repair_request_id for row in resolutions
            if row.status in {RepairResolutionStatus.RESOLVED, RepairResolutionStatus.EXHAUSTED}
        }

        repair_revision_count = sum(1 for row in revisions if row.fork_branch_id is None)
        for revision in revisions:
            if revision.fork_branch_id is not None or revision.repair_request_id in resolved_request_ids:
                continue
            try:
                request = next(
                    row for row in requests if row.repair_request_id == revision.repair_request_id
                )
            except StopIteration as exc:
                raise ValueError(
                    f"repair revision references missing repair request: "
                    f"{revision.repair_request_id}"
                ) from exc
            resolution = build_repair_resolution(
                request=request,
                revision=revision,
                project=project,
                plan=plan,
                results=results,
                assessments=assessments,
                artifacts=artifacts,
                revisions=revisions,
                exhausted=repair_revision_count >= project.max_replans,
            )
            if resolution is not None:
                existing_resolution = next(
                    (row for row in resolutions
                     if row.repair_request_id == request.repair_request_id),
                    None,
                )
                if existing_resolution is not None and existing_resolution.status != resolution.status:
                    store.replace_repair_resolution(resolution, existing_resolution.resolution_id)
                    resolutions = [
                        row for row in resolutions
                        if row.repair_request_id != request.repair_request_id
                    ]
                    resolutions.append(resolution)
                elif existing_resolution is None:
                    store.append_repair_resolution(resolution)
                    resolutions.append(resolution)
                if existing_resolution is None or existing_resolution.status != resolution.status:
                    store.append_event(
                        "repair_resolved",
                        resolution.status.value,
                        work_item_id=request.target_work_item_id,
                        detail={
                            "repair_request_id": request.repair_request_id,
                            "revision_id": revision.revision_id,
                            "after_snapshot_digest": resolution.after_snapshot_digest,
                        },
                    )
                if resolution.status in {
                    RepairResolutionStatus.RESOLVED, RepairResolutionStatus.EXHAUSTED,
                }:
                    resolved_request_ids.add(request.repair_request_id)

        request = propose_transient_repair(
            project=project,
            base_plan=base_plan,
            plan=plan,
            results=results,
            assessments=assessments,
            artifacts=artifacts,
            revisions=revisions,
            registry=self.registry,
        )
        if request is None:
            request = propose_domain_repair(
                project=project,
                base_plan=base_plan,
                plan=plan,
                results=results,
                assessments=assessments,
                artifacts=artifacts,
                revisions=revisions,
                registry=self.registry,
            )
        if request is None:
            return {"plan": plan, "results": results, "execution_done": True, "execution_paused": False}

        existing_request = next(
            (row for row in requests if row.repair_request_id == request.repair_request_id),
            None,
        )
        if existing_request is None:
            if request.directive_id is not None:
                directive = next(
                    (row for row in store.read_repair_directives()
                     if row.directive_id == request.directive_id),
                    None,
                )
                if directive is None:
                    from .research_contracts import RepairDirective
                    store.append_repair_directive(RepairDirective(
                        directive_id=request.directive_id,
                        project_id=project.project_id,
                        work_item_id=request.target_work_item_id,
                        operation=request.action,
                        subject_key={
                            "switch_dataset_same_context": "dataset_selection",
                            "downgrade_claim": "derived_claims",
                            "supplement_evidence": "evidence_refs",
                            "exclude_evidence": "evidence_refs",
                            "split_context_same_scope": "evidence_refs",
                        }.get(request.action.value, "derived_layer"),
                        payload=request.directive_payload,
                        expected_risk=request.risk,
                        expected_authorization=request.authorization,
                        rationale=request.rationale,
                    ))
            store.append_repair_request(request)
            store.append_event(
                "repair_requested",
                request.authorization.value,
                work_item_id=request.target_work_item_id,
                detail={
                    "repair_request_id": request.repair_request_id,
                    "action": request.action.value,
                    "risk": request.risk.value,
                    "affected_work_item_ids": request.affected_work_item_ids,
                    "trigger_snapshot_digest": request.trigger_snapshot_digest,
                },
            )
        else:
            request = existing_request

        rejected = any(
            row.action == DecisionAction.REJECT and request.repair_request_id in row.target_ids
            for row in store.read_decisions()
        )
        if rejected:
            store.append_event(
                "repair_declined",
                "completed_with_gaps",
                work_item_id=request.target_work_item_id,
                detail={"repair_request_id": request.repair_request_id},
            )
            return {"plan": plan, "results": results, "execution_done": True, "execution_paused": False}

        approved = request.authorization == RepairAuthorization.AUTOMATIC or self._accepted_snapshot(
            store, request.repair_request_id, request.trigger_snapshot_digest,
        )
        if not approved:
            prior_state = store.load_state() or ProjectState(project_id=project.project_id)
            store.save_state(ProjectState(
                project_id=project.project_id,
                status=ProjectStatus.WAITING_REVIEW,
                current_item_id=request.target_work_item_id,
                completed_items=_completed_item_ids(results),
                attempts=prior_state.attempts,
                checkpoint_kind="repair", checkpoint_target_id=request.repair_request_id,
                checkpoint_snapshot_digest=request.trigger_snapshot_digest,
                terminal_reason=f"Repair approval is required for {request.repair_request_id}.",
            ))
            if not any(
                row.event_type == "human_checkpoint"
                and row.detail.get("repair_request_id") == request.repair_request_id
                for row in store.read_events()
            ):
                store.append_event(
                    "human_checkpoint",
                    "repair_approval_required",
                    work_item_id=request.target_work_item_id,
                    detail={
                        "repair_request_id": request.repair_request_id,
                        "trigger_snapshot_digest": request.trigger_snapshot_digest,
                    },
                )
            return {"plan": plan, "results": results, "execution_done": False, "execution_paused": True}

        revision = build_plan_revision(
            request=request,
            base_plan=base_plan,
            plan=plan,
            assessments=assessments,
            artifacts=artifacts,
            revisions=revisions,
        )
        store.append_plan_revision(revision)
        if not any(
            row.action == DecisionAction.REPLAN and revision.revision_id in row.target_ids
            for row in store.read_decisions()
        ):
            store.append_decision(DecisionEvent(
                project_id=project.project_id,
                action=DecisionAction.REPLAN,
                target_ids=[request.repair_request_id, revision.revision_id],
                rationale=request.rationale,
                actor="deterministic_repair_policy",
                evidence_snapshot_digest=request.trigger_snapshot_digest,
                reversible=True,
            ))
        store.append_event(
            "plan_revised",
            "repair_applied",
            work_item_id=request.target_work_item_id,
            detail={
                "repair_request_id": request.repair_request_id,
                "revision_id": revision.revision_id,
                "revision_digest": revision.revision_digest,
                "added_work_item_ids": [row.item_id for row in revision.added_items],
                "superseded_work_item_ids": revision.superseded_item_ids,
            },
        )
        revised_plan = effective_plan(base_plan, [*revisions, revision])
        return {"plan": revised_plan, "results": results, "execution_done": False, "execution_paused": False}

    def _finalize(self, state: ResearchRuntimeState) -> dict[str, Any]:
        store, project, plan, results = state["store"], state["project"], state["plan"], state["results"]
        revisions = store.read_plan_revisions()
        active_ids = active_item_ids(plan, revisions)
        active_results = {item_id: row for item_id, row in results.items() if item_id in active_ids}
        assessments = active_assessments(store.read_assessments(), revisions)
        blocking = [row for row in assessments if row.blocking and row.result == AssessmentResult.FAIL]
        required = [active_results.get(item.item_id) for item in plan.items
                    if item.item_id in active_ids and item.required]
        needs_input = [row for row in required if row is not None and row.status == WorkItemStatus.NEEDS_INPUT]
        hard_failures = [row for row in required if row is None or row.status in {
            WorkItemStatus.FAILED, WorkItemStatus.BLOCKED, WorkItemStatus.SKIPPED,
        }]
        gaps = [row for row in required if row is not None and row.status == WorkItemStatus.COMPLETED_WITH_GAPS]
        active_artifacts = [row for row in store.read_artifacts() if row.work_item_id in active_ids]
        report_artifacts = [row for row in active_artifacts if row.logical_name == "research_report"]
        completed_count = sum(row.status in {WorkItemStatus.COMPLETED, WorkItemStatus.COMPLETED_WITH_GAPS}
                              for row in active_results.values())
        target_items = [item for item in plan.items
                        if item.item_id in active_ids and item.module == "target_discovery"]
        target_result = active_results.get(target_items[-1].item_id) if target_items else None
        if project.domain == "disease_target_discovery" and (
            target_result is None or target_result.outputs.get("deliverables_complete") is not True
        ):
            if target_result is not None and target_result not in needs_input and target_result not in hard_failures:
                gaps.append(target_result)
        if completed_count == 0:
            terminal = ProjectStatus.FAILED
            reason = "No required research work item completed."
        elif needs_input:
            terminal = ProjectStatus.NEEDS_INPUT
            reason = f"Project requires user input for {len(needs_input)} required work item(s)."
        elif hard_failures or gaps or blocking or not report_artifacts:
            terminal = ProjectStatus.COMPLETED_WITH_GAPS
            reason = (f"Project retained {len(hard_failures)} incomplete required items, {len(gaps)} gap-bearing items, "
                      f"{len(blocking)} blocking assessments, and {0 if report_artifacts else 1} missing final reports.")
        else:
            terminal = ProjectStatus.COMPLETED
            reason = "All required work items and release gates completed."
        store.assert_integrity()
        release_snapshot = project_snapshot_digest(
            plan=plan,
            results=results,
            assessments=store.read_assessments(),
            artifacts=store.read_artifacts(),
            revisions=revisions,
        )
        if terminal == ProjectStatus.COMPLETED and project.autonomy_mode != AutonomyMode.AUTONOMOUS:
            release_target = f"release:{release_snapshot}"
            if not self._accepted_snapshot(store, release_target, release_snapshot):
                prior_state = store.load_state() or ProjectState(project_id=project.project_id)
                store.save_state(ProjectState(
                    project_id=project.project_id, status=ProjectStatus.WAITING_REVIEW,
                    completed_items=_completed_item_ids(results), attempts=prior_state.attempts,
                    checkpoint_kind="release", checkpoint_target_id=release_target,
                    checkpoint_snapshot_digest=release_snapshot,
                    terminal_reason=f"Human release acceptance is required for {release_target}.",
                ))
                store.append_event("human_checkpoint", "release_approval_required",
                                   detail={"target_id": release_target})
                return {}
        if terminal in {ProjectStatus.NEEDS_INPUT, ProjectStatus.COMPLETED_WITH_GAPS}:
            action_targets = [row.item_id for row in [*needs_input, *hard_failures] if row is not None]
            action_targets += [row.assessment_id for row in blocking]
            if not any(
                row.action == DecisionAction.REQUEST_EVIDENCE
                and row.evidence_snapshot_digest == release_snapshot
                for row in store.read_decisions()
            ):
                store.append_decision(DecisionEvent(
                    project_id=project.project_id, action=DecisionAction.REQUEST_EVIDENCE,
                    target_ids=list(dict.fromkeys(action_targets)), rationale=reason,
                    actor="research_runtime", evidence_snapshot_digest=release_snapshot, reversible=True,
                ))
        elif terminal == ProjectStatus.COMPLETED:
            if not any(
                row.action == DecisionAction.RELEASE
                and row.evidence_snapshot_digest == release_snapshot
                for row in store.read_decisions()
            ):
                store.append_decision(DecisionEvent(
                    project_id=project.project_id, action=DecisionAction.RELEASE,
                    target_ids=[row.artifact_id for row in report_artifacts], rationale=reason,
                    actor="research_runtime", evidence_snapshot_digest=release_snapshot, reversible=False,
                ))
        failed_ids = [item_id for item_id, row in active_results.items()
                      if row.status in {WorkItemStatus.FAILED, WorkItemStatus.BLOCKED, WorkItemStatus.NEEDS_INPUT}]
        final_state = ProjectState(
            project_id=project.project_id, status=terminal,
            completed_items=_completed_item_ids(active_results), failed_items=sorted(failed_ids),
            attempts=(store.load_state() or ProjectState(project_id=project.project_id)).attempts,
            terminal_reason=reason, updated_at=utc_now(),
        )
        store.save_state(final_state)
        store.append_event("project_terminal", terminal.value, detail={
            "reason": reason,
            "release_snapshot_digest": release_snapshot,
        })
        return {}

    def run(self, project: ResearchProjectSpec, resume: bool = False) -> dict[str, Any]:
        store = ResearchProjectStore(self.projects_dir, project.project_id)
        existing = store.load_spec()
        current = store.load_state()
        if existing is not None:
            incoming_payload = project.model_dump(mode="json", exclude={"created_at"})
            stored_payload = existing.model_dump(mode="json", exclude={"created_at"})
            if incoming_payload != stored_payload:
                raise ValueError("project id already exists with a different immutable research goal")
        if current is not None and not resume:
            raise ValueError("project already has durable progress; use resume to continue or inspect it")
        try:
            with store.execution_lock():
                self._graph.invoke(
                    {"project": project, "project_id": project.project_id, "resume": resume},
                    config={"recursion_limit": project.max_work_items * (project.max_replans + project.max_forks + 1) + 40},
                )
        except Exception as exc:
            existing = store.load_spec()
            current = store.load_state()
            terminal = current and current.status in {
                ProjectStatus.COMPLETED, ProjectStatus.COMPLETED_WITH_GAPS,
                ProjectStatus.FAILED, ProjectStatus.CANCELLED,
            }
            if existing is not None and not terminal:
                store.save_state(ProjectState(
                    project_id=project.project_id, status=ProjectStatus.FAILED,
                    completed_items=_completed_item_ids(store.load_work_item_results()),
                    attempts=current.attempts if current else {},
                    terminal_reason=(
                        f"Unhandled runtime error: {exc.__class__.__name__}: {exc}"
                    ),
                ))
                store.append_event("project_terminal", "failed", detail={
                    # Structured worker failure record: task identifier,
                    # exception type/message and wall-clock time. The executor
                    # re-raises so callers observe the failure; later tasks can
                    # still be processed by the next run() call.
                    "task_id": project.project_id,
                    "work_item_id": current.current_item_id if current is not None else None,
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc) or None,
                    "at": utc_now(),
                })
            raise
        state = store.load_state()
        if state is None:
            raise RuntimeError("research runtime did not persist project state")
        return state.model_dump(mode="json")


__all__ = ["ResearchProjectRuntime", "ResearchRuntimeState", "validate_data_contract"]
