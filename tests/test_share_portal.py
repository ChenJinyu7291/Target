from __future__ import annotations

import json
import re
import time
from pathlib import Path

from target_agent.research_contracts import AutonomyMode
from target_agent.share_portal import (
    build_portal_payload,
    render_share_portal_for_project,
    render_share_portal_from_package,
)
from target_agent.webapp import create_app

from .test_research_runtime import fake_research_runtime, research_project
from .test_runtime import fake_runtime as fake_target_runtime


def _wait_for(client, project_id, predicate, timeout_seconds: float = 10.0):
    deadline = time.monotonic() + timeout_seconds
    latest = None
    while time.monotonic() < deadline:
        latest = client.get(f"/api/projects/{project_id}").get_json()
        if predicate(latest):
            return latest
        time.sleep(0.01)
    raise AssertionError(
        f"project {project_id} did not satisfy predicate; last: {latest}"
    )


def _completed_project(tmp_path):
    research_runtime, calls = fake_research_runtime(tmp_path)
    client = create_app(
        fake_target_runtime(tmp_path),
        research_runtime=research_runtime,
    ).test_client()
    project = research_project(
        "project-share-portal",
        autonomy_mode=AutonomyMode.CHECKPOINTED,
    )
    created = client.post("/api/projects", json=project.model_dump(mode="json"))
    assert created.status_code == 202
    project_id = project.project_id

    def at_plan(snap):
        return (snap.get("state") or {}).get("checkpoint_kind") == "plan"

    planned = _wait_for(client, project_id, at_plan)
    plan_target = planned["next_actions"][0]["target_id"]
    session_id = client.post(
        f"/api/projects/{project_id}/sessions", json={"title": "审批会话"}
    ).get_json()["session"]["session_id"]
    approved = client.post(
        f"/api/projects/{project_id}/sessions/{session_id}/interventions",
        json={
            "action": "accept_checkpoint",
            "rationale": "计划可接受",
            "actor": "reviewer",
            "target_id": plan_target,
        },
    )
    assert approved.status_code == 202

    def at_release(snap):
        return (snap.get("state") or {}).get("checkpoint_kind") == "release"

    release = _wait_for(client, project_id, at_release)
    release_target = release["next_actions"][0]["target_id"]
    released = client.post(
        f"/api/projects/{project_id}/sessions/{session_id}/interventions",
        json={
            "action": "accept_checkpoint",
            "rationale": "发布通过",
            "actor": "reviewer",
            "target_id": release_target,
        },
    )
    assert released.status_code == 202

    def at_completed(snap):
        return (snap.get("state") or {}).get("status") == "completed"

    completed = _wait_for(client, project_id, at_completed)
    assert completed["state"]["status"] == "completed"
    return research_runtime, client, project_id, session_id


def _embedded_data(html: str) -> dict:
    line = html.split("const PORTAL_DATA = ", 1)[1].splitlines()[0]
    assert line.endswith(";")
    return json.loads(line[:-1].replace("\\/", "/"))


def test_share_portal_is_offline_single_file_review(tmp_path):
    research_runtime, client, project_id, session_id = _completed_project(tmp_path)
    html = render_share_portal_for_project(research_runtime.projects_dir, project_id)
    assert html.startswith("<!doctype html>")
    assert 'lang="zh-CN"' in html
    assert "只读审查视图" in html
    assert "执行计划" in html
    assert "事件时间线" in html
    assert "决策记录" in html
    assert "产物清单" in html
    assert "审查边界" in html
    assert '<script src=' not in html
    assert '<link rel="stylesheet" href="http' not in html
    assert 'href="https://' not in html
    assert re.search(r"[0-9a-f]{64}", html)
    for token in ("C:\\", "D:\\", "/ho" + "me/", "/ro" + "ot/", "hy" + "wang", "STEP_API_KEY", "sk-"):
        assert token not in html
    data = _embedded_data(html)
    assert data["project_id"] == project_id
    assert data["status"] == "completed"
    assert len(data["_portal_fingerprint"]) == 64
    preview = data["previews"].get("research_report", {})
    assert preview.get("text", "").strip()
    assert preview.get("truncated") is False


def test_share_portal_from_package_matches_project_render(tmp_path):
    research_runtime, client, project_id, session_id = _completed_project(tmp_path)
    exported = client.get(f"/api/projects/{project_id}/export")
    assert exported.status_code == 200
    package = tmp_path / "portal-package.zip"
    package.write_bytes(exported.data)

    html_project = render_share_portal_for_project(research_runtime.projects_dir, project_id)
    html_package = render_share_portal_from_package(package)
    data_project = _embedded_data(html_project)
    data_package = _embedded_data(html_package)
    assert data_project["_portal_fingerprint"] == data_package["_portal_fingerprint"]
    assert data_package["previews"]["research_report"]["text"] == (
        data_project["previews"]["research_report"]["text"]
    )


def test_share_portal_web_route_serves_html(tmp_path):
    research_runtime, client, project_id, session_id = _completed_project(tmp_path)
    response = client.get(f"/api/projects/{project_id}/share")
    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert "只读审查视图" in response.get_data(as_text=True)


def test_viewer_session_is_read_only_and_ui_is_gated(tmp_path):
    research_runtime, client, project_id, session_id = _completed_project(tmp_path)
    created = client.post(
        f"/api/projects/{project_id}/sessions",
        json={"title": "只读会话", "role": "viewer"},
    )
    assert created.status_code == 201
    viewer_session = created.get_json()["session"]
    assert viewer_session["role"] == "viewer"

    blocked = client.post(
        f"/api/projects/{project_id}/sessions/{viewer_session['session_id']}/interventions",
        json={
            "action": "accept_checkpoint",
            "rationale": "试图审批",
            "actor": "viewer",
            "target_id": "any-target",
        },
    )
    assert blocked.status_code == 400
    assert "viewer" in blocked.get_json()["error"].lower()

    app_js = (
        Path(__file__).resolve().parents[1]
        / "src" / "target_agent" / "web" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "currentSessionRole" in app_js
    assert "data-role=" in app_js
    assert "只读会话：可提问与查看，不能审批、修复或补充输入。" in app_js


def test_portal_payload_scrubs_secrets_and_paths():
    payload = build_portal_payload({
        "spec": {
            "project_id": "project-x",
            "title": "scrub-check",
            "domain": "life_science",
            "autonomy_mode": "checkpointed",
            "context": {
                "api_key": "sk-abc123",
                "uri": "https://europepmc.org/article/MED/42123659",
                "note": "see " + "/ho" + "me/user/x and D:\\tmp\\y and /da" + "ta/foo/bar and a@b" + ".com",
                "credential": {"token": "tok-1234567890"},
            },
        },
        "state": {},
        "plan": {},
        "events": [],
        "plan_revisions": [],
        "plan_branches": [],
        "work_item_results": [],
        "assessments": [],
        "decisions": [],
        "repair_requests": [],
        "repair_resolutions": [],
        "review_targets": [],
        "artifacts": [],
        "artifact_versions": [],
        "domain_stage_summary": {},
        "next_actions": [],
        "active_work_item_ids": [],
        "active_artifact_ids": [],
    })
    text = json.dumps(payload, ensure_ascii=False)
    assert "sk-abc123" not in text
    assert "https://europepmc.org/article/MED/42123659" in text
    assert "tok-1234567890" not in text
    assert "/home/user" not in text
    assert "D:\\tmp" not in text
    assert "/data/foo/bar" not in text
    assert "a@b" + ".com" not in text
    assert "redacted" in text