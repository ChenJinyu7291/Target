"""Flask API and static single-page research workbench."""
from __future__ import annotations

import hmac
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from pydantic import ValidationError

from .contracts import CONTRACT_VERSION, TaskSpec, new_id
from .kernel import (
    KernelConfigError, KernelDisabledError, KernelForbiddenError, KernelManager,
    KernelNotConfiguredError, KernelNotFoundError, KernelTimeoutError, KernelUnavailableError,
)
from .legacy import parse_task_spec
from .research_contracts import (
    RESEARCH_CONTRACT_VERSION, ControlRequestKind, ProjectStatus, ResearchProjectSpec,
)
from .research_runtime import ResearchProjectRuntime
from .research_service import ResearchDecisionError, ResearchProjectNotFound, ResearchProjectService
from .research_session import ResearchSessionService
from .research_store import ResearchProjectStore
from .runtime import TargetDiscoveryRuntime


DEMO_CASES = (
    {
        "id": "luad", "run_id": "run-luad-v21-cached-3", "kind": "main",
        "title": "肺腺癌靶点发现", "subtitle": "完整证据链与TargetCard",
        "description": "动态GEO筛选、组学分析、遗传学/文献/药物融合与实验设计。",
    },
    {
        "id": "uc", "run_id": "run-uc-v21-cached-3", "kind": "boundary",
        "title": "UC可靠降级", "subtitle": "证据不足时不伪造结论",
        "description": "展示not_covered、context_mismatch和completed_with_gaps。",
    },
    {
        "id": "ad", "run_id": "run-ad-v21-cached-3", "kind": "generalization",
        "title": "阿尔茨海默病", "subtitle": "跨疾病冷启动案例",
        "description": "展示通用疾病标准化与公开数据发现能力。",
    },
)


class BoundedExecutor:
    def __init__(self, workers: int, queue_size: int):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="target-agent")
        self.capacity = threading.BoundedSemaphore(workers + queue_size)
        self.workers = workers
        self.queue_size = queue_size

    def submit(self, function, *args) -> bool:
        if not self.capacity.acquire(blocking=False):
            return False
        future = self.executor.submit(function, *args)
        future.add_done_callback(lambda _: self.capacity.release())
        return True


def create_app(
    runtime: TargetDiscoveryRuntime | None = None,
    research_runtime: ResearchProjectRuntime | None = None,
) -> Flask:
    if runtime is None:
        from .runtime_langgraph import LangGraphRuntime
        runtime = LangGraphRuntime()
    research_runtime = research_runtime or ResearchProjectRuntime(settings=runtime.settings)
    research_service = ResearchProjectService(research_runtime)
    session_service = ResearchSessionService(research_runtime)
    kernel_manager = KernelManager(runtime.settings)
    static_dir = Path(__file__).with_name("web") / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    pool = BoundedExecutor(runtime.settings.web_workers, runtime.settings.web_queue_size)

    @app.errorhandler(ValueError)
    def invalid_path(exc):
        app.logger.warning("request rejected with ValueError: %s", exc)
        return jsonify({"error": "invalid_request", "detail": "request could not be processed"}), 400

    @app.before_request
    def require_web_token():
        """Optional bearer-token gate for /api/*; /healthz stays public."""
        token = runtime.settings.web_token
        configured = token.get_secret_value() if token else ""
        if not configured or not request.path.startswith("/api/"):
            return None
        header = request.headers.get("Authorization", "")
        scheme, _, presented = header.partition(" ")
        presented = presented.strip() if scheme.lower() == "bearer" else ""
        if not presented or not hmac.compare_digest(
            presented.encode("utf-8"), configured.encode("utf-8")
        ):
            return jsonify({"error": "unauthorized", "detail": "authentication required"}), 401
        return None

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/healthz")
    def health():
        public = runtime.settings.public_summary()
        return jsonify({
            "status": "ok", "contract_version": CONTRACT_VERSION,
            "research_contract_version": RESEARCH_CONTRACT_VERSION,
            "service": {"status": "ok"},
            "database": {
                "kind": "filesystem_evidence_store",
                "status": "ok" if public["runs_dir_writable"] else "unavailable",
            },
            "cache": {"status": "ok" if public["cache_dir_writable"] else "unavailable"},
            "projects": {"status": "ok" if public["projects_dir_writable"] else "unavailable"},
            "executor": {"status": "ok", "workers": pool.workers, "queue_size": pool.queue_size},
        })

    @app.get("/api/capabilities")
    def capabilities():
        import importlib.util

        return jsonify({
            "contract_version": CONTRACT_VERSION,
            "research_contract_version": RESEARCH_CONTRACT_VERSION,
            "auth": {"token_required": bool(
                runtime.settings.web_token and runtime.settings.web_token.get_secret_value()
            )},
            "settings": runtime.settings.public_summary(),
            "tools": runtime.registry.public_capabilities(),
            "research_modules": research_runtime.registry.public_capabilities(),
            "skills": research_runtime.skill_catalog.public_summary(),
            "kernels": kernel_manager.capabilities(),
            "analysis_backends": {
                "pydeseq2": bool(importlib.util.find_spec("pydeseq2")),
                "gseapy": bool(importlib.util.find_spec("gseapy")),
                "scanpy_pseudobulk": bool(importlib.util.find_spec("scanpy")),
                "cellxgene_census": bool(importlib.util.find_spec("cellxgene_census")),
                "limma_declared": runtime.settings.enable_limma,
            },
            "limits": {
                "max_tool_calls": 30, "max_review_rounds": 2,
                "max_geo_candidates": 10, "max_datasets_to_analyze": 2,
                "max_cells": 100_000, "max_download_mb": 2048,
            },
        })

    @app.get("/api/workflows")
    def workflows_list():
        try:
            return jsonify({"workflows": research_service.workflow_templates()})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/skills")
    def skills_list():
        return jsonify(research_runtime.skill_catalog.public_summary())

    @app.get("/api/skills/<skill_id>")
    def skill_detail(skill_id: str):
        loaded = research_runtime.skill_catalog.load(skill_id)
        if loaded is None:
            return jsonify({"error": "skill not found"}), 404
        return jsonify(loaded)

    @app.get("/api/kernels")
    def kernels_list():
        return jsonify({
            "kernels": [info.to_dict() for info in kernel_manager.list()],
            "capabilities": kernel_manager.capabilities(),
        })

    @app.post("/api/kernels")
    def kernels_create():
        body = request.get_json(silent=True) or {}
        language = str(body.get("language") or "python")
        cwd = body.get("cwd")
        if not isinstance(cwd, str):
            cwd = None
        try:
            info = kernel_manager.create(language=language, cwd=cwd)
        except (KernelDisabledError, KernelNotConfiguredError, KernelConfigError) as exc:
            return jsonify({"error": exc.__class__.__name__, "detail": str(exc)}), 400
        except KernelForbiddenError as exc:
            return jsonify({"error": exc.__class__.__name__, "detail": str(exc)}), 403
        return jsonify(info.to_dict()), 201

    @app.get("/api/kernels/<kernel_id>")
    def kernels_get(kernel_id: str):
        try:
            return jsonify(kernel_manager.get(kernel_id).to_dict())
        except KernelNotFoundError as exc:
            return jsonify({"error": exc.__class__.__name__, "detail": str(exc)}), 404

    @app.post("/api/kernels/<kernel_id>/exec")
    def kernels_exec(kernel_id: str):
        body = request.get_json(silent=True) or {}
        code = body.get("code")
        if not isinstance(code, str) or not code.strip():
            return jsonify({"error": "invalid_code", "detail": "code must be a non-empty string"}), 400
        timeout = body.get("timeout")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            return jsonify({"error": "invalid_timeout", "detail": "timeout must be a positive number"}), 400
        try:
            result = kernel_manager.execute(kernel_id, code, timeout=timeout)
        except KernelNotFoundError as exc:
            return jsonify({"error": exc.__class__.__name__, "detail": str(exc)}), 404
        except KernelUnavailableError as exc:
            return jsonify({"error": exc.__class__.__name__, "detail": str(exc)}), 409
        except KernelTimeoutError as exc:
            return jsonify({"error": exc.__class__.__name__, "detail": str(exc)}), 408
        except KernelConfigError as exc:
            return jsonify({"error": exc.__class__.__name__, "detail": str(exc)}), 400
        return jsonify(result.to_dict())

    @app.delete("/api/kernels/<kernel_id>")
    def kernels_stop(kernel_id: str):
        try:
            return jsonify(kernel_manager.stop(kernel_id).to_dict())
        except KernelNotFoundError as exc:
            return jsonify({"error": exc.__class__.__name__, "detail": str(exc)}), 404

    @app.get("/api/diseases")
    def diseases():
        try:
            from .diseases import load_library

            library = load_library()
        except Exception as exc:
            return jsonify({"error": "disease library unavailable", "detail": exc.__class__.__name__}), 503
        return jsonify({
            "version": library.version,
            "template_kinds": sorted(library.task_templates),
            "diseases": [
                {
                    "id": entry.id, "name": entry.name, "name_zh": entry.name_zh,
                    "ontology_id": entry.ontology_id, "category": entry.category,
                    "reference_target_count": len(entry.reference_targets),
                    "tissue": entry.context.tissue, "cell_type": entry.context.cell_type,
                }
                for entry in library.diseases
            ],
        })

    @app.get("/api/demo/cases")
    def demo_cases():
        cases = []
        for configured in DEMO_CASES:
            item = {key: value for key, value in configured.items() if key != "run_id"}
            run_dir = _safe_run_dir(runtime.runs_dir, configured["run_id"])
            status = _read_json(run_dir / "status.json")
            available = bool(status and (run_dir / "report.json").is_file())
            item["available"] = available
            item["recommended"] = configured["id"] == "luad"
            if available:
                item["run_id"] = configured["run_id"]
                item["terminal_status"] = status.get("terminal_status")
            cases.append(item)
        return jsonify({"contract_version": CONTRACT_VERSION, "cases": cases})

    @app.post("/api/runs")
    def create_run():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        try:
            task = parse_task_spec(payload)
        except ValidationError as exc:
            return jsonify({"error": "invalid TaskSpec", "detail": exc.errors(include_url=False)}), 400
        except ValueError as exc:
            return jsonify({"error": "invalid TaskSpec", "detail": str(exc)}), 400
        run_id = new_id("run")

        def worker() -> None:
            try:
                runtime.run(task, run_id=run_id)
            except Exception as exc:
                run_dir = runtime.runs_dir / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "status.json").write_text(json.dumps({
                    "contract_version": CONTRACT_VERSION, "run_id": run_id, "task_id": task.task_id,
                    "state": "terminal", "terminal_status": "failed", "detail": {"error": exc.__class__.__name__},
                }), encoding="utf-8")

        if not pool.submit(worker):
            return jsonify({"error": "run queue is full", "retryable": True}), 429
        return jsonify({"run_id": run_id, "status_url": f"/api/runs/{run_id}"}), 202

    @app.get("/api/runs/<run_id>")
    def get_run(run_id: str):
        path = _safe_run_dir(runtime.runs_dir, run_id) / "status.json"
        if not path.exists():
            return jsonify({"error": "run not found"}), 404
        return send_file(path, mimetype="application/json")

    @app.get("/api/runs/<run_id>/events")
    def events(run_id: str):
        run_dir = _safe_run_dir(runtime.runs_dir, run_id)
        if not run_dir.is_dir():
            return jsonify({"error": "run not found"}), 404

        def stream():
            # Read only the bytes appended since the last tick (byte cursor);
            # idle is capped so abandoned streams do not hold a worker forever.
            offset = 0
            idle = 0
            while idle < 30:
                trace_path = run_dir / "trace.jsonl"
                new_lines: list[str] = []
                if trace_path.exists():
                    with trace_path.open(encoding="utf-8") as fh:
                        end = fh.seek(0, 2)
                        if offset > end:
                            offset = 0  # trace was truncated/rotated; restart
                        fh.seek(offset)
                        new_lines = [line.rstrip("\r\n") for line in fh]
                        offset = fh.tell()
                for line in new_lines:
                    yield f"data: {line}\n\n"
                idle = 0 if new_lines else idle + 1
                status_path = run_dir / "status.json"
                if status_path.exists():
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    if status.get("terminal_status"):
                        yield f"event: terminal\ndata: {json.dumps(status, ensure_ascii=False)}\n\n"
                        break
                # Heartbeat keeps proxies from closing idle connections while
                # the stream is waiting for new trace lines.
                yield ": heartbeat\n\n"
                time.sleep(1)

        return Response(stream(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    @app.get("/api/runs/<run_id>/bundle")
    def bundle(run_id: str):
        run_dir = _safe_run_dir(runtime.runs_dir, run_id)
        if not run_dir.is_dir():
            return jsonify({"error": "run not found"}), 404
        report_payload = _read_json(run_dir / "report.json")
        status_payload = _read_json(run_dir / "status.json")
        plan_payload = _read_json(run_dir / "execution_plan.json")
        if not report_payload or not status_payload or not plan_payload:
            return jsonify({"error": "run bundle is not ready"}), 409
        return jsonify(_build_public_bundle(
            run_id, status_payload, report_payload, plan_payload,
            _read_json(run_dir / "target_cards.json") or [],
            _read_jsonl(run_dir / "tool_results.jsonl"),
            _read_jsonl(run_dir / "evidence_items.jsonl"),
            _read_jsonl(run_dir / "trace.jsonl"),
        ))

    @app.get("/api/runs/<run_id>/report")
    def report(run_id: str):
        path = _safe_run_dir(runtime.runs_dir, run_id) / "report.md"
        if not path.exists():
            return jsonify({"error": "report not ready"}), 404
        return send_file(path, mimetype="text/markdown; charset=utf-8", as_attachment=True, download_name=f"{run_id}-report.md")

    @app.get("/api/runs/<run_id>/artifacts/<name>")
    def artifact(run_id: str, name: str):
        if Path(name).name != name:
            return jsonify({"error": "invalid artifact name"}), 400
        path = _safe_run_dir(runtime.runs_dir, run_id) / name
        if not path.is_file():
            return jsonify({"error": "artifact not found"}), 404
        return send_file(path, as_attachment=name.endswith((".md", ".json", ".jsonl", ".csv")))

    @app.post("/api/questions")
    def draft_project_from_question():
        """Turn a natural-language question into a reviewable draft spec (never executes)."""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("question"), str) or not payload["question"].strip():
            return jsonify({"error": "request body must include a non-empty question string"}), 400
        hints = payload.get("hints")
        if hints is not None and not isinstance(hints, dict):
            return jsonify({"error": "hints must be a JSON object"}), 400
        from .llm import StepClient
        from .question_intake import QuestionNeedsInput, build_draft

        try:
            draft = build_draft(
                payload["question"].strip(),
                hints=hints or {},
                client=StepClient.from_settings(runtime.settings),
                project_id=payload.get("project_id"),
            )
        except QuestionNeedsInput as exc:
            return jsonify({"error": "question needs input", "review_notes": str(exc)}), 422
        return jsonify(draft.model_dump(mode="json")), 200
    @app.post("/api/projects")
    def create_project():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        try:
            project = ResearchProjectSpec.model_validate(payload)
        except ValidationError as exc:
            return jsonify({"error": "invalid ResearchProjectSpec", "detail": exc.errors(include_url=False)}), 400
        try:
            reserved = research_service.reserve(project)["created"]
        except ValueError as exc:
            return jsonify({"error": "project id conflicts with an immutable project", "detail": str(exc)}), 409
        if not reserved:
            return jsonify({"error": "project id already exists"}), 409

        def project_worker() -> None:
            try:
                research_runtime.run(project)
            except Exception as exc:
                failed_store = ResearchProjectStore(research_runtime.projects_dir, project.project_id)
                from .research_contracts import ProjectState, ProjectStatus
                current = failed_store.load_state()
                terminal = current and current.status in {
                    ProjectStatus.COMPLETED, ProjectStatus.COMPLETED_WITH_GAPS,
                    ProjectStatus.FAILED, ProjectStatus.CANCELLED,
                }
                if not terminal:
                    failed_store.save_state(ProjectState(
                        project_id=project.project_id, status=ProjectStatus.FAILED,
                        attempts=current.attempts if current else {},
                        terminal_reason=f"Unhandled project runtime error: {exc.__class__.__name__}",
                    ))
                    failed_store.append_event("project_terminal", "failed", detail={"error": exc.__class__.__name__})

        if not pool.submit(project_worker):
            return jsonify({"error": "project queue is full", "retryable": True}), 429
        return jsonify({
            "project_id": project.project_id,
            "status_url": f"/api/projects/{project.project_id}",
            "events_url": f"/api/projects/{project.project_id}/events",
        }), 202

    @app.get("/api/projects")
    def list_projects():
        try:
            return jsonify({"projects": research_service.list_projects()})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/projects/<project_id>/sessions")
    def create_session(project_id: str):
        project_id = _safe_project_id(project_id)
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(session_service.create(
                project_id,
                title=payload.get("title"),
                role=str(payload.get("role") or "researcher"),
            )), 201
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/projects/<project_id>/sessions")
    def list_sessions(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            return jsonify(session_service.list(project_id))
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404

    @app.get("/api/projects/<project_id>/sessions/<session_id>")
    def read_session(project_id: str, session_id: str):
        project_id = _safe_project_id(project_id)
        if not session_id or Path(session_id).name != session_id:
            return jsonify({"error": "invalid session id"}), 400
        try:
            return jsonify(session_service.messages(project_id, session_id))
        except ResearchProjectNotFound:
            return jsonify({"error": "session or project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/projects/<project_id>/sessions/<session_id>/messages")
    def post_session_message(project_id: str, session_id: str):
        project_id = _safe_project_id(project_id)
        if not session_id or Path(session_id).name != session_id:
            return jsonify({"error": "invalid session id"}), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        text = str(payload.get("text") or "").strip()
        ask_agent = bool(payload.get("ask_agent", False))
        actor = str(payload.get("actor") or "researcher").strip()
        if not text:
            return jsonify({"error": "text is required"}), 400
        try:
            return jsonify(session_service.post_message(
                project_id, session_id, text, ask_agent=ask_agent, actor=actor,
            ))
        except ResearchProjectNotFound:
            return jsonify({"error": "session or project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/projects/<project_id>/sessions/<session_id>/interventions")
    def post_session_intervention(project_id: str, session_id: str):
        project_id = _safe_project_id(project_id)
        if not session_id or Path(session_id).name != session_id:
            return jsonify({"error": "invalid session id"}), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        action = str(payload.get("action") or "").strip()
        rationale = str(payload.get("rationale") or "").strip()
        actor = str(payload.get("actor") or "researcher").strip()
        target_id = str(payload.get("target_id") or "").strip() or None
        approve = payload.get("approve")
        snapshot_digest = str(payload.get("snapshot_digest") or "").strip() or None
        mode = str(payload.get("mode") or "").strip() or None
        rollback_to_attempt_id = str(payload.get("rollback_to_attempt_id") or "").strip() or None
        input_overrides = payload.get("input_overrides")
        if input_overrides is not None and not isinstance(input_overrides, dict):
            return jsonify({"error": "input_overrides must be a JSON object"}), 400
        if not action or not rationale or not target_id:
            return jsonify({"error": "action, rationale and target_id are required"}), 400
        try:
            result = session_service.intervene(
                project_id=project_id,
                session_id=session_id,
                action=action,
                rationale=rationale,
                actor=actor,
                target_id=target_id,
                approve=approve,
                snapshot_digest=snapshot_digest,
                mode=mode,
                rollback_to_attempt_id=rollback_to_attempt_id,
                input_overrides=input_overrides,
            )
        except ResearchProjectNotFound:
            return jsonify({"error": "session or project not found"}), 404
        except ResearchDecisionError as exc:
            status = 409 if "stale" in str(exc).lower() else 400
            return jsonify({"error": str(exc)}), status
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        resume_needed = action == "accept_checkpoint" or (
            approve is True and action in {"decide_repair", "decide_fork"}
        )
        queued = False
        if resume_needed:
            store = ResearchProjectStore(research_runtime.projects_dir, project_id)
            spec = store.load_spec()
            assert spec is not None

            def resume_session_worker() -> None:
                try:
                    research_runtime.run(spec, resume=True)
                except Exception:
                    return

            queued = pool.submit(resume_session_worker)
        return jsonify({
            **result,
            "decision_persisted": True,
            "resume_queued": queued,
            "status_url": f"/api/projects/{project_id}",
        }), 202
    def _control_payload() -> tuple[str, str]:
        payload = request.get_json(silent=True) or {}
        actor = str(payload.get("actor") or "researcher").strip()
        rationale = str(payload.get("rationale") or "").strip()
        return actor, rationale

    @app.post("/api/projects/<project_id>/pause")
    def pause_project(project_id: str):
        project_id = _safe_project_id(project_id)
        actor, rationale = _control_payload()
        rationale = rationale or "Paused from the web workbench."
        try:
            result = research_service.pause_project(
                project_id=project_id, actor=actor, rationale=rationale,
            )
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ResearchDecisionError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result), 202

    @app.post("/api/projects/<project_id>/cancel")
    def cancel_project(project_id: str):
        project_id = _safe_project_id(project_id)
        actor, rationale = _control_payload()
        rationale = rationale or "Cancelled from the web workbench."
        try:
            result = research_service.cancel_project(
                project_id=project_id, actor=actor, rationale=rationale,
            )
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ResearchDecisionError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(result), 202

    @app.post("/api/projects/<project_id>/resume")
    def resume_project(project_id: str):
        project_id = _safe_project_id(project_id)
        actor, rationale = _control_payload()
        rationale = rationale or "Resumed from the web workbench."
        store = ResearchProjectStore(research_runtime.projects_dir, project_id)
        if store.load_spec() is None:
            return jsonify({"error": "project not found"}), 404
        pending = store.load_control()
        if pending is not None and pending.request != ControlRequestKind.NONE:
            return jsonify({
                "error": f"a {pending.request.value} control request is pending; resolve it before resuming",
            }), 409

        def resume_worker() -> None:
            try:
                research_service.resume_project(
                    project_id=project_id, actor=actor, rationale=rationale,
                )
            except Exception:
                return

        queued = pool.submit(resume_worker)
        return jsonify({
            "project_id": project_id,
            "resume_queued": queued,
            "status_url": f"/api/projects/{project_id}",
        }), 202

    @app.get("/api/projects/<project_id>")
    def get_project(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            payload = research_service.snapshot(project_id)
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        return jsonify(payload)

    @app.get("/api/projects/<project_id>/share")
    def share_project_portal(project_id: str):
        project_id = _safe_project_id(project_id)
        from .share_portal import render_share_portal_for_project

        html = render_share_portal_for_project(research_runtime.projects_dir, project_id)
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.get("/api/projects/<project_id>/export")
    def export_project_package(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            from .project_package import export_project_bytes

            data, summary = export_project_bytes(research_runtime.projects_dir, project_id)
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return Response(
            data,
            mimetype="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{project_id}.target-project.zip"'
                ),
                "X-Project-File-Count": str(summary["file_count"]),
                "X-Project-Total-Bytes": str(summary["total_bytes"]),
            },
        )

    @app.get("/api/projects/<project_id>/graph")
    def project_graph(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            payload = research_service.evidence_graph(project_id)
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(payload)

    @app.get("/api/projects/<project_id>/mechanism-graph")
    def project_mechanism_graph(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            payload = research_service.mechanism_graph(project_id)
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(payload)

    @app.get("/api/projects/<project_id>/files")
    def project_files(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            payload = research_service.project_files(project_id)
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(payload)

    @app.get("/api/projects/<project_id>/files/preview")
    def project_file_preview(project_id: str):
        project_id = _safe_project_id(project_id)
        rel_path = request.args.get("path", "")
        try:
            payload = research_service.preview_file(project_id, rel_path)
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(payload)

    @app.get("/api/projects/<project_id>/events")
    def project_events(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            after_sequence = int(request.args.get("after_sequence", "0"))
            events = research_service.events(project_id, after_sequence=after_sequence)
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({
            "events": events,
            "next_cursor": events[-1]["sequence"] if events else after_sequence,
        })

    @app.get("/api/projects/<project_id>/activities")
    def project_domain_activities(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            page = research_service.domain_activities(
                project_id,
                after_sequence=int(request.args.get("after_sequence", "0")),
                limit=int(request.args.get("limit", "200")),
                work_item_id=request.args.get("work_item_id"),
            )
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(page)

    @app.get("/api/projects/<project_id>/repairs")
    def project_repairs(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            return jsonify(research_service.repairs(project_id))
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404

    @app.post("/api/projects/<project_id>/repairs/<repair_request_id>/decision")
    def decide_project_repair(project_id: str, repair_request_id: str):
        project_id = _safe_project_id(project_id)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        actor = str(payload.get("actor") or "").strip()
        rationale = str(payload.get("rationale") or "").strip()
        snapshot_digest = str(payload.get("trigger_snapshot_digest") or "").strip()
        approve = payload.get("approve")
        if not actor or not rationale or not snapshot_digest or not isinstance(approve, bool):
            return jsonify({
                "error": "actor, rationale, trigger_snapshot_digest and boolean approve are required"
            }), 400
        try:
            decided = research_service.decide_repair(
                project_id=project_id,
                repair_request_id=repair_request_id,
                trigger_snapshot_digest=snapshot_digest,
                approve=approve,
                actor=actor,
                rationale=rationale,
                resume=False,
            )
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ResearchDecisionError as exc:
            status = 409 if "stale" in str(exc).lower() else 400
            return jsonify({"error": str(exc)}), status
        store = ResearchProjectStore(research_runtime.projects_dir, project_id)
        spec = store.load_spec()
        assert spec is not None

        def resume_repair_worker() -> None:
            try:
                research_runtime.run(spec, resume=True)
            except Exception:
                return

        queued = pool.submit(resume_repair_worker)
        return jsonify({
            "decision": decided["decision"],
            "decision_persisted": True,
            "resume_queued": queued,
            "status_url": f"/api/projects/{project_id}",
        }), 202

    @app.post("/api/projects/<project_id>/forks")
    def propose_project_fork(project_id: str):
        project_id = _safe_project_id(project_id)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        target_work_item_id = str(payload.get("target_work_item_id") or "").strip()
        mode = str(payload.get("mode") or "").strip()
        rationale = str(payload.get("rationale") or "").strip()
        actor = str(payload.get("actor") or "").strip()
        rollback_to_attempt_id = str(payload.get("rollback_to_attempt_id") or "").strip() or None
        input_overrides = payload.get("input_overrides")
        if not target_work_item_id or not mode or not rationale or not actor:
            return jsonify({
                "error": "target_work_item_id, mode, rationale and actor are required"
            }), 400
        if input_overrides is not None and not isinstance(input_overrides, dict):
            return jsonify({"error": "input_overrides must be a JSON object"}), 400
        try:
            proposed = research_service.propose_fork(
                project_id=project_id,
                target_work_item_id=target_work_item_id,
                mode=mode,
                rationale=rationale,
                actor=actor,
                rollback_to_attempt_id=rollback_to_attempt_id,
                input_overrides=input_overrides or None,
            )
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except (ResearchDecisionError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"fork": proposed}), 202

    @app.get("/api/projects/<project_id>/branches")
    def project_branches(project_id: str):
        project_id = _safe_project_id(project_id)
        try:
            return jsonify(research_service.branches(project_id))
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404

    @app.post("/api/projects/<project_id>/forks/<branch_id>/decision")
    def decide_project_fork(project_id: str, branch_id: str):
        project_id = _safe_project_id(project_id)
        if not branch_id or not branch_id.startswith("branch-"):
            return jsonify({"error": "invalid branch id"}), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        actor = str(payload.get("actor") or "").strip()
        rationale = str(payload.get("rationale") or "").strip()
        approve = payload.get("approve")
        if not actor or not rationale or not isinstance(approve, bool):
            return jsonify({"error": "actor, rationale and boolean approve are required"}), 400
        try:
            decided = research_service.decide_fork(
                project_id=project_id,
                branch_id=branch_id,
                approve=approve,
                actor=actor,
                rationale=rationale,
                resume=False,
            )
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ResearchDecisionError as exc:
            return jsonify({"error": str(exc)}), 400
        store = ResearchProjectStore(research_runtime.projects_dir, project_id)
        spec = store.load_spec()
        assert spec is not None

        def resume_fork_worker() -> None:
            try:
                research_runtime.run(spec, resume=True)
            except Exception:
                return

        queued = pool.submit(resume_fork_worker)
        return jsonify({
            "decision": decided["decision"],
            "decision_persisted": True,
            "resume_queued": queued,
            "status_url": f"/api/projects/{project_id}",
        }), 202

    @app.post("/api/projects/<project_id>/decisions")
    def accept_project_checkpoint(project_id: str):
        project_id = _safe_project_id(project_id)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        target_id = str(payload.get("target_id") or "").strip()
        actor = str(payload.get("actor") or "").strip()
        rationale = str(payload.get("rationale") or "").strip()
        if not target_id or not actor or not rationale:
            return jsonify({"error": "target_id, actor and rationale are required"}), 400
        try:
            accepted = research_service.accept_checkpoint(
                project_id=project_id,
                target_id=target_id,
                actor=actor,
                rationale=rationale,
                resume=False,
            )
        except ResearchProjectNotFound:
            return jsonify({"error": "project not found"}), 404
        except ResearchDecisionError as exc:
            return jsonify({"error": str(exc)}), 400
        decision = accepted["decision"]
        store = ResearchProjectStore(research_runtime.projects_dir, project_id)
        spec = store.load_spec()
        assert spec is not None

        def resume_worker() -> None:
            try:
                research_runtime.run(spec, resume=True)
            except Exception:
                return

        queued = pool.submit(resume_worker)
        return jsonify({
            "decision": decision,
            "decision_persisted": True,
            "resume_queued": queued,
            "status_url": f"/api/projects/{project_id}",
        }), 202

    @app.get("/api/projects/<project_id>/artifacts/<artifact_id>")
    def project_artifact(project_id: str, artifact_id: str):
        project_id = _safe_project_id(project_id)
        if not artifact_id or Path(artifact_id).name != artifact_id:
            return jsonify({"error": "invalid artifact id"}), 400
        store = ResearchProjectStore(research_runtime.projects_dir, project_id)
        record = next((row for row in store.read_artifacts() if row.artifact_id == artifact_id), None)
        if record is None:
            return jsonify({"error": "artifact not found"}), 404
        try:
            store.assert_integrity()
        except ValueError:
            return jsonify({"error": "project integrity check failed; artifact was not served"}), 409
        path = store.artifact_path(record)
        if not path.is_file():
            return jsonify({"error": "artifact content is unavailable"}), 410
        return send_file(path, mimetype=record.media_type, as_attachment=True,
                         download_name=Path(path).name)

    return app


def _safe_run_dir(root: Path, run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or not run_id.startswith("run-"):
        raise ValueError("invalid run id")
    root = root.resolve()
    candidate = (root / run_id).resolve()
    if root not in candidate.parents:
        raise ValueError("run path escaped root")
    return candidate


def _safe_project_id(project_id: str) -> str:
    if not project_id or Path(project_id).name != project_id or not project_id.startswith("project-"):
        raise ValueError("invalid project id")
    return project_id


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def _public_value(value):
    blocked_keys = {"tool_run_id", "event_id", "job_id", "run_dir", "cache_dir", "absolute_path"}
    if isinstance(value, dict):
        return {key: _public_value(item) for key, item in value.items() if key not in blocked_keys}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith("/") or (len(value) > 2 and value[1] == ":" and value[2] in "\\/"):
            return "[server artifact]"
    return value


def _build_public_bundle(
    run_id: str, status: dict, report: dict, plan: dict,
    cards: list[dict], tools: list[dict], evidence: list[dict], trace: list[dict],
) -> dict:
    source_contract_version = (
        status.get("contract_version")
        or report.get("contract_version")
        or plan.get("contract_version")
        or CONTRACT_VERSION
    )
    claim_classes: dict[str, int] = {}
    genes: dict[str, int] = {}
    for item in evidence:
        claim_class = item.get("claim_class", "UNKNOWN")
        claim_classes[claim_class] = claim_classes.get(claim_class, 0) + 1
        gene = item.get("gene_symbol")
        if gene:
            genes[gene] = genes.get(gene, 0) + 1
    highlighted = set(report.get("highlighted_targets", []))
    selected_evidence = [item for item in evidence if item.get("gene_symbol") in highlighted][:24]
    if not selected_evidence:
        selected_evidence = evidence[:24]

    return _public_value({
        "contract_version": source_contract_version,
        "source_contract_version": source_contract_version,
        "rendered_contract_version": CONTRACT_VERSION,
        "run": {
            "run_id": run_id,
            "terminal_status": status.get("terminal_status"),
            "state": status.get("state"),
            "detail": status.get("detail", {}),
        },
        "question": report.get("question"),
        "context": report.get("context", {}),
        "plan": {
            "planner_backend": plan.get("planner_backend"),
            "fallback_used": plan.get("fallback_used", False),
            "steps": [
                {
                    "step_id": step.get("step_id"), "name": step.get("name"),
                    "tool": step.get("tool"), "dependencies": step.get("dependencies", []),
                    "success_criteria": step.get("success_criteria", []),
                    "degradation_conditions": step.get("degradation_conditions", []),
                }
                for step in plan.get("steps", [])
            ],
        },
        "dataset_selection_trace": report.get("dataset_selection_trace", []),
        "ranking": report.get("ranked_targets", []),
        "highlighted_targets": report.get("highlighted_targets", []),
        "target_cards": cards,
        "reviewer_findings": report.get("reviewer_findings", []),
        "report_policy": report.get("report_policy", {}),
        "tools": [
            {
                "tool_name": item.get("tool_name"), "status": item.get("status"),
                "coverage_status": item.get("coverage_status"),
                "context_match_score": item.get("context_match_score"),
                "cached": bool(item.get("cached")), "elapsed_ms": item.get("elapsed_ms"),
                "warnings": item.get("warnings", []), "limitations": item.get("limitations", []),
            }
            for item in tools
        ],
        "evidence": {
            "total": len(evidence), "claim_classes": claim_classes,
            "genes": dict(sorted(genes.items(), key=lambda pair: (-pair[1], pair[0]))[:20]),
            "items": [
                {
                    "gene_symbol": item.get("gene_symbol"), "claim_class": item.get("claim_class"),
                    "statement": item.get("statement"), "source": item.get("source", {}),
                    "source_span": item.get("source_span"), "context": item.get("context", {}),
                    "stance": item.get("stance"), "uncertainty": item.get("uncertainty"),
                    "context_match_score": item.get("context_match_score"),
                }
                for item in selected_evidence
            ],
        },
        "trace": [
            {
                "event_type": item.get("event_type"), "state": item.get("state"),
                "detail": item.get("detail", {}), "created_at": item.get("created_at"),
            }
            for item in trace
        ],
    })
