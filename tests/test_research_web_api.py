from __future__ import annotations

import time

from target_agent.webapp import create_app

from .test_research_runtime import fake_research_runtime, research_project
from .test_runtime import fake_runtime as fake_target_runtime


def _wait_for_project(client, project_id: str, timeout_seconds: float = 3.0):
    deadline = time.monotonic() + timeout_seconds
    latest = None
    while time.monotonic() < deadline:
        latest = client.get(f"/api/projects/{project_id}")
        if latest.status_code == 200:
            state = latest.get_json().get("state") or {}
            if state.get("status") in {"completed", "completed_with_gaps", "needs_input", "failed"}:
                return latest
        time.sleep(0.01)
    raise AssertionError(
        f"project {project_id} did not reach a terminal state; "
        f"last status was {None if latest is None else latest.status_code}"
    )


def test_project_post_get_events_and_artifact_download_use_durable_state(tmp_path):
    research_runtime, calls = fake_research_runtime(tmp_path)
    app = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    )
    client = app.test_client()
    project = research_project("project-web-api")

    created = client.post("/api/projects", json=project.model_dump(mode="json"))

    assert created.status_code == 202
    assert created.get_json() == {
        "project_id": project.project_id,
        "status_url": f"/api/projects/{project.project_id}",
        "events_url": f"/api/projects/{project.project_id}/events",
    }
    response = _wait_for_project(client, project.project_id)
    payload = response.get_json()
    assert payload["contract_version"] == "3.1.0"
    assert payload["spec"]["goal"]["question"] == project.goal.question
    assert payload["state"]["status"] == "completed"
    assert len(payload["plan"]["items"]) == 5
    assert len(payload["work_item_results"]) == 5
    assert calls["research_report"] == 1

    events = client.get(f"/api/projects/{project.project_id}/events")
    assert events.status_code == 200
    event_rows = events.get_json()["events"]
    assert [row["sequence"] for row in event_rows] == list(range(1, len(event_rows) + 1))
    assert event_rows[-1]["event_type"] == "project_terminal"
    tail = client.get(
        f"/api/projects/{project.project_id}/events?after_sequence={event_rows[-2]['sequence']}"
    ).get_json()
    assert tail["events"] == [event_rows[-1]]
    assert tail["next_cursor"] == event_rows[-1]["sequence"]

    activities = client.get(f"/api/projects/{project.project_id}/activities")
    assert activities.status_code == 200
    assert activities.get_json() == {
        "contract_version": "3.1.0",
        "project_id": project.project_id,
        "activities": [],
        "next_cursor": 0,
        "has_more": False,
    }
    repairs = client.get(f"/api/projects/{project.project_id}/repairs")
    assert repairs.status_code == 200
    assert repairs.get_json()["requests"] == []
    assert repairs.get_json()["remaining_replans"] == 2

    report = next(row for row in payload["artifacts"] if row["logical_name"] == "research_report")
    downloaded = client.get(
        f"/api/projects/{project.project_id}/artifacts/{report['artifact_id']}"
    )
    assert downloaded.status_code == 200
    assert downloaded.mimetype == "text/markdown"
    assert downloaded.get_data(as_text=True).startswith("# Fake research report")


def test_project_api_rejects_invalid_payload_and_duplicate_project(tmp_path):
    research_runtime, _ = fake_research_runtime(tmp_path)
    client = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    ).test_client()

    invalid = client.post("/api/projects", json={"title": "missing goal"})
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "invalid ResearchProjectSpec"

    project = research_project("project-web-duplicate")
    assert client.post("/api/projects", json=project.model_dump(mode="json")).status_code == 202
    _wait_for_project(client, project.project_id)
    duplicate = client.post("/api/projects", json=project.model_dump(mode="json"))
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"] == "project id already exists"


def test_mechanism_graph_endpoint_is_explicit_when_no_domain_task(tmp_path):
    research_runtime, _ = fake_research_runtime(tmp_path)
    client = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    ).test_client()
    project = research_project("project-mechanism-web")
    assert client.post("/api/projects", json=project.model_dump(mode="json")).status_code == 202
    _wait_for_project(client, project.project_id)
    response = client.get(f"/api/projects/{project.project_id}/mechanism-graph")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["available"] is False
    assert payload["graph"] is None
    assert payload["reason"]
    assert client.get("/api/projects/project-does-not-exist/mechanism-graph").status_code == 404


def test_project_api_missing_resources_are_explicit(tmp_path):
    research_runtime, _ = fake_research_runtime(tmp_path)
    client = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    ).test_client()

    assert client.get("/api/projects/project-does-not-exist").status_code == 404
    assert client.get("/api/projects/project-does-not-exist/events").status_code == 404
    assert client.get("/api/projects/project-does-not-exist/activities").status_code == 404
    assert client.get("/api/projects/project-does-not-exist/repairs").status_code == 404
    assert client.get("/api/projects/project-does-not-exist/events?after_sequence=bad").status_code == 400
    assert (
        client.get("/api/projects/project-does-not-exist/artifacts/artifact-missing").status_code
        == 404
    )


def test_project_repair_api_exposes_verified_autonomous_repair(tmp_path):
    research_runtime, _ = fake_research_runtime(
        tmp_path, transient_fail_once="literature_search",
    )
    client = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    ).test_client()
    project = research_project("project-web-repair")

    assert client.post("/api/projects", json=project.model_dump(mode="json")).status_code == 202
    payload = _wait_for_project(client, project.project_id).get_json()
    assert payload["state"]["status"] == "completed"
    queue = client.get(f"/api/projects/{project.project_id}/repairs").get_json()
    assert len(queue["requests"]) == len(queue["revisions"]) == len(queue["resolutions"]) == 1
    assert queue["resolutions"][0]["status"] == "resolved"


def test_project_list_exposes_durable_summaries(tmp_path):
    research_runtime, _ = fake_research_runtime(tmp_path)
    client = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    ).test_client()

    assert client.get("/api/projects").get_json() == {"projects": []}

    first = research_project("project-web-list-a")
    second = research_project("project-web-list-b")
    assert client.post("/api/projects", json=first.model_dump(mode="json")).status_code == 202
    assert client.post("/api/projects", json=second.model_dump(mode="json")).status_code == 202
    _wait_for_project(client, first.project_id)
    _wait_for_project(client, second.project_id)

    rows = client.get("/api/projects").get_json()["projects"]
    assert {row["project_id"] for row in rows} == {first.project_id, second.project_id}
    by_id = {row["project_id"]: row for row in rows}
    assert by_id[first.project_id]["status"] == "completed"
    assert by_id[first.project_id]["title"] == first.title
    assert by_id[second.project_id]["status"] == "completed"


def test_project_resume_endpoint_queues_reconcile_and_rejects_missing(tmp_path):
    research_runtime, _ = fake_research_runtime(tmp_path)
    client = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    ).test_client()

    assert client.post("/api/projects/project-does-not-exist/resume").status_code == 404

    project = research_project("project-web-resume")
    assert client.post("/api/projects", json=project.model_dump(mode="json")).status_code == 202
    _wait_for_project(client, project.project_id)
    resumed = client.post(f"/api/projects/{project.project_id}/resume")
    assert resumed.status_code == 202
    assert resumed.get_json()["resume_queued"] is True
    assert resumed.get_json()["project_id"] == project.project_id
    assert _wait_for_project(client, project.project_id).get_json()["state"]["status"] == "completed"
