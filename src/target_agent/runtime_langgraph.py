"""LangGraph-based runtime: same pipeline semantics as runtime.py, expressed as a StateGraph.

Migration notes
---------------
The original :class:`~target_agent.runtime.TargetDiscoveryRuntime` is a hand-rolled
state machine. This module replicates its exact node semantics on LangGraph so that:

- the graph topology (intake -> plan -> tool loop -> integrity -> reviewer -> ranking -> report)
  is explicit, inspectable and extensible (new tools = new plan steps, no runtime edits);
- checkpoint/resume stays file-backed through ``EvidenceStore`` (identical contract:
  ``checkpoint.json`` keys, ``status.json`` shape, trace events);
- both runtimes are parity-tested on the same fake registry, so observable scientific
  outputs cannot silently diverge.

Artifacts written to the run directory are contract-compatible with the legacy runtime
(status.json, checkpoint.json, evidence_items.jsonl, tool_results.jsonl, trace.jsonl,
ranked_targets.json, target_cards.json, mechanistic_evidence_graph.json, report.*,
case_record.json).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .candidate_policy import formal_gwas_candidates, merge_candidates_for_task
from .cards import build_cards
from .contracts import (
    CONTRACT_VERSION, CaseRecord, Claim, ClaimClass, EvidenceItem, ExecutionPlan,
    PlanStep, ReviewerFinding, TaskSpec, TerminalStatus, ToolResult, TraceEvent, new_id,
)


from .graphs import build_mechanistic_graph
from .ablations import from_env as ablation_switches
from .evidence_tiers import concordance_matrix, format_report_section, load_tiers
from .llm import StepClient
from .legacy import migrate_current_contract
from .paper_strategy import pattern_store_from_path
from .paper_rag import paper_rag_store_from_path
from .planner import Planner
from .ranking import RankedTarget, rank_targets
from .repair import latest_tool_results, repair_transient_connector_failures
from .reporting import build_disease_report, build_mch_report, write_report
from .resume import (
    load_validated_terminal_status, require_current_contract_for_resume,
    restore_checkpoint_state,
)
from .reviewer import Reviewer
from .settings import Settings, load_settings
from .store import EvidenceStore
from .tools import ToolRegistry, default_registry
from .tools.base import ToolContext, execute_tool_safely


CANDIDATE_BOUND_OUTPUT_KEY = "_candidate_bound"


def candidate_universe_digest(genes: list[str]) -> str:
    """Stable digest of the current candidate universe for typed evidence steps.

    Includes the contract version so a contract change invalidates every
    candidate-bound result instead of silently reusing stale evidence.
    """
    normalized = sorted({str(gene).upper() for gene in genes if gene})
    payload = CONTRACT_VERSION + "\x00" + "\n".join(normalized)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PipelineState(TypedDict, total=False):
    """Mutable state threaded through the LangGraph nodes.

    Objects are passed by reference (in-memory graph, no serializer); all durable
    state is mirrored to disk through ``EvidenceStore`` exactly like the legacy runtime.
    """

    task: TaskSpec
    run_id: str
    resume: bool
    store: EvidenceStore
    checkpoint: dict[str, Any] | None
    early_terminal: bool
    plan: ExecutionPlan
    ordered_steps: list[PlanStep]
    completed_steps: set[str]
    candidate_genes: list[str]
    candidate_digest: str
    tool_calls: int
    prior_results: list[ToolResult]
    prior_evidence: list[EvidenceItem]
    revision_history: list[dict[str, Any]]
    tool_loop_done: bool
    results: list[ToolResult]
    evidence: list[EvidenceItem]
    findings: list[ReviewerFinding]
    execution_findings: list[ReviewerFinding]
    status: TerminalStatus
    ranked_payload: list[dict[str, Any]]
    cards: list[Any]
    final_claims: list[Claim]


class LangGraphRuntime:
    """Drop-in replacement for ``TargetDiscoveryRuntime`` with a LangGraph topology."""

    def __init__(
        self,
        runs_dir: Path | None = None,
        cache_dir: Path | None = None,
        registry: ToolRegistry | None = None,
        planner: Planner | None = None,
        settings: Settings | None = None,
        trace_observer: Callable[[TraceEvent], None] | None = None,
    ):
        self.settings = settings or load_settings()
        self.runs_dir = runs_dir or self.settings.runs_dir
        self.cache_dir = cache_dir or self.settings.cache_dir
        self.registry = registry or default_registry(self.settings)
        if planner is None:
            planner = Planner(
                StepClient.from_settings(self.settings),
                self.registry,
                pattern_store=pattern_store_from_path(self.settings.pattern_store_path),
                few_shot_top_k=self.settings.pattern_few_shot_top_k,
                paper_rag=paper_rag_store_from_path(self.settings.paper_rag_path),
                paper_top_k=self.settings.paper_rag_top_k,
            )
        self.planner = planner
        self.trace_observer = trace_observer
        self.trace_observer_errors: list[str] = []
        if self.planner.registry is None:
            self.planner.registry = self.registry
        self.reviewer = Reviewer(getattr(self.planner, "client", None), settings=self.settings, cache_dir=self.cache_dir)
        self._graph = self._build_graph()

    # ------------------------------------------------------------------ tracing
    def _trace(self, store: EvidenceStore, run_id: str, task: TaskSpec, event_type: str, state: str,
               detail: dict[str, Any] | None = None, related_ids: list[str] | None = None) -> None:
        event = TraceEvent(
            run_id=run_id, task_id=task.task_id, event_type=event_type, state=state,
            detail=detail or {}, related_ids=related_ids or [],
        )
        store.add_trace(event)
        if self.trace_observer is not None:
            try:
                self.trace_observer(event)
            except Exception as exc:
                # Observability cannot change the authoritative scientific run.
                self.trace_observer_errors.append(exc.__class__.__name__)

    @staticmethod
    def _status(store: EvidenceStore, run_id: str, task: TaskSpec, state: str,
                terminal: TerminalStatus | None = None, detail: dict[str, Any] | None = None) -> None:
        store.save_json("status.json", {
            "contract_version": CONTRACT_VERSION, "run_id": run_id, "task_id": task.task_id,
            "state": state, "terminal_status": terminal.value if terminal else None, "detail": detail or {},
        })

    # ------------------------------------------------------------------ graph
    def _build_graph(self):
        graph = StateGraph(PipelineState)
        graph.add_node("intake", self._node_intake)
        graph.add_node("plan", self._node_plan)
        graph.add_node("tool_step", self._node_tool_step)
        graph.add_node("integrity", self._node_integrity)
        graph.add_node("reviewer", self._node_reviewer)
        graph.add_node("ranking", self._node_ranking)
        graph.add_node("report", self._node_report)

        graph.add_edge(START, "intake")
        graph.add_conditional_edges("intake", lambda s: "terminal" if s["early_terminal"] else "plan",
                                    {"terminal": END, "plan": "plan"})
        graph.add_edge("plan", "tool_step")
        graph.add_conditional_edges("tool_step", lambda s: "integrity" if s["tool_loop_done"] else "tool_step",
                                    {"integrity": "integrity", "tool_step": "tool_step"})
        graph.add_edge("integrity", "reviewer")
        graph.add_edge("reviewer", "ranking")
        graph.add_edge("ranking", "report")
        graph.add_edge("report", END)
        return graph.compile()

    # ------------------------------------------------------------------ nodes
    def _node_intake(self, state: PipelineState) -> dict[str, Any]:
        task, run_id, resume = state["task"], state["run_id"], state["resume"]
        store = EvidenceStore(self.runs_dir / run_id)
        stored_task = None
        if resume:
            stored_task = store.load_task()
            if stored_task is None and store.has_durable_run_artifacts():
                raise ValueError("missing provenance: durable run artifacts exist without task_spec.json")
            if stored_task is not None:
                identity_fields = {"task_id", "created_at"}
                incoming_payload = task.model_dump(mode="json", exclude=identity_fields)
                stored_payload = stored_task.model_dump(mode="json", exclude=identity_fields)
                if incoming_payload != stored_payload:
                    raise ValueError("resume input does not match the stored task specification")
                task = stored_task
        checkpoint = store.load_checkpoint() if resume else None
        if resume and stored_task is not None and checkpoint is None and store.has_durable_run_artifacts():
            raise ValueError("missing provenance: durable run artifacts exist without checkpoint.json")
        if resume and stored_task is not None:
            require_current_contract_for_resume(store.task_contract_version(), checkpoint)
        if checkpoint and checkpoint.get("terminal_status"):
            load_validated_terminal_status(
                run_dir=store.run_dir, run_id=run_id, task_id=task.task_id,
                source_contract_version=store.task_contract_version(), checkpoint=checkpoint,
            )
            return {"task": task, "store": store, "checkpoint": checkpoint, "early_terminal": True}
        store.save_task(task)
        self._status(store, run_id, task, "intake")
        self._trace(store, run_id, task, "state_transition", "intake", {"resume": resume})
        return {
            "task": task, "store": store, "checkpoint": checkpoint, "early_terminal": False,
            "completed_steps": set(),
            "candidate_genes": list(task.candidate_genes),
            "candidate_digest": candidate_universe_digest(list(task.candidate_genes)),
            "tool_calls": 0,
            "prior_results": store.tool_results() if checkpoint else [],
            "prior_evidence": store.evidences() if checkpoint else [],
            "revision_history": list((checkpoint or {}).get("revision_history", [])),
            "execution_findings": [],
            "tool_loop_done": False,
        }

    def _node_plan(self, state: PipelineState) -> dict[str, Any]:
        task, store, checkpoint = state["task"], state["store"], state["checkpoint"]
        plan_path = store.run_dir / "execution_plan.json"
        if checkpoint and plan_path.exists():
            plan = ExecutionPlan.model_validate(migrate_current_contract(
                json.loads(plan_path.read_text(encoding="utf-8")),
            ))
            self.planner._validate(
                task,
                plan,
                enforce_tool_budget=not plan.planner_backend.startswith("deterministic:"),
            )
        else:
            plan = self.planner.create_plan(task)
            store.save_plan(plan)
            hints = getattr(self.planner, "last_pattern_hints", None) or []
            if hints:
                pattern_ids = [str(hint.get("pattern_id")) for hint in hints if hint.get("pattern_id")]
                self._trace(store, state["run_id"], task, "planner_pattern_hints", "planned", {
                    "count": len(hints),
                    "pattern_ids": pattern_ids,
                }, related_ids=pattern_ids)
            paper_evidence = getattr(self.planner, "last_paper_evidence", None) or []
            if paper_evidence:
                chunk_ids = [str(row.get("chunk_id")) for row in paper_evidence if row.get("chunk_id")]
                pmids = sorted({str(row.get("pmid")) for row in paper_evidence if row.get("pmid")})
                self._trace(store, state["run_id"], task, "planner_paper_evidence", "planned", {
                    "count": len(paper_evidence),
                    "chunk_ids": chunk_ids,
                    "pmids": pmids,
                }, related_ids=chunk_ids)
        completed_steps, candidate_genes, tool_calls = restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint=checkpoint,
            stored_results=state["prior_results"],
            stored_evidence=state["prior_evidence"],
            merge_candidates=lambda current, result, limit: merge_candidates_for_task(
                task, current, result, limit, self._merge_candidates,
                state["prior_results"], state["prior_evidence"],
                task.constraints.genetics.minimum_coloc_pp4,
            ),
        )
        stale_step_ids = self._stale_candidate_bound_step_ids(
            plan, completed_steps, candidate_genes, state["prior_results"],
        )
        if stale_step_ids:
            completed_steps = completed_steps - stale_step_ids
            self._trace(store, state["run_id"], task, "replan", "resume", {
                "action": "stale_candidate_bound_steps_removed",
                "steps": sorted(stale_step_ids),
                "candidate_digest": candidate_universe_digest(candidate_genes),
            }, sorted(stale_step_ids))
        planner_meta = getattr(self.planner.client, "last_request_meta", None) if self.planner.client else None
        self._trace(store, state["run_id"], task, "plan", "planner", {
            "planner_backend": plan.planner_backend, "fallback_used": plan.fallback_used,
            "steps": [step.step_id for step in plan.steps], "provider_request": planner_meta,
        }, [plan.plan_id])
        return {
            "plan": plan,
            "ordered_steps": self._ordered_steps(plan),
            "completed_steps": completed_steps,
            "candidate_genes": candidate_genes,
            "candidate_digest": candidate_universe_digest(candidate_genes),
            "tool_calls": tool_calls,
        }

    def _node_tool_step(self, state: PipelineState) -> dict[str, Any]:
        task, run_id, store = state["task"], state["run_id"], state["store"]
        completed_steps = set(state["completed_steps"])
        candidate_genes = list(state["candidate_genes"])
        tool_calls = state["tool_calls"]
        prior_results = list(state["prior_results"])
        prior_evidence = list(state["prior_evidence"])
        revision_history = list(state["revision_history"])
        self._status(store, run_id, task, "tool_execution")

        for step in state["ordered_steps"]:
            if step.step_id in completed_steps:
                continue
            unmet = [dependency for dependency in step.dependencies if dependency not in completed_steps]
            if unmet:
                raise ValueError(f"step {step.step_id} has unmet dependencies: {unmet}")
            if not step.tool:
                completed_steps.add(step.step_id)
                continue
            if tool_calls >= task.constraints.max_tool_calls:
                self._trace(store, run_id, task, "degradation", "tool_execution",
                            {"reason": "tool_call_budget_exhausted"})
                message = (
                    f"Tool-call budget exhausted before planned step {step.step_id}; "
                    "remaining workflow coverage is incomplete."
                )
                finding = next((
                    item for item in store.findings()
                    if item.category == "coverage_gap" and item.message == message and not item.resolved
                ), None)
                if finding is None:
                    finding = ReviewerFinding(
                        severity="major",
                        category="coverage_gap",
                        message=message,
                        related_ids=[step.step_id],
                        required_action=(
                            "Increase max_tool_calls or narrow the task, then rerun the omitted plan steps."
                        ),
                    )
                    store.add_finding(finding)
                return {"completed_steps": completed_steps, "candidate_genes": candidate_genes, "candidate_digest": candidate_universe_digest(candidate_genes),
                        "tool_calls": tool_calls, "prior_results": prior_results,
                        "prior_evidence": prior_evidence,
                        "revision_history": revision_history, "execution_findings": [finding],
                        "tool_loop_done": True}
            candidate_bound_digest = (
                candidate_universe_digest(candidate_genes) if step.candidate_bound else None
            )
            tool = self.registry.get(step.tool)
            self._trace(store, run_id, task, "tool_call", "tool_execution",
                        {"tool": step.tool, "step_id": step.step_id,
                         "candidate_digest": candidate_bound_digest})
            execution = execute_tool_safely(tool, ToolContext(
                task=task, run_dir=store.run_dir, cache_dir=self.cache_dir, candidate_genes=candidate_genes,
                prior_results=prior_results, settings=self.settings,
            ))
            if candidate_bound_digest is not None:
                stale = self._active_candidate_bound_result(prior_results, step.step_id, None)
                execution.result.outputs[CANDIDATE_BOUND_OUTPUT_KEY] = {
                    "step_id": step.step_id,
                    "candidate_digest": candidate_bound_digest,
                    "evidence_lane": step.evidence_lane,
                }
                if stale is not None and stale.tool_run_id != execution.result.tool_run_id:
                    execution.result.supersedes_tool_run_id = stale.tool_run_id
                    self._trace(store, run_id, task, "evidence_superseded", "tool_execution", {
                        "tool": step.tool, "step_id": step.step_id,
                        "candidate_digest": candidate_bound_digest,
                        "evidence_lane": step.evidence_lane,
                        "supersedes_tool_run_id": stale.tool_run_id,
                        "reason": "candidate universe changed for candidate-bound evidence lane",
                    }, [stale.tool_run_id, execution.result.tool_run_id])
            tool_calls += 1
            for item in execution.evidence:
                store.add_evidence(item)
                prior_evidence.append(item)
            store.add_tool_result(execution.result)
            prior_results.append(execution.result)
            candidate_genes = merge_candidates_for_task(
                task,
                candidate_genes,
                execution.result,
                task.constraints.max_initial_candidates,
                self._merge_candidates,
                prior_results,
                prior_evidence,
                task.constraints.genetics.minimum_coloc_pp4,
            )
            completed_steps.add(step.step_id)
            self._trace(store, run_id, task, "tool_result", "tool_execution", {
                "tool": step.tool, "status": execution.result.status.value,
                "coverage_status": execution.result.coverage_status.value,
                "context_match_score": execution.result.context_match_score,
                "candidate_genes_emitted": len(execution.result.candidate_genes),
            }, [execution.result.tool_run_id, *execution.result.evidence_ids])
            if execution.result.outputs.get("retry_performed"):
                action = {
                    "round": min(task.constraints.max_review_rounds, 1),
                    "action": "rejected ineligible GEO candidates and selected the next eligible dataset",
                    "selection_trace": execution.result.outputs.get("selection_trace", []),
                }
                revision_history.append(action)
                self._trace(store, run_id, task, "replan", "tool_execution", action,
                            [execution.result.tool_run_id])
            checkpoint = {
                "stage": "tool_execution", "completed_steps": sorted(completed_steps),
                "candidate_genes": candidate_genes,
                "candidate_digest": candidate_universe_digest(candidate_genes),
                "tool_calls": tool_calls,
                "revision_history": revision_history,
            }
            store.checkpoint(checkpoint)
            self._trace(store, run_id, task, "checkpoint", "tool_execution", checkpoint)
            if execution.result.status.value == "out_of_scope" and step.tool == "mch_causal_gold":
                return {"completed_steps": completed_steps, "candidate_genes": candidate_genes, "candidate_digest": candidate_universe_digest(candidate_genes),
                        "tool_calls": tool_calls, "prior_results": prior_results,
                        "prior_evidence": prior_evidence,
                        "revision_history": revision_history, "tool_loop_done": True}
            # one tool execution per node invocation; loop continues via conditional edge
            return {"completed_steps": completed_steps, "candidate_genes": candidate_genes, "candidate_digest": candidate_universe_digest(candidate_genes),
                    "tool_calls": tool_calls, "prior_results": prior_results,
                    "prior_evidence": prior_evidence,
                    "revision_history": revision_history, "tool_loop_done": False}
        # no further steps to execute
        return {"completed_steps": completed_steps, "candidate_genes": candidate_genes, "candidate_digest": candidate_universe_digest(candidate_genes),
                "tool_calls": tool_calls, "prior_results": prior_results,
                "prior_evidence": prior_evidence,
                "revision_history": revision_history, "tool_loop_done": True}

    def _node_integrity(self, state: PipelineState) -> dict[str, Any]:
        store = state["store"]
        results = store.tool_results()
        evidence = store.evidences()
        store.assert_referential_integrity()
        superseded_ids = {
            result.supersedes_tool_run_id
            for result in results
            if result.supersedes_tool_run_id is not None
        }
        active_results = [result for result in results if result.tool_run_id not in superseded_ids]
        active_ids = {result.tool_run_id for result in active_results}
        active_evidence = [item for item in evidence if item.tool_run_id in active_ids]
        self._status(store, state["run_id"], state["task"], "reviewer")
        return {"results": active_results, "evidence": active_evidence}

    def _node_reviewer(self, state: PipelineState) -> dict[str, Any]:
        task, store = state["task"], state["store"]
        reviewer_findings = self.reviewer.review(task, state["results"], state["evidence"])
        for finding in reviewer_findings:
            store.add_finding(finding)
        findings = [*reviewer_findings, *state.get("execution_findings", [])]
        self._trace(store, state["run_id"], task, "review", "reviewer", {
            "round": 1, "blocking": sum(f.severity == "blocking" for f in findings),
            "major": sum(f.severity == "major" for f in findings),
            "minor": sum(f.severity == "minor" for f in findings),
            "reviewer_backend": self.reviewer.last_backend,
        }, [f.finding_id for f in findings])
        revision_history = list(state["revision_history"])
        repaired = repair_transient_connector_failures(
            task=task, run_id=state["run_id"], store=store, registry=self.registry,
            reviewer=self.reviewer, cache_dir=self.cache_dir, settings=self.settings,
            results=state["results"], evidence=state["evidence"], findings=findings,
            candidate_genes=state["candidate_genes"], tool_calls=state["tool_calls"],
            merge_candidates=lambda current, result, limit: merge_candidates_for_task(
                task, current, result, limit, self._merge_candidates,
                store.tool_results(), store.evidences(),
                task.constraints.genetics.minimum_coloc_pp4,
            ),
            trace=lambda event_type, phase, detail, related: self._trace(
                store, state["run_id"], task, event_type, phase, detail, related,
            ),
        )
        revision_history.extend(repaired.actions)
        if repaired.actions:
            checkpoint = {
                "stage": "reviewer_repair", "completed_steps": sorted(state["completed_steps"]),
                "candidate_genes": repaired.candidate_genes, "tool_calls": repaired.tool_calls,
                "revision_history": revision_history,
            }
            store.checkpoint(checkpoint)
            self._trace(store, state["run_id"], task, "checkpoint", "reviewer_repair", checkpoint)
        elif any(f.severity in {"blocking", "major"} and not f.resolved for f in findings):
            action = {"round": 2, "action": "retain unresolved external evidence gaps; no fabricated repair"}
            revision_history.append(action)
            self._trace(store, state["run_id"], task, "replan", "reviewer", action)
        return {
            "findings": repaired.findings, "revision_history": revision_history,
            "results": repaired.results, "evidence": repaired.evidence,
            "candidate_genes": repaired.candidate_genes,
            "candidate_digest": candidate_universe_digest(repaired.candidate_genes),
            "tool_calls": repaired.tool_calls,
        }

    def _node_ranking(self, state: PipelineState) -> dict[str, Any]:
        task, store = state["task"], state["store"]
        status = self._terminal_status(
            task, state["findings"], state["results"], state["evidence"],
        )
        ranked_payload: list[dict[str, Any]] = []
        cards: list[Any] = []
        final_claims: list[Claim] = []
        candidate_genes = state["candidate_genes"]
        gwas_candidates = formal_gwas_candidates(
            latest_tool_results(state["results"]),
            state["evidence"],
            task.constraints.genetics.minimum_coloc_pp4,
            task.constraints.max_initial_candidates,
        )
        if task.task_type == "gwas_locus_to_target":
            candidate_genes = gwas_candidates
        should_rank = task.task_type == "disease_to_target" or (
            task.task_type == "gwas_locus_to_target" and bool(gwas_candidates)
        )
        if should_rank and candidate_genes:
            ranked = rank_targets(
                candidate_genes, state["evidence"], state["results"], state["findings"],
                minimum_coloc_pp4=task.constraints.genetics.minimum_coloc_pp4,
                task_context=task.context,
                terminal_status=status,
                ablations=ablation_switches(),
            )
            ranked_payload = self._serialize_ranked(ranked, task.constraints.max_ranked_targets)
            cards = build_cards(task, ranked, terminal_status=status)
            store.save_json("ranked_targets.json", ranked_payload)
            store.save_cards(cards)
            graph = build_mechanistic_graph(task, state["evidence"], [row["gene"] for row in ranked_payload])
            store.save_json("mechanistic_evidence_graph.json", graph)
            for row in ranked_payload:
                if row["evidence_ids"]:
                    claim = Claim(
                        claim_class=ClaimClass.INFERRED,
                        statement=f"{row['gene']} is ranked {row['rank']} for prioritization; this is not a success probability.",
                        evidence_ids=row["evidence_ids"],
                        synthesis_rationale="Transparent six-dimensional ranking with context multipliers and independent blocker retention.",
                    )
                    store.add_claim(claim)
                    final_claims.append(claim)
            self._trace(store, state["run_id"], task, "ranking", "ranking", {
                "ranked_targets": len(ranked_payload), "target_cards": len(cards),
                "highlighted_targets": [row["gene"] for row in ranked_payload[:3]],
            }, [card.target_card_id for card in cards])
        return {
            "status": status,
            "ranked_payload": ranked_payload,
            "cards": cards,
            "final_claims": final_claims,
            "candidate_genes": candidate_genes, "candidate_digest": candidate_universe_digest(candidate_genes),
        }

    def _node_report(self, state: PipelineState) -> dict[str, Any]:
        task, store, status = state["task"], state["store"], state["status"]
        results = state["results"]
        if task.task_type in {"disease_to_target", "gwas_locus_to_target"}:
            policy = load_tiers()
            tier_genes = [row["gene"] for row in state["ranked_payload"]]
            tier_matrix = concordance_matrix(tier_genes, state["evidence"], results, policy)
            if tier_matrix:
                store.save_json("evidence_tier_concordance.json", tier_matrix)
            report_payload, markdown = build_disease_report(
                task, status, state["ranked_payload"], state["cards"], state["findings"], results,
                tier_section=format_report_section(tier_matrix, tier_genes, policy) if tier_matrix else None)
        else:
            mch_result = next((result for result in results if result.tool_name == "mch_causal_gold"), None)
            report_payload, markdown = build_mch_report(task, status, mch_result, state["findings"])
            if mch_result and mch_result.outputs.get("graph"):
                store.save_json("causal_graph.json", mch_result.outputs["graph"])
        write_report(store.run_dir, report_payload, markdown)
        self._trace(store, state["run_id"], task, "report", "report",
                    {"status": status.value}, [claim.claim_id for claim in state["final_claims"]])
        case = CaseRecord(
            run_id=state["run_id"], task_spec=task, plan=state["plan"],
            tool_run_ids=[result.tool_run_id for result in results],
            finding_ids=[finding.finding_id for finding in state["findings"]],
            revision_history=state["revision_history"],
            final_status=status, final_claim_ids=[claim.claim_id for claim in state["final_claims"]],
            scientific_review="pending", promotion_eligible=False,
        )
        store.save_case(case)
        final_checkpoint = {
            "stage": "terminal", "completed_steps": sorted(state["completed_steps"]),
            "candidate_genes": state["candidate_genes"],
            "candidate_digest": candidate_universe_digest(state["candidate_genes"]),
            "tool_calls": state["tool_calls"],
            "terminal_status": status.value, "revision_history": state["revision_history"],
        }
        store.checkpoint(final_checkpoint)
        self._status(store, state["run_id"], task, "terminal", status, {
            "report": "report.md", "ranked_targets": len(state["ranked_payload"]),
            "target_cards": len(state["cards"]), "tool_calls": state["tool_calls"],
        })
        return {}

    # ------------------------------------------------------------------ helpers (identical to legacy runtime)
    @staticmethod
    def _serialize_ranked(ranked: list[RankedTarget], limit: int) -> list[dict[str, Any]]:
        return [
            {
                "rank": rank, "gene": item.gene, "scores": item.scores.model_dump(mode="json"),
                "decision": item.decision, "evidence_ids": item.evidence_ids,
                "supporting_ids": item.supporting_ids, "opposing_ids": item.opposing_ids,
                "safety_blockers": item.safety_blockers, "evidence_gaps": item.evidence_gaps,
                "matched_drugs": item.matched_drugs,
                "context_score_origin": item.context_score_origin,
                "context_score_notes": item.context_score_notes,
                "genetic_evidence_summary": [
                    row.model_dump(mode="json") for row in item.genetic_evidence_summary
                ],
            }
            for rank, item in enumerate(ranked[:limit], start=1)
        ]

    @staticmethod
    def _active_candidate_bound_result(
        results: list[ToolResult], step_id: str, digest: str | None,
    ) -> ToolResult | None:
        """Return the latest persisted result for one candidate-bound step.

        With a digest, only a result recorded for the same candidate universe
        counts as active. Without one, the latest attempt for the step is
        returned (used to detect a stale attempt to supersede).
        """
        matches: list[ToolResult] = []
        for result in results:
            meta = result.outputs.get(CANDIDATE_BOUND_OUTPUT_KEY)
            if isinstance(meta, dict) and meta.get("step_id") == step_id:
                matches.append(result)
        if not matches:
            return None
        if digest is None:
            return matches[-1]
        for result in reversed(matches):
            meta = result.outputs.get(CANDIDATE_BOUND_OUTPUT_KEY)
            if isinstance(meta, dict) and meta.get("candidate_digest") == digest:
                return result
        return None

    @staticmethod
    def _stale_candidate_bound_step_ids(
        plan: ExecutionPlan,
        completed_steps: set[str],
        candidate_genes: list[str],
        stored_results: list[ToolResult],
    ) -> set[str]:
        """Return completed candidate-bound steps whose persisted result was
        computed for a different candidate universe than the one rebuilt from
        the authoritative ToolResult ledger on resume."""
        digest = candidate_universe_digest(candidate_genes)
        stale: set[str] = set()
        for step in plan.steps:
            if not step.candidate_bound or step.step_id not in completed_steps:
                continue
            if LangGraphRuntime._active_candidate_bound_result(
                stored_results, step.step_id, digest,
            ) is None:
                stale.add(step.step_id)
        return stale

    @staticmethod
    def _ordered_steps(plan: ExecutionPlan) -> list[PlanStep]:
        by_id = {step.step_id: step for step in plan.steps}
        if len(by_id) != len(plan.steps):
            raise ValueError("execution plan contains duplicate step IDs")
        ordered: list[PlanStep] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step: PlanStep) -> None:
            if step.step_id in visiting:
                raise ValueError("execution plan contains a dependency cycle")
            if step.step_id in visited:
                return
            visiting.add(step.step_id)
            for dependency in step.dependencies:
                if dependency not in by_id:
                    raise ValueError(f"execution plan dependency does not exist: {dependency}")
                visit(by_id[dependency])
            visiting.remove(step.step_id)
            visited.add(step.step_id)
            ordered.append(step)

        for item in plan.steps:
            visit(item)
        return ordered

    @staticmethod
    def _merge_candidates(current: list[str], result: ToolResult, limit: int) -> list[str]:
        emitted = [str(gene).upper() for gene in result.candidate_genes if gene]
        if result.tool_name in {"omics_candidate_extraction", "genetics_candidate_extraction"}:
            if current and emitted:
                quota = max(1, limit // 2)
                base = [*emitted[:quota], *current[: limit - quota], *emitted[quota:], *current[limit - quota:]]
            else:
                base = [*emitted, *current]
        elif result.tool_name == "open_targets" and emitted:
            novel = [gene for gene in emitted if gene not in current][: max(1, limit // 4)]
            base = [*current[: limit - len(novel)], *novel, *current[limit - len(novel):]]
        else:
            base = [*current, *emitted]
        return list(dict.fromkeys(base))[:limit]

    @staticmethod
    def _terminal_status(
        task: TaskSpec,
        findings: list[ReviewerFinding],
        results: list[ToolResult],
        evidence: list[EvidenceItem] | None = None,
    ) -> TerminalStatus:
        results = latest_tool_results(results)
        unresolved = [finding for finding in findings if not finding.resolved]
        blocking = [finding for finding in unresolved if finding.severity == "blocking"]
        if any(finding.category == "missing_provenance" for finding in blocking):
            return TerminalStatus.FAILED
        if task.task_type == "trait_mechanism" and any(finding.category == "coverage_gap" for finding in blocking):
            return TerminalStatus.NEEDS_INPUT
        if task.task_type == "gwas_locus_to_target":
            input_qc_failures = [
                result for result in results
                if result.tool_name == "genetics_input_audit"
                and result.status.value == "failed"
                and result.outputs.get("covered") is False
                and bool(result.outputs.get("failed_assets"))
            ]
            input_qc_failure_ids = {result.tool_run_id for result in input_qc_failures}
            if any(
                result.status.value == "failed" and result.tool_run_id not in input_qc_failure_ids
                for result in results
            ):
                return TerminalStatus.COMPLETED_WITH_GAPS
            if input_qc_failures:
                return TerminalStatus.NEEDS_INPUT
            if not formal_gwas_candidates(
                results,
                evidence or [],
                task.constraints.genetics.minimum_coloc_pp4,
                task.constraints.max_initial_candidates,
            ):
                return TerminalStatus.NEEDS_INPUT
        if any(result.status.value == "failed" for result in results):
            return TerminalStatus.COMPLETED_WITH_GAPS
        if any(finding.severity in {"blocking", "major"} for finding in unresolved):
            return TerminalStatus.COMPLETED_WITH_GAPS
        return TerminalStatus.COMPLETED

    # ------------------------------------------------------------------ entry point
    def run(self, task: TaskSpec, run_id: str | None = None, resume: bool = False) -> dict[str, Any]:
        run_id = run_id or new_id("run")
        run_dir = self.runs_dir / run_id
        if run_dir.exists() and not resume:
            raise FileExistsError(f"run already exists: {run_id}; use resume=True")
        self._graph.invoke({"task": task, "run_id": run_id, "resume": resume})
        return json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
