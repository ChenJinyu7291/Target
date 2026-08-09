"""Round-4 control plane: durable pause/cancel/resume semantics.

The control request is an append-only event plus a current-intent file; the
runtime consumes it only at safe work-item boundaries. Cancelled projects are
terminal and produce no ranking/report/release; paused projects resume from
the last durable boundary.
"""
from __future__ import annotations

from collections import Counter

import pytest

from target_agent.research_contracts import (
    ControlRequestKind,
    ProjectStatus,
    ResearchProjectControl,
    ResearchProjectSpec,
    WorkItemStatus,
)
from target_agent.research_modules import ResearchModuleRegistry
from target_agent.research_planner import ResearchPlanner
from target_agent.research_runtime import ResearchProjectRuntime
from target_agent.research_service import ResearchDecisionError, ResearchProjectService
from target_agent.research_store import ResearchProjectStore
from target_agent.settings import Settings
from target_agent.webapp import create_app

from .test_research_runtime import BASELINE_MODULES, FakeResearchModule, research_project
from .test_runtime import fake_runtime as fake_target_runtime


class ControlRequestingModule(FakeResearchModule):
    """Deterministic module that queues a control request after one execution."""

    def __init__(self, name, calls, kind, projects_dir, project_id, fail_module=None):
        super().__init__(name, calls, fail_module=fail_module)
        self.kind = kind
        self.projects_dir = projects_dir
        self.project_id = project_id
        self.triggered = False

    def execute(self, context):
        result = super().execute(context)
        if not self.triggered:
            self.triggered = True
            store = ResearchProjectStore(self.projects_dir, self.project_id)
            store.save_control(ResearchProjectControl(
                project_id=self.project_id, request=self.kind,
                actor="tester", rationale="queued from a deterministic module",
            ))
        return result


def control_runtime(tmp_path, kind, *, trigger_module="literature_search", project_id="project-control"):
    calls: Counter = Counter()
    modules = []
    for name in (*BASELINE_MODULES, "target_discovery"):
        if trigger_module is not None and name == trigger_module:
            modules.append(ControlRequestingModule(
                name, calls, kind, tmp_path / "projects", project_id,
            ))
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


def test_store_control_roundtrip_and_clear(tmp_path):
    store = ResearchProjectStore(tmp_path / "projects", "project-control-store")
    store.create(ResearchProjectSpec(
        project_id="project-control-store",
        title="Control store roundtrip",
        goal=research_project().goal,
    ))
    assert store.load_control() is None
    control = ResearchProjectControl(
        project_id="project-control-store", request=ControlRequestKind.PAUSE,
        actor="tester", rationale="pause for review",
    )
    store.save_control(control)
    assert store.load_control().request == ControlRequestKind.PAUSE
    store.clear_control()
    assert store.load_control() is None


def test_pause_queued_while_running_is_consumed_at_boundary(tmp_path):
    runtime, calls = control_runtime(tmp_path, ControlRequestKind.PAUSE)
    project = research_project("project-control")
    terminal = runtime.run(project)

    assert terminal["status"] == ProjectStatus.PAUSED.value
    assert calls["project_brief"] == 1
    assert calls["literature_search"] == 1
    assert calls["hypothesis_generation"] == 0
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    assert store.load_control() is None
    events = [row.event_type for row in store.read_events()]
    assert "control_requested" not in events  # runtime-level test queues directly
    assert "execution_paused" in events
    assert "project_terminal" not in events

    service = ResearchProjectService(runtime)
    resumed = service.resume_project(
        project_id=project.project_id, actor="tester", rationale="continue after pause",
    )
    assert resumed["state"]["status"] == ProjectStatus.COMPLETED.value
    assert calls["hypothesis_generation"] == 1
    assert calls["independent_review"] == 1
    assert calls["research_report"] == 1


def test_cancel_queued_while_running_marks_skipped_and_no_report(tmp_path):
    runtime, calls = control_runtime(tmp_path, ControlRequestKind.CANCEL)
    project = research_project("project-control")
    terminal = runtime.run(project)

    assert terminal["status"] == ProjectStatus.CANCELLED.value
    assert calls["project_brief"] == 1
    assert calls["literature_search"] == 1
    assert calls["research_report"] == 0
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    assert store.load_control() is None
    results = store.load_work_item_results()
    assert results["hypothesis_generation"].status == WorkItemStatus.SKIPPED
    assert results["independent_review"].status == WorkItemStatus.SKIPPED
    assert results["research_report"].status == WorkItemStatus.SKIPPED
    assert not any(row.logical_name == "research_report" for row in store.read_artifacts())
    events = [row.event_type for row in store.read_events()]
    assert "execution_cancelled" in events
    assert events[-1] == "project_terminal"

    terminal_snapshot = ResearchProjectService(runtime).resume_project(
        project_id=project.project_id, actor="tester", rationale="inspect after cancel",
    )
    # Terminal resume stays an idempotent inspect-only no-op (BM-04 semantics).
    assert terminal_snapshot["state"]["status"] == ProjectStatus.CANCELLED.value


def test_cancel_immediate_when_idle(tmp_path):
    runtime, _ = control_runtime(tmp_path, ControlRequestKind.CANCEL)
    service = ResearchProjectService(runtime)
    project = research_project("project-idle-cancel")
    service.reserve(project)

    snapshot = service.cancel_project(
        project_id=project.project_id, actor="tester", rationale="decided to stop",
    )
    assert snapshot["control_queued"] is False
    assert snapshot["state"]["status"] == ProjectStatus.CANCELLED.value
    assert "execution_cancelled" in [
        row["event_type"] for row in service.events(project.project_id)
    ]


def test_pause_immediate_then_resume_completes(tmp_path):
    runtime, _ = control_runtime(tmp_path, ControlRequestKind.PAUSE)
    service = ResearchProjectService(runtime)
    project = research_project("project-idle-pause")
    service.reserve(project)

    snapshot = service.pause_project(
        project_id=project.project_id, actor="tester", rationale="pause before start",
    )
    assert snapshot["control_queued"] is False
    assert snapshot["state"]["status"] == ProjectStatus.PAUSED.value
    assert snapshot["next_actions"][0]["action"] == "resume_project"

    resumed = service.resume_project(
        project_id=project.project_id, actor="tester", rationale="go ahead",
    )
    assert resumed["state"]["status"] == ProjectStatus.COMPLETED.value


def test_control_requires_actor_and_rationale(tmp_path):
    runtime, _ = control_runtime(tmp_path, ControlRequestKind.PAUSE)
    service = ResearchProjectService(runtime)
    project = research_project("project-control-validation")
    service.reserve(project)
    with pytest.raises(ResearchDecisionError, match="actor and rationale"):
        service.pause_project(project_id=project.project_id, actor="  ", rationale="x")
    with pytest.raises(ResearchDecisionError, match="actor and rationale"):
        service.cancel_project(project_id=project.project_id, actor="tester", rationale="  ")


def test_web_pause_cancel_resume_routes(tmp_path):
    research_runtime, _ = control_runtime(
        tmp_path, ControlRequestKind.PAUSE, project_id="project-web-control",
        trigger_module=None,
    )
    client = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    ).test_client()
    project = research_project("project-web-control")
    service = ResearchProjectService(research_runtime)
    service.reserve(project)

    paused = client.post(f"/api/projects/{project.project_id}/pause", json={
        "actor": "tester", "rationale": "pause via http",
    })
    assert paused.status_code == 202
    assert paused.get_json()["state"]["status"] == "paused"

    resumed = client.post(f"/api/projects/{project.project_id}/resume", json={
        "actor": "tester", "rationale": "resume via http",
    })
    assert resumed.status_code == 202
    assert resumed.get_json()["resume_queued"] is True

    import time
    deadline = time.monotonic() + 5.0
    status = None
    while time.monotonic() < deadline:
        status = client.get(f"/api/projects/{project.project_id}").get_json()["state"]["status"]
        if status in {"completed", "completed_with_gaps", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    assert status == "completed"

    cancelled = client.post(f"/api/projects/{project.project_id}/cancel", json={
        "actor": "tester", "rationale": "cancel a completed project",
    })
    assert cancelled.status_code == 409
