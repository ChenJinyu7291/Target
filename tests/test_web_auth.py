from __future__ import annotations

from pydantic import SecretStr

from target_agent.webapp import create_app

from .test_research_runtime import fake_research_runtime
from .test_runtime import fake_runtime as fake_target_runtime


def _client(tmp_path, token=None):
    runtime = fake_target_runtime(tmp_path)
    if token is not None:
        runtime.settings.web_token = SecretStr(token)
    research_runtime, _ = fake_research_runtime(tmp_path)
    return create_app(runtime, research_runtime=research_runtime).test_client()


def test_api_requires_token_when_configured(tmp_path):
    client = _client(tmp_path, token="test-token")
    response = client.get("/api/capabilities")
    assert response.status_code == 401
    assert response.get_json() == {
        "error": "unauthorized",
        "detail": "authentication required",
    }


def test_api_401_does_not_leak_path_information(tmp_path):
    client = _client(tmp_path, token="test-token")
    response = client.get("/api/projects/project-secret/export")
    assert response.status_code == 401
    payload = response.get_json()
    assert payload["error"] == "unauthorized"
    assert "project-secret" not in payload["detail"]
    assert "export" not in payload["detail"]


def test_api_accepts_valid_bearer_token(tmp_path):
    client = _client(tmp_path, token="test-token")
    response = client.get("/api/capabilities", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.get_json()["auth"]["token_required"] is True


def test_api_rejects_wrong_token(tmp_path):
    client = _client(tmp_path, token="test-token")
    response = client.get(
        "/api/capabilities", headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_healthz_stays_public_with_token_enabled(tmp_path):
    client = _client(tmp_path, token="test-token")
    assert client.get("/healthz").status_code == 200


def test_token_auth_disabled_by_default(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/capabilities")
    assert response.status_code == 200
    assert response.get_json()["auth"]["token_required"] is False
