"""Application service for durable Target research projects.

The HTTP workbench, CLI and MCP adapter should operate on this service rather
than inventing separate project semantics.  It exposes only durable project
records and never returns deployment paths or credentials.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .contracts import EvidenceItem, TaskContext, TaskSpec, utc_now
from .graphs import synthesize_evidence_graph
from .paper_strategy import StrategyPattern
from .research_contracts import (
    RESEARCH_CONTRACT_VERSION,
    AutonomyMode,
    ControlRequestKind,
    DecisionAction,
    DecisionEvent,
    DomainActivityPage,
    ForkDirective,
    ForkMode,
    PlanBranch,
    PlanBranchStatus,
    ProjectState,
    ProjectStatus,
    RepairQueueSnapshot,
    ResearchGoal,
    ResearchProjectControl,
    ResearchProjectSnapshot,
    ResearchProjectSpec,
    WorkItemResult,
    WorkItemStatus,
)
from .research_runtime import ResearchProjectRuntime
from .workflow_catalog import WorkflowCatalog
from .research_projection import summarize_domain_activities
from .research_repair import (
    active_item_ids, effective_plan, fork_affected_item_ids, project_snapshot_digest,
)
from .research_store import ProjectBusyError, ResearchProjectStore


_TERMINAL_PROJECT_STATUSES = {
    ProjectStatus.COMPLETED,
    ProjectStatus.COMPLETED_WITH_GAPS,
    ProjectStatus.FAILED,
    ProjectStatus.CANCELLED,
}


class ResearchProjectNotFound(ValueError):
    """Raised when a requested durable project does not exist."""


class ResearchDecisionError(ValueError):
    """Raised when a checkpoint decision is not part of the frozen plan."""


class ResearchProjectService:
    """Stable product-facing operations over the research runtime and store."""

    def __init__(self, runtime: ResearchProjectRuntime):
        self.runtime = runtime
        self.projects_dir = runtime.projects_dir
        self.workflow_catalog = WorkflowCatalog(runtime.settings.workflow_catalog_path)

    def workflow_templates(self) -> list[dict[str, Any]]:
        """Public, secret-free summary of executable workflow templates."""
        return [
            {
                "template_id": template.template_id,
                "description": template.description,
                "product": template.product,
                "domain": template.domain,
                "task_types": template.task_types,
                "modules": [item.module for item in template.modules],
                "required_modules": [item.module for item in template.modules if item.required],
                "max_work_items": template.max_work_items,
                "human_checkpoints": template.human_checkpoints,
                "contract_version": template.contract_version,
                "source_sha256": template.source_sha256,
            }
            for template in self.workflow_catalog.list_templates()
        ]

    def build_disease_project(
        self,
        *,
        question: str,
        disease: str,
        title: str | None = None,
        project_id: str | None = None,
        disease_subtype: str | None = None,
        tissue: str | None = None,
        cell_type: str | None = None,
        disease_stage: str | None = None,
        desired_phenotype: str | None = None,
        organism: str = "Homo sapiens",
        autonomy_mode: str = AutonomyMode.CHECKPOINTED.value,
        workflow_template: str | None = None,
    ) -> ResearchProjectSpec:
        """Build the typed minimum intake for a public-data target project.

        Missing biological context is preserved as missing.  The downstream
        workflow may request it or complete with gaps; this helper never infers
        tissue, cell type, disease stage or phenotype from prose.
        """
        mode = AutonomyMode(autonomy_mode)
        target_task = TaskSpec(
            task_type="disease_to_target",
            question=question,
            context=TaskContext(
                disease=disease,
                disease_subtype=disease_subtype,
                organism=organism,
                tissue=tissue,
                cell_type=cell_type,
                disease_stage=disease_stage,
                desired_phenotype=desired_phenotype,
            ),
            requested_outputs=[
                "ranked_targets",
                "target_cards",
                "falsifiable_experiment_plans",
                "traceable_research_report",
            ],
        )
        values: dict[str, Any] = {
            "title": title or f"Target research: {disease}",
            "domain": "disease_target_discovery",
            "goal": ResearchGoal(
                question=question,
                success_criteria=[
                    "Every material conclusion traces to a source or reproducible tool result.",
                    "Candidate ranking preserves supporting, opposing and missing evidence separately.",
                    "Priority targets include falsifiable experiments and explicit scientific boundaries.",
                ],
                deliverables=[
                    "Ranked target list",
                    "TargetCards",
                    "Falsifiable experiment plans",
                    "Machine-readable evidence package",
                    "Human-readable research report",
                ],
                constraints=[
                    "Use public data only unless a later immutable project explicitly changes this constraint.",
                    "Do not represent retrieval, association or model prediction as causal biological truth.",
                ],
            ),
            "context": {"target_task_spec": target_task.model_dump(mode="json")},
            "autonomy_mode": mode,
        }
        self._bind_workflow_template(values, workflow_template)
        if project_id:
            values["project_id"] = project_id
        return ResearchProjectSpec.model_validate(values)

    def build_generic_project(
        self,
        *,
        question: str,
        title: str | None = None,
        project_id: str | None = None,
        workflow: str = "literature_review",
        autonomy_mode: str = AutonomyMode.CHECKPOINTED.value,
    ) -> ResearchProjectSpec:
        """Build a non-target research project from an executable workflow template.

        The template decides which typed modules run (for example literature
        review -> hypotheses -> independent review -> report).  Missing domain
        context stays missing; the workflow requests it or completes with gaps.
        """
        mode = AutonomyMode(autonomy_mode)
        values: dict[str, Any] = {
            "title": title or f"Research project: {question[:60]}",
            "domain": "life_science",
            "goal": ResearchGoal(
                question=question,
                success_criteria=[
                    "Every material conclusion traces to a source or reproducible tool result.",
                    "Independent review and report gates are satisfied before release.",
                    "Missing evidence is reported as an explicit gap, never fabricated.",
                ],
                deliverables=[
                    "Frozen research brief",
                    "Source-indexed literature evidence",
                    "Falsifiable hypotheses",
                    "Independent review record",
                    "Human-readable research report",
                    "Machine-readable evidence package",
                ],
                constraints=[
                    "Use public data only unless a later immutable project explicitly changes this constraint.",
                    "Retrieval, association and prediction are never presented as causal biological truth.",
                ],
            ),
            "context": {"literature_query": question},
            "autonomy_mode": mode,
        }
        self._bind_workflow_template(values, workflow)
        if project_id:
            values["project_id"] = project_id
        return ResearchProjectSpec.model_validate(values)

    def _bind_workflow_template(
        self, values: dict[str, Any], workflow_template: str | None,
    ) -> None:
        if not workflow_template:
            return
        template = self.workflow_catalog.get(workflow_template)
        values["workflow_template"] = template.template_id
        values["workflow_template_sha256"] = template.source_sha256
        values["max_work_items"] = min(values.get("max_work_items", 12), template.max_work_items)

    def reserve(self, project: ResearchProjectSpec) -> dict[str, Any]:
        """Persist an immutable project specification without executing it."""
        store = self._store(project.project_id)
        created = store.create(project)
        return {
            "created": created,
            "project": self.snapshot(project.project_id),
        }

    def run(self, project_id: str) -> dict[str, Any]:
        """Advance a project until its next terminal state or human checkpoint."""
        store = self._existing_store(project_id)
        project = store.load_spec()
        assert project is not None
        state = store.load_state()
        resume = state is not None
        self.runtime.run(project, resume=resume)
        return self.snapshot(project_id)


    def cancel_project(self, project_id: str, *, actor: str, rationale: str) -> dict[str, Any]:
        """Cancel a project immediately, or queue cancellation for the next safe boundary.

        If an execution holds the project lock, the request is persisted and the
        running graph consumes it before starting the next work item. Otherwise
        the cancellation is applied synchronously. A cancelled project is
        terminal: no ranking, report, release or downstream artifact is produced.
        """
        return self._request_control(project_id, ControlRequestKind.CANCEL, actor, rationale)

    def pause_project(self, project_id: str, *, actor: str, rationale: str) -> dict[str, Any]:
        """Pause a project immediately, or queue pause for the next safe boundary.

        Pausing never consumes a worker lease or marks work items; resuming
        continues from the last durable item boundary.
        """
        return self._request_control(project_id, ControlRequestKind.PAUSE, actor, rationale)

    def resume_project(self, project_id: str, *, actor: str, rationale: str) -> dict[str, Any]:
        """Resume a paused (or checkpointed) project and advance until the next stop."""
        if not actor.strip() or not rationale.strip():
            raise ResearchDecisionError("actor and rationale are required")
        store = self._existing_store(project_id)
        state = store.load_state()
        if state is not None and state.status in _TERMINAL_PROJECT_STATUSES:
            # Resume on a terminal project is an idempotent inspect-only no-op,
            # matching the runtime's terminal-resume semantics (BM-04).
            return self.snapshot(project_id)
        pending = store.load_control()
        if pending is not None and pending.request != ControlRequestKind.NONE:
            raise ResearchDecisionError(
                f"a {pending.request.value} control request is pending; resolve it before resuming"
            )
        store.append_event(
            "control_requested", state.status.value if state else "draft",
            detail={"control": "resume", "actor": actor, "rationale": rationale},
        )
        return self.run(project_id)

    def _request_control(
        self,
        project_id: str,
        kind: ControlRequestKind,
        actor: str,
        rationale: str,
    ) -> dict[str, Any]:
        if not actor.strip() or not rationale.strip():
            raise ResearchDecisionError("actor and rationale are required")
        store = self._existing_store(project_id)
        state = store.load_state()
        if state is not None and state.status in _TERMINAL_PROJECT_STATUSES:
            raise ResearchDecisionError("project already reached a terminal state")
        control = ResearchProjectControl(
            project_id=project_id, request=kind, actor=actor, rationale=rationale,
        )
        store.save_control(control)
        store.append_event(
            "control_requested", state.status.value if state else "draft",
            detail={"control": kind.value, "actor": actor, "rationale": rationale},
        )
        applied = False
        try:
            with store.execution_lock():
                if kind == ControlRequestKind.CANCEL:
                    self._apply_cancel(store, control)
                else:
                    self._apply_pause(store, control)
                applied = True
        except ProjectBusyError:
            # An execution holds the project lock; the runtime consumes the
            # queued request at its next safe work-item boundary.
            pass
        snapshot = self.snapshot(project_id)
        snapshot["control_queued"] = not applied
        return snapshot

    @staticmethod
    def _apply_cancel(store: ResearchProjectStore, control: ResearchProjectControl) -> None:
        spec = store.load_spec()
        assert spec is not None
        plan = store.load_plan()
        revisions = store.read_plan_revisions()
        results = store.load_work_item_results()
        active_ids = active_item_ids(plan, revisions) if plan is not None else set()
        if plan is not None:
            for item in plan.items:
                if item.item_id in active_ids and item.item_id not in results:
                    store.save_work_item_result(WorkItemResult(
                        item_id=item.item_id, module=item.module, status=WorkItemStatus.SKIPPED,
                        summary="The project was cancelled before this work item started.",
                        limitations=[f"Project cancelled by {control.actor}: {control.rationale}"],
                    ))
        prior = store.load_state()
        store.save_state(ProjectState(
            project_id=spec.project_id, status=ProjectStatus.CANCELLED,
            current_item_id=prior.current_item_id if prior else None,
            completed_items=sorted(
                item_id for item_id, row in results.items()
                if row.status in {WorkItemStatus.COMPLETED, WorkItemStatus.COMPLETED_WITH_GAPS}
            ),
            failed_items=prior.failed_items if prior else [],
            attempts=prior.attempts if prior else {},
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

    @staticmethod
    def _apply_pause(store: ResearchProjectStore, control: ResearchProjectControl) -> None:
        spec = store.load_spec()
        assert spec is not None
        prior = store.load_state()
        store.save_state(ProjectState(
            project_id=spec.project_id, status=ProjectStatus.PAUSED,
            current_item_id=prior.current_item_id if prior else None,
            completed_items=prior.completed_items if prior else [],
            failed_items=prior.failed_items if prior else [],
            attempts=prior.attempts if prior else {},
            terminal_reason=f"Paused by {control.actor}: {control.rationale}",
            updated_at=utc_now(),
        ))
        store.append_event("execution_paused", "paused", detail={
            "actor": control.actor, "rationale": control.rationale,
        })
        store.clear_control()

    def accept_checkpoint(
        self,
        *,
        project_id: str,
        target_id: str,
        actor: str,
        rationale: str,
        resume: bool = False,
    ) -> dict[str, Any]:
        """Accept a frozen plan, supervised item or release gate.

        The accepted target must already exist in the immutable plan.  This is
        intentionally narrower than a generic decision API because the current
        runtime does not yet execute project-level reject/replan semantics.
        """
        if not actor.strip() or not rationale.strip():
            raise ResearchDecisionError("actor and rationale are required")
        store = self._existing_store(project_id)
        project, base_plan = store.load_spec(), store.load_plan()
        if project is None or base_plan is None:
            raise ResearchDecisionError("project does not yet have a frozen plan")
        revisions = store.read_plan_revisions()
        plan = effective_plan(base_plan, revisions)
        release_snapshot = self._release_snapshot(store, plan)
        release_target = f"release:{release_snapshot}"
        allowed = {
            base_plan.plan_id,
            *(item.item_id for item in plan.items),
            release_target,
        }
        if target_id not in allowed:
            raise ResearchDecisionError("decision target is not part of the frozen project plan")
        decision_digest = release_snapshot if target_id == release_target else None
        decision = next(
            (
                row
                for row in store.read_decisions()
                if row.action == DecisionAction.ACCEPT
                and target_id in row.target_ids
                and row.evidence_snapshot_digest == decision_digest
            ),
            None,
        )
        if decision is None:
            decision = DecisionEvent(
                project_id=project_id,
                action=DecisionAction.ACCEPT,
                target_ids=[target_id],
                actor=actor.strip(),
                rationale=rationale.strip(),
                evidence_snapshot_digest=decision_digest,
                reversible=False,
            )
            store.append_decision(decision)
        if resume:
            self.runtime.run(project, resume=True)
        return {
            "decision": decision.model_dump(mode="json"),
            "project": self.snapshot(project_id),
        }

    def decide_repair(
        self,
        *,
        project_id: str,
        repair_request_id: str,
        trigger_snapshot_digest: str,
        approve: bool,
        actor: str,
        rationale: str,
        resume: bool = False,
    ) -> dict[str, Any]:
        """Approve or reject one exact, immutable repair request snapshot."""
        if not actor.strip() or not rationale.strip():
            raise ResearchDecisionError("actor and rationale are required")
        store = self._existing_store(project_id)
        request = next(
            (row for row in store.read_repair_requests() if row.repair_request_id == repair_request_id),
            None,
        )
        if request is None:
            raise ResearchDecisionError("repair request does not exist")
        if request.trigger_snapshot_digest != trigger_snapshot_digest:
            raise ResearchDecisionError("stale repair snapshot digest")
        action = DecisionAction.ACCEPT if approve else DecisionAction.REJECT
        opposite = DecisionAction.REJECT if approve else DecisionAction.ACCEPT
        try:
            with store.execution_lock():
                decisions = store.read_decisions()
                if any(
                    row.action == opposite
                    and repair_request_id in row.target_ids
                    and row.evidence_snapshot_digest == trigger_snapshot_digest
                    for row in decisions
                ):
                    raise ResearchDecisionError("repair request already has an immutable opposite decision")
                decision = next(
                    (
                        row for row in decisions
                        if row.action == action
                        and repair_request_id in row.target_ids
                        and row.evidence_snapshot_digest == trigger_snapshot_digest
                    ),
                    None,
                )
                if decision is None:
                    decision = DecisionEvent(
                        project_id=project_id,
                        action=action,
                        target_ids=[repair_request_id],
                        actor=actor.strip(),
                        rationale=rationale.strip(),
                        evidence_snapshot_digest=trigger_snapshot_digest,
                        reversible=False,
                    )
                    store.append_decision(decision)
        except ProjectBusyError as exc:
            raise ResearchDecisionError("project decision is busy; retry") from exc
        project = store.load_spec()
        if resume and project is not None:
            self.runtime.run(project, resume=True)
        return {
            "decision": decision.model_dump(mode="json"),
            "project": self.snapshot(project_id),
        }

    def propose_fork(
        self,
        project_id: str,
        *,
        target_work_item_id: str,
        mode: str,
        rationale: str,
        actor: str,
        rollback_to_attempt_id: str | None = None,
        input_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Issue a user-requested rollback branch (redo or restore).

        The directive is immutable and bound to the exact project snapshot.
        A checkpointed project pauses for approval; autonomous redo forks are
        auto-approved by the runtime on the next run.
        """
        if not actor.strip() or not rationale.strip():
            raise ResearchDecisionError("actor and rationale are required")
        store = self._existing_store(project_id)
        spec = store.load_spec()
        assert spec is not None
        base_plan = store.load_plan()
        if base_plan is None:
            raise ResearchDecisionError("project does not yet have a frozen plan")
        revisions = store.read_plan_revisions()
        branches = store.read_branches()
        if len(branches) >= spec.max_forks:
            raise ResearchDecisionError(f"fork budget exhausted (max_forks={spec.max_forks})")
        plan = effective_plan(base_plan, revisions)
        active = active_item_ids(plan, revisions)
        fork_mode = ForkMode(mode)
        by_id = {item.item_id: item for item in plan.items}

        def ancestors_of(item_id: str) -> set[str]:
            seen: set[str] = set()
            current = by_id.get(item_id)
            while current is not None and current.rerun_of_item_id is not None:
                if current.rerun_of_item_id in seen:
                    break
                seen.add(current.rerun_of_item_id)
                current = by_id.get(current.rerun_of_item_id)
            return seen

        if target_work_item_id not in active:
            # A restore may target the original logical step even when a fork
            # branch replaced it; resolve to the unique active representative.
            if fork_mode == ForkMode.RESTORE:
                representatives = [
                    item_id for item_id in active
                    if target_work_item_id in ancestors_of(item_id)
                ]
                if len(representatives) == 1:
                    target_work_item_id = representatives[0]
            if target_work_item_id not in active:
                raise ResearchDecisionError("fork target is not an active work item")
        results = store.load_work_item_results()
        if target_work_item_id not in results:
            raise ResearchDecisionError("fork target has not produced a result yet")
        affected = fork_affected_item_ids(plan, target_work_item_id, fork_mode, active)
        overrides = dict(input_overrides or {})
        unknown_overrides = set(overrides) - set(affected)
        if unknown_overrides:
            raise ResearchDecisionError(
                f"fork input overrides target non-affected work items: {sorted(unknown_overrides)}"
            )
        current_snapshot = project_snapshot_digest(
            plan=plan, results=results, assessments=store.read_assessments(),
            artifacts=store.read_artifacts(), revisions=revisions,
        )
        if fork_mode == ForkMode.RESTORE:
            if not rollback_to_attempt_id:
                raise ResearchDecisionError("restore fork requires rollback_to_attempt_id")
            allowed_attempt_items = {target_work_item_id} | ancestors_of(target_work_item_id)
            attempt = next(
                (row for row in store.read_attempts()
                 if row.attempt_id == rollback_to_attempt_id
                 and row.work_item_id in allowed_attempt_items),
                None,
            )
            if attempt is None or attempt.output_digest is None or attempt.status.value not in {
                "completed", "completed_with_gaps",
            }:
                raise ResearchDecisionError("restore attempt is not a terminal completed result")
            snapshot = store.load_attempt_result(attempt.attempt_id)
            if snapshot is None:
                raise ResearchDecisionError("restore attempt result snapshot is missing")
        else:
            rollback_to_attempt_id = None
        fork_count = len(branches) + 1
        branch_id = ResearchProjectRuntime._new_contract_id("branch")
        directive = ForkDirective(
            fork_directive_id=ResearchProjectRuntime._new_contract_id("fork"),
            project_id=project_id,
            branch_id=branch_id,
            target_work_item_id=target_work_item_id,
            mode=fork_mode,
            rollback_to_attempt_id=rollback_to_attempt_id,
            snapshot_digest=current_snapshot,
            input_overrides=overrides,
            rationale=rationale.strip(),
            actor=actor.strip(),
        )
        branch = PlanBranch(
            branch_id=branch_id,
            project_id=project_id,
            base_plan_id=base_plan.plan_id,
            parent_branch_id=branches[-1].branch_id if branches else None,
            fork_directive_id=directive.fork_directive_id,
            fork_point_item_id=target_work_item_id,
            mode=fork_mode,
            rollback_to_attempt_id=rollback_to_attempt_id,
            fork_count=fork_count,
            superseded_item_ids=affected,
            added_item_ids=[f"{item_id}__fork_{fork_count}" for item_id in affected],
            before_snapshot_digest=current_snapshot,
            status=PlanBranchStatus.PROPOSED,
        )
        store.append_fork_directive(directive)
        store.append_branch_snapshot(branch)
        store.append_event(
            "fork_proposed", "proposed", work_item_id=target_work_item_id,
            detail={
                "branch_id": branch.branch_id,
                "fork_directive_id": directive.fork_directive_id,
                "mode": fork_mode.value,
                "superseded_work_item_ids": affected,
                "snapshot_digest": current_snapshot,
            },
        )
        prior_state = store.load_state() or ProjectState(project_id=project_id)
        store.save_state(ProjectState(
            project_id=project_id, status=ProjectStatus.WAITING_REVIEW,
            current_item_id=target_work_item_id,
            completed_items=[row.item_id for row in results.values()
                             if row.status.value in {"completed", "completed_with_gaps"}],
            attempts=prior_state.attempts,
            checkpoint_kind="fork", checkpoint_target_id=branch.branch_id,
            checkpoint_snapshot_digest=current_snapshot,
            terminal_reason=f"Fork approval is required for {branch.branch_id}.",
        ))
        return self.snapshot(project_id)

    def decide_fork(
        self,
        project_id: str,
        *,
        branch_id: str,
        approve: bool,
        actor: str,
        rationale: str,
        resume: bool = False,
    ) -> dict[str, Any]:
        """Approve or reject one exact, immutable fork branch snapshot."""
        if not actor.strip() or not rationale.strip():
            raise ResearchDecisionError("actor and rationale are required")
        store = self._existing_store(project_id)
        branch = store.current_branch(branch_id)
        if branch is None:
            raise ResearchDecisionError("fork branch does not exist")
        if branch.revision_id is not None:
            raise ResearchDecisionError("fork branch is already applied")
        directive = next(
            (row for row in store.read_fork_directives()
             if row.fork_directive_id == branch.fork_directive_id),
            None,
        )
        if directive is None:
            raise ResearchDecisionError("fork branch references a missing directive")
        action = DecisionAction.ACCEPT if approve else DecisionAction.REJECT
        try:
            with store.execution_lock():
                decisions = store.read_decisions()
                if any(
                    row.action != action
                    and branch_id in row.target_ids
                    and row.evidence_snapshot_digest == branch.before_snapshot_digest
                    for row in decisions
                ):
                    raise ResearchDecisionError("fork branch already has an immutable opposite decision")
                decision = next(
                    (row for row in decisions
                     if row.action == action
                     and branch_id in row.target_ids
                     and row.evidence_snapshot_digest == branch.before_snapshot_digest),
                    None,
                )
                if decision is None:
                    decision = DecisionEvent(
                        project_id=project_id,
                        action=action,
                        target_ids=[branch_id],
                        actor=actor.strip(),
                        rationale=rationale.strip(),
                        evidence_snapshot_digest=branch.before_snapshot_digest,
                        reversible=False,
                    )
                    store.append_decision(decision)
                updated = branch.model_copy(update={
                    "status": PlanBranchStatus.APPROVED if approve else PlanBranchStatus.REJECTED,
                })
                store.append_branch_snapshot(updated)
                store.append_event(
                    "fork_decided", "approved" if approve else "rejected",
                    work_item_id=branch.fork_point_item_id,
                    detail={"branch_id": branch.branch_id, "snapshot_digest": branch.before_snapshot_digest},
                )
        except ProjectBusyError as exc:
            raise ResearchDecisionError("project decision is busy; retry") from exc
        project = store.load_spec()
        if resume and project is not None:
            self.runtime.run(project, resume=True)
        return {
            "decision": decision.model_dump(mode="json"),
            "project": self.snapshot(project_id),
        }

    def branches(self, project_id: str) -> dict[str, Any]:
        """Return the durable fork branch history for one project."""
        store = self._existing_store(project_id)
        return {
            "project_id": project_id,
            "fork_directives": [row.model_dump(mode="json") for row in store.read_fork_directives()],
            "branches": [row.model_dump(mode="json") for row in store.read_branches()],
        }

    def list_projects(self) -> list[dict[str, Any]]:
        """List durable project summaries without exposing filesystem paths."""
        if not self.projects_dir.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(self.projects_dir.iterdir(), key=lambda item: item.name):
            if not path.is_dir():
                continue
            try:
                store = self._store(path.name)
                spec = store.load_spec()
                if spec is None:
                    continue
                state = store.load_state()
            except ValueError:
                continue
            rows.append(
                {
                    "project_id": spec.project_id,
                    "title": spec.title,
                    "domain": spec.domain,
                    "status": state.status.value if state else ProjectStatus.DRAFT.value,
                    "updated_at": state.updated_at if state else spec.created_at,
                }
            )
        return rows

    def snapshot(self, project_id: str) -> dict[str, Any]:
        """Return the complete safe control-plane projection for one project."""
        store = self._existing_store(project_id)
        spec = store.load_spec()
        assert spec is not None
        state = store.load_state()
        base_plan = store.load_plan()
        revisions = store.read_plan_revisions()
        plan = effective_plan(base_plan, revisions) if base_plan is not None else None
        results = store.load_work_item_results()
        events = store.read_events()
        activities = store.read_domain_activities()
        return ResearchProjectSnapshot(
            spec=spec,
            state=state,
            plan=plan,
            work_item_results=[results[key] for key in sorted(results)],
            artifacts=store.read_artifacts(),
            assessments=store.read_assessments(),
            decisions=store.read_decisions(),
            event_cursor=events[-1].sequence if events else 0,
            domain_activity_cursor=activities[-1].sequence if activities else 0,
            domain_stage_summary=summarize_domain_activities(activities),
            repair_requests=store.read_repair_requests(),
            plan_revisions=revisions,
            repair_resolutions=store.read_repair_resolutions(),
            fork_directives=store.read_fork_directives(),
            plan_branches=store.read_branches(),
            work_attempts=store.read_attempts(),
            artifact_versions=store.read_artifact_versions(),
            review_targets=store.read_review_targets(),
            worker_leases=store.read_leases(),
            work_item_heads=store.read_work_item_heads(),
            artifact_heads=store.read_artifact_heads(),
            active_work_item_ids=sorted(active_item_ids(plan, revisions)) if plan is not None else [],
            active_artifact_ids=sorted(
                head.artifact_id
                for head in store.read_artifact_heads()
                if plan is not None and head.work_item_id in active_item_ids(plan, revisions)
            ),
            release_snapshot_digest=self._release_snapshot(store, plan) if plan is not None else None,
            next_actions=self._next_actions(store),
        ).model_dump(mode="json")

    def repairs(self, project_id: str) -> dict[str, Any]:
        store = self._existing_store(project_id)
        spec = store.load_spec()
        assert spec is not None
        revisions = store.read_plan_revisions()
        return RepairQueueSnapshot(
            project_id=project_id,
            requests=store.read_repair_requests(),
            revisions=revisions,
            resolutions=store.read_repair_resolutions(),
            remaining_replans=max(
                0, spec.max_replans - sum(1 for row in revisions if row.fork_branch_id is None)
            ),
        ).model_dump(mode="json")

    def events(self, project_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        """Read replayable ordered events after a client cursor."""
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        store = self._existing_store(project_id)
        return [
            row.model_dump(mode="json")
            for row in store.read_events()
            if row.sequence > after_sequence
        ]

    def domain_activities(
        self,
        project_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        """Page through the safe, source-linked child workflow activity index."""
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        store = self._existing_store(project_id)
        available = store.read_domain_activities(
            after_sequence=after_sequence,
            work_item_id=work_item_id,
        )
        page = available[:limit]
        return DomainActivityPage(
            project_id=project_id,
            activities=page,
            next_cursor=page[-1].sequence if page else after_sequence,
            has_more=len(available) > len(page),
        ).model_dump(mode="json")

    def evidence_graph(self, project_id: str) -> dict[str, Any]:
        """Project DAG: work-item dependencies plus artifact producers.

        This is the machine-readable view behind the workbench graph panel.
        Nodes and edges are derived only from the durable snapshot; nothing
        is invented client-side.
        """
        snap = self.snapshot(project_id)
        plan = snap.get("plan") or {}
        items = plan.get("items") or []
        results = {row["item_id"]: row for row in (snap.get("work_item_results") or [])}
        artifacts = snap.get("artifacts") or []
        branches = snap.get("plan_branches") or []
        active = set(snap.get("active_work_item_ids") or [])
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        if items:
            for item in items:
                item_id = item["item_id"]
                result = results.get(item_id)
                nodes.append({
                    "id": f"work:{item_id}",
                    "kind": "work_item",
                    "label": item.get("title") or item_id,
                    "module": item.get("module"),
                    "status": (result or {}).get("status") or "pending",
                    "active": item_id in active,
                    "rerun_of": item.get("rerun_of_item_id"),
                    "fork_branch_id": item.get("fork_branch_id"),
                    "summary": (result or {}).get("summary"),
                })
            for item in items:
                for dep in item.get("dependencies") or []:
                    edges.append({"source": f"work:{dep}", "target": f"work:{item['item_id']}", "kind": "depends_on"})
        for row in artifacts:
            nodes.append({
                "id": f"artifact:{row['artifact_id']}",
                "kind": "artifact",
                "label": row.get("logical_name") or row['artifact_id'],
                "media_type": row.get("media_type"),
                "size_bytes": row.get("size_bytes"),
                "sha256_short": (row.get("sha256") or "")[:12],
            })
            if row.get("work_item_id"):
                edges.append({
                    "source": f"work:{row['work_item_id']}",
                    "target": f"artifact:{row['artifact_id']}",
                    "kind": "produces",
                })
        for branch in branches:
            nodes.append({
                "id": f"branch:{branch['branch_id']}",
                "kind": "branch",
                "label": branch["branch_id"],
                "mode": branch.get("mode"),
                "status": branch.get("status"),
            })
        return {
            "contract_version": snap.get("contract_version"),
            "project_id": project_id,
            "state_status": (snap.get("state") or {}).get("status"),
            "nodes": nodes,
            "edges": edges,
        }

    @staticmethod
    def _active_artifact_record(store: ResearchProjectStore, logical_name: str):
        records = [row for row in store.read_artifacts() if row.logical_name == logical_name]
        if not records:
            return None
        heads = {row.artifact_id for row in store.read_artifact_heads()}
        active = [row for row in records if row.artifact_id in heads]
        return (active or records)[-1]

    def _read_ndjson_artifact(
        self, store: ResearchProjectStore, logical_name: str, *, limit: int = 200,
        model=None,
    ) -> list[Any]:
        record = self._active_artifact_record(store, logical_name)
        if record is None:
            return []
        path = store.artifact_path(record)
        rows: list[Any] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        if model is not None:
                            row = model.model_validate(row)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    rows.append(row)
                    if len(rows) >= limit:
                        break
        except OSError:
            return []
        return rows

    def _read_json_artifact(
        self, store: ResearchProjectStore, logical_name: str, *, limit: int = 200,
    ) -> list[dict[str, Any]]:
        record = self._active_artifact_record(store, logical_name)
        if record is None:
            return []
        path = store.artifact_path(record)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = payload if isinstance(payload, list) else [payload]
        return rows[:limit]

    def mechanism_graph(self, project_id: str) -> dict[str, Any]:
        """Mechanistic evidence graph plus deterministic synthesis gates.

        The graph is projected only from durable child-run artifacts
        (evidence, claims, reviewer findings, ranked targets) and the plan's
        evidence-strategy patterns. No number is invented in the service or
        client layer.
        """
        store = self._existing_store(project_id)
        spec = store.load_spec()
        assert spec is not None
        state = store.load_state()
        plan = store.load_plan()
        task = None
        try:
            task = TaskSpec.model_validate(spec.context["target_task_spec"])
        except (KeyError, ValidationError, ValueError):
            task = None
        evidence = self._read_ndjson_artifact(
            store, "target_discovery_evidence", limit=400, model=EvidenceItem,
        )
        claims = self._read_ndjson_artifact(store, "target_discovery_claims", limit=200)
        reviewer_findings = self._read_ndjson_artifact(
            store, "target_discovery_reviewer_findings", limit=50,
        )
        ranked_rows = self._read_json_artifact(store, "ranked_targets", limit=200)
        genes = [
            str(row["gene"]).strip() for row in ranked_rows
            if isinstance(row, dict) and row.get("gene")
        ]
        if not genes:
            genes = sorted({
                str(getattr(row, "gene_symbol", "") or "").strip()
                for row in evidence
                if getattr(row, "gene_symbol", None)
            })
        patterns: list[StrategyPattern] = []
        paper_evidence: list[dict[str, Any]] = []
        if plan is not None:
            for raw in plan.evidence_strategy_patterns:
                try:
                    patterns.append(StrategyPattern.model_validate(raw))
                except ValidationError:
                    continue
            paper_evidence = list(plan.paper_evidence or [])
        status = state.status.value if state else ProjectStatus.DRAFT.value
        if task is None:
            return {
                "contract_version": RESEARCH_CONTRACT_VERSION,
                "project_id": project_id,
                "status": status,
                "available": False,
                "reason": "Project has no disease-target TaskSpec.",
                "graph": None,
                "synthesis_findings": [],
                "reviewer_findings": reviewer_findings,
                "lane_coverage": {},
                "pattern_links": [],
                "paper_links": [],
                "ranked_genes": genes,
            }
        synthesis = synthesize_evidence_graph(
            task, evidence, genes, patterns=patterns, paper_evidence=paper_evidence,
        )
        return {
            "contract_version": RESEARCH_CONTRACT_VERSION,
            "project_id": project_id,
            "status": status,
            "available": True,
            "graph": synthesis.graph.model_dump(mode="json"),
            "synthesis_findings": synthesis.findings,
            "reviewer_findings": reviewer_findings,
            "lane_coverage": synthesis.lane_coverage,
            "pattern_links": synthesis.pattern_links,
            "paper_links": synthesis.paper_links,
            "ranked_genes": genes,
            "claim_count": len(claims),
            "evidence_count": len(evidence),
            "pattern_count": len(patterns),
        }

    def project_files(self, project_id: str) -> dict[str, Any]:
        """List the durable project directory as a safe, relative file tree."""
        store = self._existing_store(project_id)
        try:
            store.assert_integrity()
        except ValueError as exc:
            raise ValueError(f"project integrity check failed: {exc}") from exc
        from .project_package import _SECRET_RE

        rows: list[dict[str, Any]] = []
        for path in sorted(store.project_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(store.project_dir).as_posix()
            if _SECRET_RE.search(rel):
                continue
            rows.append({
                "path": rel,
                "size_bytes": path.stat().st_size,
                "media_type": _guess_media_type(rel),
            })
        return {"contract_version": RESEARCH_CONTRACT_VERSION, "project_id": project_id, "files": rows}

    def preview_file(self, project_id: str, rel_path: str, max_characters: int = 200_000) -> dict[str, Any]:
        """Preview one textual project file with an explicit size bound."""
        if max_characters < 1 or max_characters > 1_000_000:
            raise ValueError("max_characters must be between 1 and 1000000")
        store = self._existing_store(project_id)
        from .project_package import _SECRET_RE

        if _SECRET_RE.search(rel_path):
            raise ValueError("refusing to preview a secret-like file")
        candidate = (store.project_dir / rel_path).resolve()
        if not candidate.is_relative_to(store.project_dir) or not candidate.is_file():
            raise ValueError("preview path escapes the project directory")
        if candidate.stat().st_size > 2_000_000:
            raise ValueError("file is too large to preview")
        media = _guess_media_type(rel_path)
        textual = media.startswith("text/") or media in {"application/json", "application/x-ndjson"}
        if not textual:
            return {"path": rel_path, "media_type": media, "content": None, "reason": "binary_file"}
        content = candidate.read_text(encoding="utf-8")
        return {
            "path": rel_path,
            "media_type": media,
            "content": content[:max_characters],
            "truncated": len(content) > max_characters,
        }

    def read_text_artifact(
        self, project_id: str, artifact_id: str, max_characters: int = 100_000,
    ) -> dict[str, Any]:
        """Read a verified text artifact with an explicit context-size bound."""
        if max_characters < 1 or max_characters > 1_000_000:
            raise ValueError("max_characters must be between 1 and 1000000")
        store = self._existing_store(project_id)
        record = next((row for row in store.read_artifacts() if row.artifact_id == artifact_id), None)
        if record is None:
            raise ResearchProjectNotFound(f"artifact not found: {artifact_id}")
        store.assert_integrity()
        path = store.artifact_path(record)
        textual = record.media_type.startswith("text/") or record.media_type in {
            "application/json",
            "application/x-ndjson",
        }
        payload: dict[str, Any] = {"artifact": record.model_dump(mode="json")}
        if not textual:
            return {**payload, "content": None, "truncated": False, "reason": "binary_artifact"}
        content = path.read_text(encoding="utf-8")
        return {
            **payload,
            "content": content[:max_characters],
            "truncated": len(content) > max_characters,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "research_contract_version": RESEARCH_CONTRACT_VERSION,
            "domain": "disease_target_discovery",
            "operations": [
                "create_disease_project",
                "run_project",
                "get_project",
                "list_projects",
                "get_events",
                "get_domain_activities",
                "get_repairs",
                "decide_repair",
                "propose_fork",
                "decide_fork",
                "get_branches",
                "accept_checkpoint",
                "read_text_artifact",
            ],
            "autonomy_modes": [mode.value for mode in AutonomyMode],
            "module_capabilities": self.runtime.registry.public_capabilities(),
            "skills": self.runtime.skill_catalog.public_summary(),
            "boundaries": [
                "No arbitrary model-generated code or shell execution is exposed by this service.",
                "Missing biological context remains missing and may require user input.",
                "MCP/HTTP clients receive durable projections, not filesystem deployment paths.",
                "Automatic repair is limited to source-bound same-input reruns of replay-safe transient failures.",
                "User forks are immutable, snapshot-bound rollbacks; restore mode always requires human approval.",
            ],
        }

    def _next_actions(self, store: ResearchProjectStore) -> list[dict[str, Any]]:
        spec, state, plan = store.load_spec(), store.load_state(), store.load_plan()
        if spec is None:
            return []
        if state is None:
            return [{"action": "run_project", "project_id": spec.project_id}]
        if state.checkpoint_kind == "plan":
            return [{
                "action": "accept_checkpoint", "target_id": state.checkpoint_target_id,
                "reason": "The frozen plan requires human acceptance.",
            }]
        if state.checkpoint_kind == "work_item":
            return [{
                "action": "accept_checkpoint", "target_id": state.checkpoint_target_id,
                "reason": "The supervised work item requires human acceptance or additional input.",
            }]
        if state.checkpoint_kind == "repair":
            request = next(
                row for row in store.read_repair_requests()
                if row.repair_request_id == state.checkpoint_target_id
            )
            decided = any(
                request.repair_request_id in decision.target_ids
                and decision.action in {DecisionAction.ACCEPT, DecisionAction.REJECT}
                for decision in store.read_decisions()
            )
            if decided:
                return [{
                    "action": "run_project", "project_id": spec.project_id,
                    "reason": "A durable repair decision must be reconciled before release review.",
                }]
            return [{
                "action": "decide_repair",
                "repair_request_id": request.repair_request_id,
                "trigger_snapshot_digest": request.trigger_snapshot_digest,
                "risk": request.risk.value,
                "affected_work_item_ids": request.affected_work_item_ids,
                "reason": request.rationale,
            }]
        if state.checkpoint_kind == "fork":
            branch = store.current_branch(state.checkpoint_target_id)
            if branch is None:
                return [{
                    "action": "run_project", "project_id": spec.project_id,
                    "reason": "The fork checkpoint target is missing; reconcile the project.",
                }]
            decided = any(
                branch.branch_id in decision.target_ids
                and decision.action in {DecisionAction.ACCEPT, DecisionAction.REJECT}
                and decision.evidence_snapshot_digest == branch.before_snapshot_digest
                for decision in store.read_decisions()
            )
            if decided:
                return [{
                    "action": "run_project", "project_id": spec.project_id,
                    "reason": "A durable fork decision must be reconciled before execution.",
                }]
            directive = next(
                (row for row in store.read_fork_directives()
                 if row.fork_directive_id == branch.fork_directive_id),
                None,
            )
            return [{
                "action": "decide_fork",
                "branch_id": branch.branch_id,
                "snapshot_digest": branch.before_snapshot_digest,
                "mode": branch.mode.value,
                "target_work_item_id": branch.fork_point_item_id,
                "reason": directive.rationale if directive else "Fork approval is required.",
            }]
        if state.checkpoint_kind == "release":
            return [{
                "action": "accept_checkpoint",
                "target_id": state.checkpoint_target_id,
                "evidence_snapshot_digest": state.checkpoint_snapshot_digest,
                "reason": "The exact release artifact set requires human acceptance.",
            }]
        accepted = {
            target_id
            for row in store.read_decisions()
            if row.action == DecisionAction.ACCEPT
            for target_id in row.target_ids
        }
        if plan is not None and plan.plan_id not in accepted and state.status == ProjectStatus.NEEDS_INPUT:
            return [{
                "action": "accept_checkpoint",
                "target_id": plan.plan_id,
                "reason": "The frozen plan requires human acceptance.",
            }]
        if state.status == ProjectStatus.NEEDS_INPUT and state.current_item_id:
            return [{
                "action": "accept_checkpoint",
                "target_id": state.current_item_id,
                "reason": "The supervised work item requires human acceptance or additional input.",
            }]
        pending_repairs = [
            row for row in store.read_repair_requests()
            if not any(row.repair_request_id == revision.repair_request_id
                       for revision in store.read_plan_revisions())
            and not any(row.repair_request_id in decision.target_ids
                        and decision.action in {DecisionAction.ACCEPT, DecisionAction.REJECT}
                        for decision in store.read_decisions())
        ]
        if state.status == ProjectStatus.PAUSED:
            return [{
                "action": "resume_project", "project_id": spec.project_id,
                "reason": state.terminal_reason or "The project is paused; resume to continue.",
            }]
        if state.status == ProjectStatus.WAITING_REVIEW and pending_repairs:
            request = pending_repairs[0]
            return [{
                "action": "decide_repair",
                "repair_request_id": request.repair_request_id,
                "trigger_snapshot_digest": request.trigger_snapshot_digest,
                "risk": request.risk.value,
                "affected_work_item_ids": request.affected_work_item_ids,
                "reason": request.rationale,
            }]
        pending_forks = [
            row for row in store.read_branches()
            if row.revision_id is None and row.status == PlanBranchStatus.PROPOSED
        ]
        if state.status == ProjectStatus.WAITING_REVIEW and pending_forks:
            branch = pending_forks[0]
            directive = next(
                (row for row in store.read_fork_directives()
                 if row.fork_directive_id == branch.fork_directive_id),
                None,
            )
            return [{
                "action": "decide_fork",
                "branch_id": branch.branch_id,
                "snapshot_digest": branch.before_snapshot_digest,
                "mode": branch.mode.value,
                "target_work_item_id": branch.fork_point_item_id,
                "reason": directive.rationale if directive else "Fork approval is required.",
            }]
        decided_unapplied_repairs = [
            request for request in store.read_repair_requests()
            if not any(request.repair_request_id == revision.repair_request_id
                       for revision in store.read_plan_revisions())
            and any(request.repair_request_id in decision.target_ids
                    and decision.action in {DecisionAction.ACCEPT, DecisionAction.REJECT}
                    for decision in store.read_decisions())
        ]
        if state.status == ProjectStatus.WAITING_REVIEW and decided_unapplied_repairs:
            return [{
                "action": "run_project",
                "project_id": spec.project_id,
                "reason": "A durable repair decision must be reconciled before release review.",
            }]
        if state.status == ProjectStatus.WAITING_REVIEW and plan is not None:
            effective = effective_plan(plan, store.read_plan_revisions())
            release_snapshot = self._release_snapshot(store, effective)
            return [{
                "action": "accept_checkpoint",
                "target_id": f"release:{release_snapshot}",
                "evidence_snapshot_digest": release_snapshot,
                "reason": "The exact release artifact set requires human acceptance.",
            }]
        if state.status == ProjectStatus.COMPLETED_WITH_GAPS:
            gaps = [
                {"item_id": row.item_id, "limitations": row.limitations}
                for row in store.load_work_item_results().values()
                if row.limitations
            ]
            return [{
                "action": "inspect_gaps",
                "reason": state.terminal_reason,
                "work_items": gaps,
            }]
        if state.status in _TERMINAL_PROJECT_STATUSES:
            return [{"action": "inspect_artifacts", "project_id": spec.project_id}]
        return [{"action": "run_project", "project_id": spec.project_id}]

    @staticmethod
    def _release_snapshot(store: ResearchProjectStore, plan) -> str:
        return project_snapshot_digest(
            plan=plan,
            results=store.load_work_item_results(),
            assessments=store.read_assessments(),
            artifacts=store.read_artifacts(),
            revisions=store.read_plan_revisions(),
        )

    def _store(self, project_id: str) -> ResearchProjectStore:
        return ResearchProjectStore(self.projects_dir, project_id)

    def _existing_store(self, project_id: str) -> ResearchProjectStore:
        store = self._store(project_id)
        if store.load_spec() is None:
            raise ResearchProjectNotFound(f"project not found: {project_id}")
        return store


__all__ = [
    "ResearchDecisionError",
    "ResearchProjectNotFound",
    "ResearchProjectService",
]


def _guess_media_type(rel_path: str) -> str:
    suffix = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""
    return {
        "md": "text/markdown",
        "markdown": "text/markdown",
        "txt": "text/plain",
        "json": "application/json",
        "jsonl": "application/x-ndjson",
        "csv": "text/csv",
        "tsv": "text/tab-separated-values",
        "yaml": "text/yaml",
        "yml": "text/yaml",
        "html": "text/html",
        "py": "text/x-python",
        "r": "text/x-r",
        "sh": "text/x-shellscript",
    }.get(suffix, "application/octet-stream")
