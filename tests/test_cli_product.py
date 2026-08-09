from __future__ import annotations

import json
import sys

import pytest

from target_agent import cli, secret_store
from target_agent.research_service import ResearchDecisionError, ResearchProjectNotFound


def _run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["target-agent", *argv])
    cli.main()


def test_up_runs_doctor_then_starts_workbench(monkeypatch, capsys, tmp_path):
    captured = {}

    def fake_doctor(settings):
        return {
            "required_dependencies": {name: True for name in ("flask", "waitress", "pydantic")},
            "settings": {
                "llm_configured": True,
                "projects_dir_writable": True,
            },
            "keyring": {"backend": "FakeKeyringBackend"},
        }

    def fake_start(settings, args):
        captured["port"] = args.port
        captured["host"] = args.host

    monkeypatch.setattr(cli, "_doctor", fake_doctor)
    monkeypatch.setattr(cli, "_start_workbench", fake_start)
    _run_cli(
        monkeypatch,
        "up", "--port", "8899", "--projects-dir", str(tmp_path / "projects"),
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["start"] == "up"
    assert payload["llm_configured"] is True
    assert payload["keyring_backend"] == "FakeKeyringBackend"
    assert captured == {"port": 8899, "host": "127.0.0.1"}


def test_up_fails_fast_when_required_dependency_missing(monkeypatch, tmp_path):
    def fake_doctor(settings):
        return {
            "required_dependencies": {
                "flask": True,
                "waitress": False,
                "pydantic": True,
            },
            "settings": {"llm_configured": True, "projects_dir_writable": True},
            "keyring": {"backend": None},
        }

    monkeypatch.setattr(cli, "_doctor", fake_doctor)

    def fail_if_started(settings, args):
        raise AssertionError("workbench must not start when required deps are missing")

    monkeypatch.setattr(cli, "_start_workbench", fail_if_started)
    with pytest.raises(SystemExit, match="waitress"):
        _run_cli(monkeypatch, "up", "--port", "8899", "--projects-dir", str(tmp_path / "projects"))


def test_secrets_status_cli(monkeypatch, capsys):
    monkeypatch.setattr(secret_store, "keyring_backend_name", lambda: "FakeKeyringBackend")
    monkeypatch.setattr(
        secret_store,
        "get_secret",
        lambda name: "configured-value" if name == "STEP_API_KEY" else None,
    )
    _run_cli(monkeypatch, "secrets", "status")
    payload = json.loads(capsys.readouterr().out)
    assert payload["backend"] == "FakeKeyringBackend"
    assert payload["secrets"]["STEP_API_KEY"] == "configured"
    assert payload["secrets"]["OPENAI_API_KEY"] == "not_configured"
    assert "configured-value" not in capsys.readouterr().out


def test_secrets_set_and_delete_cli(monkeypatch, capsys):
    stored = {}

    def fake_set(name, value):
        stored[name] = value
        return True

    def fake_delete(name):
        return stored.pop(name, None) is not None

    monkeypatch.setattr(secret_store, "set_secret", fake_set)
    monkeypatch.setattr(secret_store, "delete_secret", fake_delete)
    _run_cli(monkeypatch, "secrets", "set", "STEP_API_KEY", "--value", "key-456")
    assert json.loads(capsys.readouterr().out) == {"stored": True, "name": "STEP_API_KEY"}
    assert stored == {"STEP_API_KEY": "key-456"}
    _run_cli(monkeypatch, "secrets", "delete", "STEP_API_KEY")
    assert json.loads(capsys.readouterr().out) == {"deleted": True, "name": "STEP_API_KEY"}
    assert stored == {}
class _FakeRuntime:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeProjectService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []

    def branches(self, project_id):
        self.calls.append(("branches", project_id))
        return {"project_id": project_id, "fork_directives": [], "branches": []}

    def propose_fork(self, **kwargs):
        self.calls.append(("propose_fork", kwargs))
        return {"fork": kwargs}

    def decide_fork(self, **kwargs):
        self.calls.append(("decide_fork", kwargs))
        return {"decision": "accepted" if kwargs["approve"] else "rejected"}


class _FakeSessionService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []

    def create(self, project_id, title=None, *, role="researcher"):
        self.calls.append(("create", project_id, title, role))
        return {
            "session": {"session_id": "session-1", "project_id": project_id, "role": role},
            "messages": [],
        }

    def list(self, project_id):
        self.calls.append(("list", project_id))
        return {"project_id": project_id, "sessions": []}

    def messages(self, project_id, session_id):
        self.calls.append(("messages", project_id, session_id))
        return {"project_id": project_id, "session_id": session_id, "messages": []}

    def post_message(self, project_id, session_id, text, *, ask_agent=False, actor="researcher"):
        self.calls.append(("post_message", project_id, session_id, text, ask_agent, actor))
        return {"project_id": project_id, "session_id": session_id, "messages": []}

    def intervene(
        self,
        project_id,
        session_id,
        *,
        action,
        rationale,
        actor="researcher",
        target_id=None,
        approve=None,
        snapshot_digest=None,
        mode=None,
        rollback_to_attempt_id=None,
        input_overrides=None,
    ):
        self.calls.append((
            "intervene", project_id, session_id,
            {
                "action": action,
                "rationale": rationale,
                "actor": actor,
                "target_id": target_id,
                "approve": approve,
                "snapshot_digest": snapshot_digest,
                "mode": mode,
                "rollback_to_attempt_id": rollback_to_attempt_id,
                "input_overrides": input_overrides,
            },
        ))
        return {"intervened": True, "action": action}


def _patch_project_cli(monkeypatch):
    runtime = _FakeRuntime()
    service = _FakeProjectService(runtime)
    monkeypatch.setattr(cli, "ResearchProjectRuntime", lambda **kwargs: runtime)
    monkeypatch.setattr(cli, "ResearchProjectService", lambda runtime: service)
    return service


def _patch_session_cli(monkeypatch):
    runtime = _FakeRuntime()
    sessions = _FakeSessionService(runtime)
    monkeypatch.setattr(cli, "ResearchProjectRuntime", lambda **kwargs: runtime)
    monkeypatch.setattr(cli, "ResearchSessionService", lambda runtime: sessions)
    return sessions


def test_project_branches_cli(monkeypatch, capsys):
    service = _patch_project_cli(monkeypatch)
    _run_cli(monkeypatch, "project-branches", "--project-id", "project-abc")
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"project_id": "project-abc", "fork_directives": [], "branches": []}
    assert service.calls == [("branches", "project-abc")]


def test_project_fork_propose_cli(monkeypatch, capsys):
    service = _patch_project_cli(monkeypatch)
    _run_cli(
        monkeypatch,
        "project-fork-propose",
        "--project-id", "project-abc",
        "--target-work-item-id", "item-1",
        "--mode", "redo",
        "--rationale", "rerun with bounded inputs",
        "--actor", "researcher",
        "--input-overrides", '{"item-1": {"record_count": 2}}',
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["fork"]["mode"] == "redo"
    assert payload["fork"]["actor"] == "researcher"
    assert payload["fork"]["input_overrides"] == {"item-1": {"record_count": 2}}
    call = service.calls[0]
    assert call[0] == "propose_fork"
    assert call[1]["target_work_item_id"] == "item-1"
    assert call[1]["rollback_to_attempt_id"] is None


def test_project_fork_propose_invalid_overrides_json(monkeypatch, capsys):
    service = _patch_project_cli(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run_cli(
            monkeypatch,
            "project-fork-propose",
            "--project-id", "project-abc",
            "--target-work-item-id", "item-1",
            "--mode", "redo",
            "--rationale", "rerun",
            "--actor", "researcher",
            "--input-overrides", "{not-json",
        )
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"].startswith("input_overrides must be valid JSON")
    assert service.calls == []


def test_project_fork_decision_cli(monkeypatch, capsys):
    service = _patch_project_cli(monkeypatch)
    _run_cli(
        monkeypatch,
        "project-fork-decision",
        "--project-id", "project-abc",
        "--branch-id", "branch-1",
        "--no-approve",
        "--actor", "reviewer",
        "--rationale", "reject stale branch",
        "--resume",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"decision": "rejected"}
    call = service.calls[0]
    assert call[0] == "decide_fork"
    assert call[1]["branch_id"] == "branch-1"
    assert call[1]["approve"] is False
    assert call[1]["resume"] is True


def test_session_create_cli(monkeypatch, capsys):
    sessions = _patch_session_cli(monkeypatch)
    _run_cli(
        monkeypatch,
        "session", "create",
        "--project-id", "project-abc",
        "--title", "review session",
        "--role", "reviewer",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"]["role"] == "reviewer"
    assert sessions.calls == [("create", "project-abc", "review session", "reviewer")]


def test_session_list_and_read_cli(monkeypatch, capsys):
    sessions = _patch_session_cli(monkeypatch)
    _run_cli(monkeypatch, "session", "list", "--project-id", "project-abc")
    assert json.loads(capsys.readouterr().out) == {"project_id": "project-abc", "sessions": []}
    _run_cli(
        monkeypatch,
        "session", "read",
        "--project-id", "project-abc",
        "--session-id", "session-1",
    )
    assert json.loads(capsys.readouterr().out) == {
        "project_id": "project-abc", "session_id": "session-1", "messages": [],
    }
    assert sessions.calls == [("list", "project-abc"), ("messages", "project-abc", "session-1")]


def test_session_post_cli(monkeypatch, capsys):
    sessions = _patch_session_cli(monkeypatch)
    _run_cli(
        monkeypatch,
        "session", "post",
        "--project-id", "project-abc",
        "--session-id", "session-1",
        "--text", "what is the status?",
        "--ask-agent",
        "--actor", "admin",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "session-1"
    assert sessions.calls == [
        ("post_message", "project-abc", "session-1", "what is the status?", True, "admin")
    ]


def test_session_intervene_cli(monkeypatch, capsys):
    sessions = _patch_session_cli(monkeypatch)
    _run_cli(
        monkeypatch,
        "session", "intervene",
        "--project-id", "project-abc",
        "--session-id", "session-1",
        "--action", "propose_fork",
        "--rationale", "supplement input",
        "--actor", "researcher",
        "--target-id", "item-2",
        "--mode", "redo",
        "--input-overrides", '{"item-2": {"tissue": "colon"}}',
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["intervened"] is True
    call = sessions.calls[0]
    assert call[0] == "intervene"
    assert call[1] == "project-abc"
    assert call[2] == "session-1"
    assert call[3]["action"] == "propose_fork"
    assert call[3]["mode"] == "redo"
    assert call[3]["input_overrides"] == {"item-2": {"tissue": "colon"}}
    assert call[3]["approve"] is None


def test_session_error_exits_with_json_error(monkeypatch, capsys):
    sessions = _patch_session_cli(monkeypatch)

    def failing_messages(project_id, session_id):
        raise ResearchProjectNotFound("project not found")

    sessions.messages = failing_messages
    with pytest.raises(SystemExit) as exc:
        _run_cli(
            monkeypatch,
            "session", "read",
            "--project-id", "missing",
            "--session-id", "session-1",
        )
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "project not found"}


def test_session_decision_error_exits_with_json_error(monkeypatch, capsys):
    sessions = _patch_session_cli(monkeypatch)

    def failing_intervene(**kwargs):
        raise ResearchDecisionError("fork branch is already applied")

    sessions.intervene = failing_intervene
    with pytest.raises(SystemExit) as exc:
        _run_cli(
            monkeypatch,
            "session", "intervene",
            "--project-id", "project-abc",
            "--session-id", "session-1",
            "--action", "decide_fork",
            "--rationale", "approve",
            "--target-id", "branch-1",
            "--approve",
        )
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "fork branch is already applied"}
