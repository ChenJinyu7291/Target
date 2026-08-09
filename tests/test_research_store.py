import json

import pytest

from target_agent.contracts import ToolDescriptor, TraceEvent
from target_agent.research_contracts import (
    AssessmentDimension,
    WorkAttempt,
    WorkAttemptStatus,
    WorkItemHead,
    WorkerLease,
    AssessmentLevel,
    AssessmentRecord,
    AssessmentResult,
    DecisionAction,
    DecisionEvent,
    ProjectState,
    ProjectStatus,
    ResearchGoal,
    ResearchPlan,
    ResearchProjectSpec,
    WorkItemResult,
    WorkItemSpec,
    WorkItemStatus,
)
from target_agent.research_store import ProjectBusyError, ResearchProjectStore
from target_agent.research_projection import project_trace_event


def project_spec(project_id="project-test"):
    return ResearchProjectSpec(
        project_id=project_id,
        title="Traceable research project",
        goal=ResearchGoal(
            question="Which mechanism should be tested next?",
            success_criteria=["Every conclusion has a durable artifact"],
            deliverables=["Research report"],
        ),
    )


def research_plan(project_id="project-test"):
    return ResearchPlan(
        plan_id="plan-test",
        project_id=project_id,
        planner_backend="deterministic-test",
        rationale="Exercise the durable project store.",
        items=[
            WorkItemSpec(
                item_id="literature",
                title="Collect literature",
                module="literature_search",
                objective="Collect grounded source records.",
                acceptance_criteria=["At least one source is retained"],
            )
        ],
    )


def initialized_store(tmp_path):
    store = ResearchProjectStore(tmp_path / "projects", "project-test")
    store.create(project_spec())
    store.save_plan(research_plan())
    return store


def test_project_store_round_trip_and_integrity(tmp_path):
    store = initialized_store(tmp_path)
    work_file = store.project_dir / "work_items" / "literature" / "records.json"
    work_file.parent.mkdir(parents=True)
    work_file.write_text('{"pmid": "123"}\n', encoding="utf-8")
    artifact = store.register_artifact(work_file, "literature", "source-records", "application/json")

    result = WorkItemResult(
        item_id="literature",
        module="literature_search",
        status=WorkItemStatus.COMPLETED,
        summary="One source retained.",
        outputs={"record_count": 1},
        artifact_ids=[artifact.artifact_id],
        evidence_refs=["https://pubmed.ncbi.nlm.nih.gov/123/"],
    )
    store.save_work_item_result(result)
    store.save_state(ProjectState(
        project_id="project-test",
        status=ProjectStatus.COMPLETED,
        completed_items=["literature"],
        attempts={"literature": 1},
    ))
    first = store.append_event("item_started", "running", "literature")
    second = store.append_event("item_completed", "completed", "literature")
    store.append_assessment(AssessmentRecord(
        project_id="project-test",
        target_id=artifact.artifact_id,
        target_digest=artifact.sha256,
        dimension=AssessmentDimension.INTEGRITY,
        level=AssessmentLevel.A0,
        result=AssessmentResult.PASS,
        actor="research-store",
        method="sha256",
        rationale="Stored bytes match the immutable record.",
    ))
    store.append_decision(DecisionEvent(
        project_id="project-test",
        action=DecisionAction.ACCEPT,
        target_ids=[artifact.artifact_id],
        rationale="Integrity gate passed.",
        actor="test-reviewer",
    ))

    assert (store.load_spec().model_dump(mode="json", exclude={"created_at"})
            == project_spec().model_dump(mode="json", exclude={"created_at"}))
    assert (store.load_plan().model_dump(mode="json", exclude={"created_at"})
            == research_plan().model_dump(mode="json", exclude={"created_at"}))
    assert store.load_state().status == ProjectStatus.COMPLETED
    assert store.load_work_item_results()["literature"] == result
    assert [first.sequence, second.sequence] == [1, 2]
    assert store.read_assessments()[0].target_digest == artifact.sha256
    assert store.read_decisions()[0].action == DecisionAction.ACCEPT
    assert store.artifact_path(artifact).read_text(encoding="utf-8") == work_file.read_text(encoding="utf-8")
    store.assert_integrity()


def test_spec_and_plan_are_immutable_but_idempotent(tmp_path):
    store = initialized_store(tmp_path)
    store.create(project_spec())
    store.save_plan(research_plan())

    changed_spec = project_spec().model_copy(update={"title": "A different project title"})
    with pytest.raises(ValueError, match="immutable"):
        store.create(changed_spec)
    changed_plan = research_plan().model_copy(update={"rationale": "Changed after approval."})
    with pytest.raises(ValueError, match="immutable"):
        store.save_plan(changed_plan)


def test_artifact_registration_is_content_addressed_versioned_and_idempotent(tmp_path):
    store = initialized_store(tmp_path)
    work_file = store.project_dir / "work_items" / "literature" / "report.md"
    work_file.parent.mkdir(parents=True)
    work_file.write_text("version one\n", encoding="utf-8")

    first = store.register_artifact(work_file, "literature", "report", "text/markdown")
    duplicate = store.register_artifact(work_file, "literature", "report", "text/markdown")
    work_file.write_text("version two\n", encoding="utf-8")
    second = store.register_artifact(work_file, "literature", "report", "text/markdown")

    assert duplicate == first
    assert first.version == 1
    assert second.version == 2
    assert first.sha256 != second.sha256
    assert store.artifact_path(first).read_text(encoding="utf-8") == "version one\n"
    assert store.artifact_path(second).read_text(encoding="utf-8") == "version two\n"
    assert len(store.read_artifacts()) == 2
    store.assert_integrity()


def test_artifact_source_and_uri_cannot_escape_project(tmp_path):
    store = initialized_store(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not project evidence", encoding="utf-8")
    with pytest.raises(ValueError, match="inside the project"):
        store.register_artifact(outside, "literature", "outside", "text/plain")

    artifact_file = store.project_dir / "inside.txt"
    artifact_file.write_text("inside", encoding="utf-8")
    artifact = store.register_artifact(artifact_file, "literature", "inside", "text/plain")
    escaped = artifact.model_copy(update={"uri": "project://../outside.txt"})
    with pytest.raises(ValueError, match="escapes"):
        store.artifact_path(escaped)

    with pytest.raises(ValueError, match="unsafe project_id"):
        ResearchProjectStore(tmp_path / "projects", "../escaped")


def test_integrity_detects_artifact_tampering_and_missing_references(tmp_path):
    store = initialized_store(tmp_path)
    work_file = store.project_dir / "source.txt"
    work_file.write_text("original", encoding="utf-8")
    artifact = store.register_artifact(work_file, "literature", "source", "text/plain")
    store.artifact_path(artifact).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact (size|digest) mismatch"):
        store.assert_integrity()

    other = initialized_store(tmp_path / "other")
    other.save_work_item_result(WorkItemResult(
        item_id="literature",
        module="literature_search",
        status=WorkItemStatus.COMPLETED,
        summary="Bad artifact reference.",
        artifact_ids=["artifact-missing"],
    ))
    with pytest.raises(ValueError, match="missing artifacts"):
        other.assert_integrity()


def test_integrity_rejects_non_monotonic_event_ledger(tmp_path):
    store = initialized_store(tmp_path)
    store.append_event("created", "draft")
    event_path = store.project_dir / "events.jsonl"
    payload = json.loads(event_path.read_text(encoding="utf-8").strip())
    payload["sequence"] = 3
    event_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sequence"):
        store.assert_integrity()


def test_execution_lock_prevents_concurrent_state_machines(tmp_path):
    store = initialized_store(tmp_path)
    second_instance = ResearchProjectStore(tmp_path / "projects", "project-test")
    with store.execution_lock():
        with pytest.raises(ProjectBusyError, match="already executing"):
            with second_instance.execution_lock():
                raise AssertionError("second execution lock must not be acquired")


def test_domain_activity_ledger_is_sequenced_idempotent_and_source_linked(tmp_path):
    store = initialized_store(tmp_path)
    trace = TraceEvent(
        run_id="target-project-test",
        task_id="task-test",
        event_type="tool_result",
        state="tool_execution",
        detail={
            "tool": "europe_pmc_rag",
            "status": "partial",
            "coverage_status": "partial",
            "context_match_score": 0.8,
        },
        related_ids=["tool-test"],
    )
    trace_path = store.project_dir / "work_items" / "literature" / "trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(trace.model_dump_json() + "\n", encoding="utf-8")
    store.register_artifact(
        trace_path, "literature", "target_discovery_trace", "application/x-ndjson",
    )
    projection = project_trace_event(
        project_id=store.project_id,
        work_item_id="literature",
        child_run_id=trace.run_id,
        event=trace,
        descriptors=[ToolDescriptor(
            tool_id="europe_pmc_rag",
            evidence_dimension="literature",
            description="Literature retrieval and grounded extraction.",
        )],
    )

    first = store.append_domain_activity(projection)
    duplicate = store.append_domain_activity(projection)

    assert first == duplicate
    assert first.sequence == 1
    assert store.domain_activity_cursor() == 1
    assert store.read_domain_activities(after_sequence=1) == []
    assert store.read_domain_activities(limit=1) == [first]
    store.assert_integrity()

    ledger = store.project_dir / "domain_activities.jsonl"
    tampered = json.loads(ledger.read_text(encoding="utf-8"))
    tampered["source_event_sha256"] = "0" * 64
    ledger.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source digest mismatch"):
        store.assert_integrity()


def test_domain_activity_integrity_rejects_sequence_and_source_tampering(tmp_path):
    store = initialized_store(tmp_path)
    path = store.project_dir / "domain_activities.jsonl"
    path.write_text(json.dumps({
        "contract_version": "3.1.0",
        "sequence": 2,
        "activity_id": "trace-one",
        "project_id": store.project_id,
        "work_item_id": "literature",
        "child_run_id": "target-project-test",
        "source_contract_version": "2.2.0",
        "source_trace_id": "trace-one",
        "source_event_sha256": "0" * 64,
        "stage": "literature",
        "activity_type": "tool_call",
        "status": "running",
        "source_state": "tool_execution",
        "related_ids": [],
        "summary": "Started a literature tool.",
        "detail": {},
        "created_at": "2026-08-05T00:00:00Z",
    }) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sequence"):
        store.assert_integrity()


def test_lease_append_release_and_reacquire(tmp_path):
    store = initialized_store(tmp_path)
    first = WorkerLease(
        lease_id="lease-" + "a" * 24,
        project_id="project-test",
        work_item_id="literature",
        attempt_id="attempt-" + "a" * 24,
        worker_id="worker-a",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    store.append_lease(first)
    active = [row for row in store.read_leases() if row.released_at is None]
    assert [row.lease_id for row in active] == [first.lease_id]

    released = store.release_lease(first.lease_id)
    assert released.released_at is not None
    latest = store.read_leases(first.work_item_id)
    assert len(latest) == 1
    assert latest[0].released_at is not None

    # A released lease must not block a new lease for the same work item.
    second = WorkerLease(
        lease_id="lease-" + "b" * 24,
        project_id="project-test",
        work_item_id="literature",
        attempt_id="attempt-" + "b" * 24,
        worker_id="worker-b",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    store.append_lease(second)
    assert [row.lease_id for row in store.read_leases() if row.released_at is None] == [second.lease_id]


def test_attempt_ledger_is_immutable_and_contiguous(tmp_path):
    store = initialized_store(tmp_path)
    first = WorkAttempt(
        attempt_id="attempt-" + "a" * 24,
        project_id="project-test",
        work_item_id="literature",
        attempt_number=1,
        status=WorkAttemptStatus.COMPLETED,
        input_digest="0" * 64,
        output_digest="1" * 64,
        completed_at="2026-08-08T00:00:00+00:00",
    )
    store.append_attempt(first)
    with pytest.raises(ValueError, match="already exists"):
        store.append_attempt(first)
    with pytest.raises(ValueError, match="does not follow"):
        store.append_attempt(first.model_copy(update={
            "attempt_id": "attempt-" + "b" * 24,
            "attempt_number": 3,
        }))
    store.append_attempt(first.model_copy(update={
        "attempt_id": "attempt-" + "b" * 24,
        "attempt_number": 2,
    }))
    assert [row.attempt_number for row in store.read_attempts("literature")] == [1, 2]
    with pytest.raises(ValueError):
        WorkAttempt(
            attempt_id="attempt-" + "c" * 24,
            project_id="project-test",
            work_item_id="literature",
            attempt_number=1,
            status=WorkAttemptStatus.RUNNING,
            input_digest="0" * 64,
            completed_at="2026-08-08T00:00:00+00:00",
        )

def test_work_item_head_cas_idempotent_and_conflict(tmp_path):
    store = initialized_store(tmp_path)
    first = WorkItemHead(
        head_id="head-" + "a" * 12,
        project_id="project-test",
        work_item_id="literature",
        attempt_id="attempt-" + "a" * 24,
        result_digest="0" * 64,
        status=WorkItemStatus.COMPLETED,
        version=1,
    )
    committed = store.update_work_item_head(first, expected_version=None)
    assert committed == first
    # replaying the same committed attempt is an idempotent no-op
    assert store.update_work_item_head(first, expected_version=1) == first
    # a stale writer cannot overwrite a newer commit
    with pytest.raises(ValueError, match="CAS conflict|must follow"):
        store.update_work_item_head(first.model_copy(update={
            "head_id": "head-" + "b" * 12,
            "attempt_id": "attempt-" + "b" * 24,
            "result_digest": "1" * 64,
        }), expected_version=1)
    # a second attempt bumps the head and records the superseded head
    second = store.update_work_item_head(WorkItemHead(
        head_id="head-" + "b" * 12,
        project_id="project-test",
        work_item_id="literature",
        attempt_id="attempt-" + "b" * 24,
        result_digest="1" * 64,
        status=WorkItemStatus.COMPLETED_WITH_GAPS,
        version=2,
    ), expected_version=1)
    assert second.version == 2
    assert second.supersedes_head_id == first.head_id
    assert store.read_work_item_head("literature").result_digest == "1" * 64
    assert [row.head_id for row in store.read_work_item_heads()] == [second.head_id]


def test_artifact_registration_writes_version_ledger_and_active_head(tmp_path):
    store = initialized_store(tmp_path)
    work_file = store.project_dir / "work_items" / "literature" / "report.md"
    work_file.parent.mkdir(parents=True)
    work_file.write_text("version one\n", encoding="utf-8")

    first = store.register_artifact(work_file, "literature", "report", "text/markdown")
    work_file.write_text("version two\n", encoding="utf-8")
    second = store.register_artifact(work_file, "literature", "report", "text/markdown")
    duplicate = store.register_artifact(work_file, "literature", "report", "text/markdown")

    assert duplicate == second
    versions = store.read_artifact_versions()
    assert [row.version for row in versions] == [1, 2]
    assert versions[0].artifact_id == versions[1].artifact_id
    assert versions[1].supersedes_version_id == versions[0].version_id
    assert versions[1].record_id == second.artifact_id
    heads = store.read_artifact_heads()
    assert len(heads) == 1
    assert heads[0].artifact_id == versions[1].artifact_id
    assert heads[0].version_id == versions[1].version_id
    assert heads[0].version == 2
    assert heads[0].record_id == second.artifact_id
    # old versions are preserved and remain readable
    assert store.artifact_path(first).read_text(encoding="utf-8") == "version one\n"
    assert store.artifact_path(second).read_text(encoding="utf-8") == "version two\n"
    store.assert_integrity()


def test_lease_heartbeat_refreshes_expiry(tmp_path):
    store = initialized_store(tmp_path)
    lease = WorkerLease(
        lease_id="lease-" + "a" * 24,
        project_id="project-test",
        work_item_id="literature",
        attempt_id="attempt-" + "a" * 24,
        worker_id="worker-a",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    store.append_lease(lease)
    refreshed = store.heartbeat_lease(lease.lease_id)
    assert refreshed.released_at is None
    assert refreshed.expires_at > "2000-01-01T00:00:00+00:00"
    assert refreshed.heartbeat_at >= lease.heartbeat_at
    store.release_lease(lease.lease_id)
    assert store.heartbeat_lease(lease.lease_id).released_at is not None

def test_event_ledger_append_is_contiguous_and_incrementally_readable(tmp_path):
    store = initialized_store(tmp_path)
    events = [store.append_event(f"event_{i}", "running") for i in range(5)]
    assert [row.sequence for row in events] == [1, 2, 3, 4, 5]
    assert store.count_events() == 5
    assert [row.sequence for row in store.read_events(after_sequence=2)] == [3, 4, 5]
    assert [row.sequence for row in store.read_events(after_sequence=2, limit=2)] == [3, 4]
    with pytest.raises(ValueError, match="after_sequence"):
        store.read_events(after_sequence=-1)
    with pytest.raises(ValueError, match="limit"):
        store.read_events(limit=0)
    # A fresh store instance observes exactly the same ledger, so the
    # process-local tail cursor never drifts from the durable file.
    fresh = ResearchProjectStore(store.projects_dir, store.project_id)
    assert [row.sequence for row in fresh.read_events()] == [1, 2, 3, 4, 5]
    assert fresh.count_events() == 5


def test_domain_activity_appends_use_tail_cursor_and_stay_contiguous(tmp_path):
    store = initialized_store(tmp_path)
    descriptors = [ToolDescriptor(
        tool_id="europe_pmc_rag",
        evidence_dimension="literature",
        description="Literature retrieval and grounded extraction.",
    )]

    def projection_for(event_id: str, run_id: str):
        return project_trace_event(
            project_id=store.project_id,
            work_item_id="literature",
            child_run_id=run_id,
            event=TraceEvent(
                event_id=event_id,
                run_id=run_id,
                task_id=f"task-{run_id}",
                event_type="tool_result",
                state="tool_execution",
                detail={
                    "tool": "europe_pmc_rag",
                    "status": "partial",
                    "coverage_status": "partial",
                    "context_match_score": 0.8,
                },
                related_ids=["tool-cursor"],
                created_at="2026-01-01T00:00:00Z",
            ),
            descriptors=descriptors,
        )

    first = store.append_domain_activity(projection_for("trace-cursor-1", "run-cursor-1"))
    assert first.sequence == 1
    assert store.append_domain_activity(
        projection_for("trace-cursor-1", "run-cursor-1")
    ) == first
    second = store.append_domain_activity(projection_for("trace-cursor-2", "run-cursor-2"))
    assert second.sequence == 2
    assert store.domain_activity_cursor() == 2
    assert [row.sequence for row in store.read_domain_activities()] == [1, 2]
    # A fresh store instance observes the same durable ledger.
    fresh = ResearchProjectStore(store.projects_dir, store.project_id)
    assert [row.sequence for row in fresh.read_domain_activities()] == [1, 2]
