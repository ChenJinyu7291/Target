"""Direct unit tests for the shared resume/restore validation module.

The runtime-level resume tests cover end-to-end recovery; these tests pin the
shared resume.py validators themselves so every durable-state failure mode has
a focused regression case.
"""
from __future__ import annotations

import json

import pytest

from target_agent.contracts import (
    ClaimClass,
    CONTRACT_VERSION,
    CoverageStatus,
    EvidenceContext,
    EvidenceItem,
    ExecutionPlan,
    PlanStep,
    SourceLocator,
    Stance,
    TaskConstraints,
    TaskContext,
    TaskSpec,
    ToolCapability,
    ToolResult,
    ToolStatus,
)
from target_agent.resume import (
    load_validated_terminal_status,
    require_current_contract_for_resume,
    restore_checkpoint_state,
)


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="task-resume-shared",
        task_type="disease_to_target",
        question="Find traceable disease targets",
        context=TaskContext(disease="ulcerative colitis", tissue="rectum"),
        constraints=TaskConstraints(max_initial_candidates=20),
        candidate_genes=[],
    )


def _plan(task: TaskSpec) -> ExecutionPlan:
    return ExecutionPlan(
        task_id=task.task_id,
        planner_backend="test",
        steps=[
            PlanStep(step_id="step-a", name="A", tool="tool_a"),
            PlanStep(step_id="step-b", name="B", tool="tool_b"),
        ],
    )


def _result(tool_name: str, tool_run_id: str, *, candidate_genes: list[str] | None = None) -> ToolResult:
    return ToolResult(
        tool_run_id=tool_run_id,
        tool_name=tool_name,
        tool_version="test",
        status=ToolStatus.SUCCESS,
        coverage_status=CoverageStatus.COVERED,
        context_match_score=1.0,
        candidate_genes=candidate_genes or [],
        capability=ToolCapability(validation_scope="resume shared test"),
    )


def _evidence(evidence_id: str, *, tool_run_id: str = "tool-a-1") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        tool_run_id=tool_run_id,
        gene_symbol="A",
        claim_class=ClaimClass.INFERRED,
        statement="Synthetic evidence for resume validation.",
        source=SourceLocator(uri="https://example.org/x", source_id="x", chunk_id="x-1"),
        source_span="span",
        context=EvidenceContext(disease="ulcerative colitis"),
        stance=Stance.SUPPORTS,
        uncertainty="test",
        context_match_score=1.0,
    )


def _merge(current, result, limit):
    return (current + list(result.candidate_genes))[:limit]


def test_require_current_contract_allows_current_version():
    require_current_contract_for_resume(CONTRACT_VERSION, {"completed_steps": []})


def test_require_current_contract_rejects_legacy_non_terminal():
    with pytest.raises(ValueError, match="legacy non-terminal runs cannot resume"):
        require_current_contract_for_resume("2.1.0", None)


def test_require_current_contract_allows_terminal_legacy_checkpoint():
    require_current_contract_for_resume("2.1.0", {"terminal_status": "completed"})


def test_load_validated_terminal_status_missing_file(tmp_path):
    with pytest.raises(ValueError, match="missing provenance"):
        load_validated_terminal_status(
            run_dir=tmp_path,
            run_id="run-x",
            task_id="task-x",
            source_contract_version=CONTRACT_VERSION,
            checkpoint={"terminal_status": "completed_with_gaps"},
        )


def test_load_validated_terminal_status_requires_object(tmp_path):
    (tmp_path / "status.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        load_validated_terminal_status(
            run_dir=tmp_path,
            run_id="run-x",
            task_id="task-x",
            source_contract_version=CONTRACT_VERSION,
            checkpoint={"terminal_status": "completed_with_gaps"},
        )


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda payload: {**payload, "contract_version": "9.9.9"}, "contract_version"),
        (lambda payload: {**payload, "run_id": "run-other"}, "run_id"),
        (lambda payload: {**payload, "task_id": "task-other"}, "task_id"),
        (lambda payload: {**payload, "state": "running"}, "state"),
        (lambda payload: {**payload, "terminal_status": "failed"}, "terminal_status"),
    ],
)
def test_load_validated_terminal_status_rejects_witness_mismatch(tmp_path, mutate, expected):
    payload = {
        "contract_version": CONTRACT_VERSION,
        "run_id": "run-x",
        "task_id": "task-x",
        "state": "terminal",
        "terminal_status": "completed_with_gaps",
    }
    (tmp_path / "status.json").write_text(json.dumps(mutate(payload)) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provenance mismatch"):
        load_validated_terminal_status(
            run_dir=tmp_path,
            run_id="run-x",
            task_id="task-x",
            source_contract_version=CONTRACT_VERSION,
            checkpoint={"terminal_status": "completed_with_gaps"},
        )


def test_load_validated_terminal_status_matching_witness_returns_payload(tmp_path):
    payload = {
        "contract_version": CONTRACT_VERSION,
        "run_id": "run-x",
        "task_id": "task-x",
        "state": "terminal",
        "terminal_status": "completed_with_gaps",
    }
    (tmp_path / "status.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    result = load_validated_terminal_status(
        run_dir=tmp_path,
        run_id="run-x",
        task_id="task-x",
        source_contract_version=CONTRACT_VERSION,
        checkpoint={"terminal_status": "completed_with_gaps"},
    )
    assert result["terminal_status"] == "completed_with_gaps"


def test_restore_without_checkpoint_returns_initial_state():
    task = _task()
    plan = _plan(task)
    completed, candidates, calls = restore_checkpoint_state(
        task=task,
        plan=plan,
        checkpoint=None,
        stored_results=[],
        stored_evidence=[],
        merge_candidates=_merge,
    )
    assert completed == set()
    assert candidates == []
    assert calls == 0


def test_restore_happy_path_rebuilds_state_in_persisted_order():
    task = _task()
    plan = _plan(task)
    first = _result("tool_a", "tool-a-1", candidate_genes=["A"])
    second = _result("tool_b", "tool-b-1", candidate_genes=["B"])
    completed, candidates, calls = restore_checkpoint_state(
        task=task,
        plan=plan,
        checkpoint={"completed_steps": ["step-a", "step-b"], "tool_calls": 2},
        stored_results=[first, second],
        stored_evidence=[],
        merge_candidates=_merge,
    )
    assert completed == {"step-a", "step-b"}
    assert candidates == ["A", "B"]
    assert calls == 2


def test_restore_rejects_duplicate_tool_run_ids():
    task = _task()
    plan = _plan(task)
    duplicate = _result("tool_a", "tool-same")
    with pytest.raises(ValueError, match="duplicate tool_run_id"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": ["step-a", "step-b"], "tool_calls": 2},
            stored_results=[duplicate, duplicate.model_copy()],
            stored_evidence=[],
            merge_candidates=_merge,
        )


def test_restore_rejects_duplicate_evidence_ids():
    task = _task()
    plan = _plan(task)
    with pytest.raises(ValueError, match="duplicate evidence_id"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": ["step-a"], "tool_calls": 1},
            stored_results=[_result("tool_a", "tool-a-1")],
            stored_evidence=[_evidence("ev-same"), _evidence("ev-same")],
            merge_candidates=_merge,
        )


def test_restore_rejects_non_list_completed_steps():
    task = _task()
    plan = _plan(task)
    with pytest.raises(ValueError, match="completed_steps must be a list"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": "step-a", "tool_calls": 0},
            stored_results=[],
            stored_evidence=[],
            merge_candidates=_merge,
        )


def test_restore_rejects_duplicate_completed_steps():
    task = _task()
    plan = _plan(task)
    with pytest.raises(ValueError, match="duplicate step IDs"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": ["step-a", "step-a"], "tool_calls": 0},
            stored_results=[],
            stored_evidence=[],
            merge_candidates=_merge,
        )


def test_restore_rejects_unknown_step():
    task = _task()
    plan = _plan(task)
    with pytest.raises(ValueError, match="unknown completed steps"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": ["step-zzz"], "tool_calls": 0},
            stored_results=[],
            stored_evidence=[],
            merge_candidates=_merge,
        )


def test_restore_rejects_missing_dependency_closure():
    task = _task()
    plan = ExecutionPlan(
        task_id=task.task_id,
        planner_backend="test",
        steps=[
            PlanStep(step_id="step-a", name="A", tool="tool_a"),
            PlanStep(step_id="step-b", name="B", tool="tool_b", dependencies=["step-a"]),
        ],
    )
    with pytest.raises(ValueError, match="missing completed dependencies"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": ["step-b"], "tool_calls": 0},
            stored_results=[],
            stored_evidence=[],
            merge_candidates=_merge,
        )


def test_restore_rejects_orphan_tool_result():
    task = _task()
    plan = _plan(task)
    with pytest.raises(ValueError, match="no completed plan step"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": ["step-a"], "tool_calls": 2},
            stored_results=[
                _result("tool_a", "tool-a-1"),
                _result("tool_c", "tool-c-1"),
            ],
            stored_evidence=[],
            merge_candidates=_merge,
        )


def test_restore_rejects_missing_tool_result():
    task = _task()
    plan = _plan(task)
    with pytest.raises(ValueError, match="without matching ToolResult"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": ["step-a", "step-b"], "tool_calls": 1},
            stored_results=[_result("tool_a", "tool-a-1")],
            stored_evidence=[],
            merge_candidates=_merge,
        )


def test_restore_rejects_tool_call_mismatch():
    task = _task()
    plan = _plan(task)
    with pytest.raises(ValueError, match="does not match the number of persisted ToolResult"):
        restore_checkpoint_state(
            task=task,
            plan=plan,
            checkpoint={"completed_steps": ["step-a"], "tool_calls": 2},
            stored_results=[_result("tool_a", "tool-a-1")],
            stored_evidence=[],
            merge_candidates=_merge,
        )