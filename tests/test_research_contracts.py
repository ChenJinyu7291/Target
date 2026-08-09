from __future__ import annotations

import pytest
from pydantic import ValidationError

from target_agent.research_contracts import (
    ArtifactVersion,
    DataContract, ResearchGoal, ResearchPlan, ResearchProjectSnapshot, ResearchProjectSpec,
    ReviewTarget, WorkAttempt, WorkAttemptStatus, WorkItemResult, WorkItemSpec, WorkItemStatus,
    WorkerLease,
)


def project() -> ResearchProjectSpec:
    return ResearchProjectSpec(
        project_id="project-contract",
        title="Disease target research",
        domain="disease_target_discovery",
        goal=ResearchGoal(
            question="Which targets should be prioritized for this disease?",
            success_criteria=["Every ranked target is evidence traceable."],
            deliverables=["Reviewed TargetCards"],
        ),
    )


def item(item_id: str, dependencies: list[str] | None = None) -> WorkItemSpec:
    return WorkItemSpec(
        item_id=item_id,
        title=f"Execute {item_id}",
        module="literature_search",
        objective="Produce a typed source-indexed result.",
        dependencies=dependencies or [],
        acceptance_criteria=["The output contract is satisfied."],
        output_contract=DataContract(
            schema_id="TestResult", required_fields=["count"], field_types={"count": "integer"},
        ),
    )
def test_research_plan_round_trips_evidence_strategy_patterns():
    patterns = [
        {
            "pattern_id": "pattern-test-ibd",
            "name": "IBD genetics-first",
            "chosen_start": "genetics",
            "ordered_lanes": ["genetics", "omics"],
            "why_this_order": "Genetics anchors causality.",
            "stop_rules": ["no candidate without genetics support"],
            "strategy_hint_not_evidence": True,
            "score": 1.0,
        }
    ]
    plan = ResearchPlan(
        project_id=project().project_id, planner_backend="step:test", rationale="ok",
        items=[item("one")],
        evidence_strategy_patterns=patterns,
    )
    restored = ResearchPlan.model_validate_json(plan.model_dump_json())
    assert restored.evidence_strategy_patterns == patterns

    legacy = ResearchPlan(
        project_id=project().project_id, planner_backend="deterministic", rationale="legacy",
        items=[item("one")],
    )
    assert legacy.evidence_strategy_patterns == []


def test_research_plan_rejects_unknown_dependencies_and_cycles():
    with pytest.raises(ValidationError, match="unknown dependencies"):
        ResearchPlan(
            project_id=project().project_id, planner_backend="test", rationale="invalid",
            items=[item("one", ["missing"])],
        )
    with pytest.raises(ValidationError, match="dependency cycle"):
        ResearchPlan(
            project_id=project().project_id, planner_backend="test", rationale="invalid",
            items=[item("one", ["two"]), item("two", ["one"])],
        )


def test_data_contract_requires_declared_types_for_required_fields():
    with pytest.raises(ValidationError, match="no declared type"):
        DataContract(schema_id="Broken", required_fields=["count"])


def test_project_id_rejects_path_components():
    payload = project().model_dump(mode="json")
    payload["project_id"] = "../escape"
    with pytest.raises(ValidationError):
        ResearchProjectSpec.model_validate(payload)


def test_project_context_rejects_credentials_but_allows_scientific_key_names():
    payload = project().model_dump(mode="json")
    payload["context"] = {"cell_type_key": "cell_type", "nested": {"api_key": "must-not-persist"}}
    with pytest.raises(ValidationError, match="cannot contain credentials"):
        ResearchProjectSpec.model_validate(payload)

    payload["context"] = {"cell_type_key": "cell_type", "donor_key": "donor_id"}
    assert ResearchProjectSpec.model_validate(payload).context["cell_type_key"] == "cell_type"

class _ProjectBoundWorkItemResult(WorkItemResult):
    """Test-only subclass that carries a project_id binding.

    The base ``WorkItemResult`` contract has no project_id today, so the
    snapshot validator's check for this collection only activates when a
    project binding is present.
    """

    project_id: str


@pytest.mark.parametrize("field_name,record", [
    (
        "work_item_results",
        _ProjectBoundWorkItemResult(
            item_id="literature", module="literature_search",
            status=WorkItemStatus.COMPLETED, summary="Mixed-in result.",
            project_id="project-other",
        ),
    ),
    (
        "work_attempts",
        WorkAttempt(
            attempt_id="attempt-" + "a" * 24,
            project_id="project-other",
            work_item_id="literature",
            attempt_number=1,
            status=WorkAttemptStatus.COMPLETED,
            input_digest="0" * 64,
            output_digest="1" * 64,
            completed_at="2026-08-08T00:00:00+00:00",
        ),
    ),
    (
        "artifact_versions",
        ArtifactVersion(
            version_id="artifact-version-" + "a" * 24,
            project_id="project-other",
            artifact_id="artifact-" + "a" * 24,
            record_id="artifact-" + "a" * 24,
            version=1,
            sha256="0" * 64,
            size_bytes=1,
            work_item_id="literature",
            logical_name="report",
            media_type="text/markdown",
            uri="project://artifacts/foreign",
        ),
    ),
    (
        "review_targets",
        ReviewTarget(
            review_target_id="review-target-" + "a" * 24,
            project_id="project-other",
            scope="work_item",
            work_item_id="literature",
            snapshot_digest="0" * 64,
            reason="Mixed-in review target.",
        ),
    ),
    (
        "worker_leases",
        WorkerLease(
            lease_id="lease-" + "a" * 24,
            project_id="project-other",
            work_item_id="literature",
            attempt_id="attempt-" + "a" * 24,
            worker_id="worker-a",
            expires_at="2026-08-08T00:00:00+00:00",
        ),
    ),
], ids=["work_item_result", "work_attempt", "artifact_version", "review_target", "worker_lease"])
def test_snapshot_rejects_cross_project_record_mixing(field_name, record):
    with pytest.raises(ValidationError, match="another project's record"):
        ResearchProjectSnapshot(spec=project(), **{field_name: [record]})
