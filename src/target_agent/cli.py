"""Command-line entry points for runs, diagnostics, schemas and the workbench."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import subprocess
from pathlib import Path

import yaml

from .alignment import generate
from .contracts import TaskContext, TaskSpec
from .legacy import parse_task_spec
from .llm import StepClient
from .paper_strategy import PatternStore
from .planner import Planner
from .research_contracts import ResearchProjectSpec
from .research_runtime import ResearchProjectRuntime
from .research_service import (
    ResearchDecisionError,
    ResearchProjectNotFound,
    ResearchProjectService,
)
from .research_session import ResearchSessionService
from .runtime import TargetDiscoveryRuntime
from .runtime_langgraph import LangGraphRuntime
from .schema_export import export_schemas
from .settings import Settings, load_settings
from .tools import default_registry
from .webapp import create_app


def load_task(path: Path) -> TaskSpec:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_task_spec(payload)


def load_research_project(path: Path) -> ResearchProjectSpec:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ResearchProjectSpec.model_validate(payload)


def _error_exit(exc: Exception) -> None:
    """Print a JSON error payload and exit non-zero (thin CLI adapter contract)."""
    print(json.dumps({"error": str(exc)}, ensure_ascii=False))
    raise SystemExit(1) from exc


def _parse_json_overrides(raw: str | None) -> dict | None:
    """Parse an optional --input-overrides JSON object into a dict."""
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("input_overrides must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("input_overrides must be a JSON object")
    return value


def _init_readme(spec: ResearchProjectSpec) -> str:
    project_id = spec.project_id
    return (
        f"# {spec.title}\n"
        "\n"
        "Research project scaffold created by target-agent init.\n"
        "\n"
        "## Question\n"
        f"{spec.goal.question}\n"
        "\n"
        "## Run\n"
        "\n"
        "1. Copy .env.example to .env and fill in your model key.\n"
        "2. target-agent project-run --input project.yaml\n"
        f"3. target-agent project-status --project-id {project_id}\n"
        "4. target-agent serve --port 8888   # open the workbench\n"
        "\n"
        "## Package\n"
        f"target-agent project-export --project-id {project_id} --output {project_id}.target-project.zip\n"
        "target-agent project-import --input <package>.zip   # on another machine\n"
        "\n"
        "## Honest boundary\n"
        "A released project is a research decision package, not a clinical recommendation.\n"
    )


def _doctor(settings: Settings) -> dict:
    from . import secret_store
    required = ["flask", "pydantic", "pydantic_settings", "requests", "waitress", "yaml"]
    optional = ["pydeseq2", "gseapy", "scanpy", "anndata", "cellxgene_census", "mcp"]
    registry = default_registry(settings)
    rscript = shutil.which("Rscript")
    limma_package = False
    if rscript:
        check = subprocess.run(
            [rscript, "-e", "quit(status=ifelse(requireNamespace('limma', quietly=TRUE),0,1))"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        limma_package = check.returncode == 0
    return {
        "settings": settings.public_summary(),
        "required_dependencies": {name: bool(importlib.util.find_spec(name)) for name in required},
        "optional_dependencies": {name: bool(importlib.util.find_spec(name)) for name in optional},
        "limma_backend": {
            "enabled": settings.enable_limma,
            "rscript_available": bool(rscript),
            "limma_package_available": limma_package,
            "ready": settings.enable_limma and bool(rscript) and limma_package,
        },
        "kernels": {
            "enabled": settings.kernel_enabled,
            "python": {
                "backend": sys.executable,
                "ready": True,
            },
            "r": {
                "backend": shutil.which(settings.kernel_r_bin or "Rscript"),
                "ready": bool(shutil.which(settings.kernel_r_bin or "Rscript")),
                "jsonlite_required": True,
            },
        },
        "enabled_tools": registry.names,
        "registry_contract_valid": True,
        "keyring": {
            "backend": secret_store.keyring_backend_name(),
            "secrets": {
                name: bool(secret_store.get_secret(name))
                for name in secret_store.SECRET_NAMES
            },
        },
    }


def _smoke_test(settings: Settings) -> dict:
    client = StepClient.from_settings(settings)
    if not client:
        raise SystemExit("Step API is not configured; provide it through an untracked env file or process environment")
    registry = default_registry(settings)
    planner = Planner(client, registry)
    task = TaskSpec(
        task_type="disease_to_target",
        question="Plan a traceable Alzheimer disease target-discovery workflow",
        context=TaskContext(disease="Alzheimer disease", tissue="brain"),
    )
    plan = planner.create_plan(task)
    if plan.fallback_used or not plan.planner_backend.startswith("step:"):
        raise SystemExit(f"Step structured-plan validation failed; backend={plan.planner_backend}")
    return {
        "configured": True, "schema_valid": True, "planner_backend": plan.planner_backend,
        "step_count": len(plan.steps), "provider_request": client.last_request_meta,
    }


def _start_workbench(settings: Settings, args: argparse.Namespace) -> None:
    runtime_cls = LangGraphRuntime if args.runtime == "langgraph" else TargetDiscoveryRuntime
    runtime = runtime_cls(runs_dir=args.runs_dir, cache_dir=args.cache_dir, settings=settings)
    research_runtime = ResearchProjectRuntime(
        projects_dir=args.projects_dir, cache_dir=args.cache_dir, settings=settings,
    )
    app = create_app(runtime, research_runtime)
    if args.dev:
        app.run(host=args.host, port=args.port, threaded=True)
    else:
        from waitress import serve as waitress_serve
        waitress_serve(app, host=args.host, port=args.port, threads=settings.web_workers)


def main() -> None:
    parser = argparse.ArgumentParser(prog="target-agent")
    parser.add_argument("--env-file", type=Path, help="Select a dotenv file; process environment remains authoritative")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run or resume an Agent case")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--run-id")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--runs-dir", type=Path)
    run.add_argument("--cache-dir", type=Path)
    run.add_argument("--runtime", choices=["legacy", "langgraph"], default="langgraph",
                     help="Execution engine; both are parity-tested and write identical artifacts")

    project_run = sub.add_parser("project-run", help="Run or resume a durable disease-target research project")
    project_run.add_argument("--input", type=Path, required=True)
    project_run.add_argument("--resume", action="store_true")
    project_run.add_argument("--projects-dir", type=Path)
    project_run.add_argument("--cache-dir", type=Path)

    project_status = sub.add_parser("project-status", help="Read durable project state without executing work")
    project_status.add_argument("--project-id", required=True)
    project_status.add_argument("--projects-dir", type=Path)

    project_pause = sub.add_parser("project-pause", help="Pause a durable project at the next safe work-item boundary")
    project_pause.add_argument("--project-id", required=True)
    project_pause.add_argument("--actor", required=True)
    project_pause.add_argument("--rationale", required=True)
    project_pause.add_argument("--projects-dir", type=Path)

    project_cancel = sub.add_parser("project-cancel", help="Cancel a durable project; no ranking/report/release is produced")
    project_cancel.add_argument("--project-id", required=True)
    project_cancel.add_argument("--actor", required=True)
    project_cancel.add_argument("--rationale", required=True)
    project_cancel.add_argument("--projects-dir", type=Path)

    project_resume = sub.add_parser("project-resume", help="Resume a paused durable project and advance until the next stop")
    project_resume.add_argument("--project-id", required=True)
    project_resume.add_argument("--actor", required=True)
    project_resume.add_argument("--rationale", required=True)
    project_resume.add_argument("--projects-dir", type=Path)

    project_approve = sub.add_parser("project-approve", help="Accept a frozen plan, supervised work item, or release gate")
    project_approve.add_argument("--project-id", required=True)
    project_approve.add_argument("--target-id", required=True,
                                 help="Plan id, work-item id, or release:<plan-id>")
    project_approve.add_argument("--actor", required=True)
    project_approve.add_argument("--rationale", required=True)
    project_approve.add_argument("--projects-dir", type=Path)
    project_approve.add_argument("--resume", action="store_true",
                                 help="Resume the project immediately after recording acceptance")

    project_repairs = sub.add_parser("project-repairs", help="Read the durable project repair queue")
    project_repairs.add_argument("--project-id", required=True)
    project_repairs.add_argument("--projects-dir", type=Path)

    repair_decision = sub.add_parser("project-repair-decision", help="Approve or reject one exact repair snapshot")
    repair_decision.add_argument("--project-id", required=True)
    repair_decision.add_argument("--repair-request-id", required=True)
    repair_decision.add_argument("--snapshot-digest", required=True)
    repair_decision.add_argument("--approve", action=argparse.BooleanOptionalAction, required=True)
    repair_decision.add_argument("--actor", required=True)
    repair_decision.add_argument("--rationale", required=True)
    repair_decision.add_argument("--projects-dir", type=Path)
    repair_decision.add_argument("--resume", action="store_true")

    project_branches = sub.add_parser("project-branches", help="Read the fork branch history and immutable directives for a project")
    project_branches.add_argument("--project-id", required=True)
    project_branches.add_argument("--projects-dir", type=Path)

    fork_propose = sub.add_parser("project-fork-propose", help="Propose a snapshot-bound redo/restore rollback branch")
    fork_propose.add_argument("--project-id", required=True)
    fork_propose.add_argument("--target-work-item-id", required=True)
    fork_propose.add_argument("--mode", choices=["redo", "restore"], required=True)
    fork_propose.add_argument("--rationale", required=True)
    fork_propose.add_argument("--actor", required=True)
    fork_propose.add_argument("--rollback-to-attempt-id", help="Historical attempt to restore (restore mode only)")
    fork_propose.add_argument("--input-overrides", help="JSON object of per-work-item input overrides")
    fork_propose.add_argument("--projects-dir", type=Path)

    fork_decision = sub.add_parser("project-fork-decision", help="Approve or reject one immutable fork branch snapshot")
    fork_decision.add_argument("--project-id", required=True)
    fork_decision.add_argument("--branch-id", required=True)
    fork_decision.add_argument("--approve", action=argparse.BooleanOptionalAction, required=True)
    fork_decision.add_argument("--actor", required=True)
    fork_decision.add_argument("--rationale", required=True)
    fork_decision.add_argument("--projects-dir", type=Path)
    fork_decision.add_argument("--resume", action="store_true",
                               help="Resume the project immediately after recording the decision")

    session_cmd = sub.add_parser("session", help="Manage role-aware research sessions over one durable project")
    session_sub = session_cmd.add_subparsers(dest="session_command", required=True)
    session_create = session_sub.add_parser("create", help="Create a conversation view over one durable project")
    session_create.add_argument("--project-id", required=True)
    session_create.add_argument("--title", help="Session title")
    session_create.add_argument("--role", choices=["researcher", "reviewer", "admin", "viewer"], default="researcher")
    session_create.add_argument("--projects-dir", type=Path)
    session_list = session_sub.add_parser("list", help="List sessions and their message counts for one project")
    session_list.add_argument("--project-id", required=True)
    session_list.add_argument("--projects-dir", type=Path)
    session_read = session_sub.add_parser("read", help="Read all messages of one session")
    session_read.add_argument("--project-id", required=True)
    session_read.add_argument("--session-id", required=True)
    session_read.add_argument("--projects-dir", type=Path)
    session_post = session_sub.add_parser("post", help="Append a user message to one session")
    session_post.add_argument("--project-id", required=True)
    session_post.add_argument("--session-id", required=True)
    session_post.add_argument("--text", required=True)
    session_post.add_argument("--ask-agent", action="store_true",
                              help="Return a deterministic snapshot summary without mutating science state")
    session_post.add_argument("--actor", default="researcher")
    session_post.add_argument("--projects-dir", type=Path)
    session_intervene = session_sub.add_parser("intervene", help="Execute one structured control-plane action from inside a session")
    session_intervene.add_argument("--project-id", required=True)
    session_intervene.add_argument("--session-id", required=True)
    session_intervene.add_argument("--action", choices=["accept_checkpoint", "decide_repair", "decide_fork", "propose_fork"], required=True)
    session_intervene.add_argument("--rationale", required=True)
    session_intervene.add_argument("--actor", default="researcher")
    session_intervene.add_argument("--target-id", help="Plan/work-item/repair/branch id required by the action")
    session_intervene.add_argument("--approve", action=argparse.BooleanOptionalAction,
                                  help="Approve or reject a decide_repair/decide_fork action")
    session_intervene.add_argument("--snapshot-digest", help="Exact repair snapshot digest (decide_repair only)")
    session_intervene.add_argument("--mode", choices=["redo", "restore"], help="Fork mode (propose_fork only)")
    session_intervene.add_argument("--rollback-to-attempt-id", help="Historical attempt to restore (propose_fork restore only)")
    session_intervene.add_argument("--input-overrides", help="JSON object of per-work-item input overrides (propose_fork only)")
    session_intervene.add_argument("--projects-dir", type=Path)

    schemas = sub.add_parser("export-schemas", help="Export canonical Pydantic JSON Schemas")
    schemas.add_argument("--output", type=Path, default=Path("schemas"))

    alignment = sub.add_parser("generate-alignment", help="Generate review-gated alignment cases")
    alignment.add_argument("--output", type=Path, default=Path("alignment_data"))

    sub.add_parser("doctor", help="Check configuration and capabilities without printing secrets")
    sub.add_parser("llm-smoke-test", help="Make one real Step structured-planning request")

    pattern_cmd = sub.add_parser("pattern", help="Inspect and maintain the paper-strategy pattern library")
    pattern_sub = pattern_cmd.add_subparsers(dest="pattern_command", required=True)
    pattern_search = pattern_sub.add_parser("search", help="Retrieve strategy patterns for a disease and data availability")
    pattern_search.add_argument("--disease", required=True)
    pattern_search.add_argument("--query", default="")
    pattern_search.add_argument("--lanes", default="", help="Comma-separated available evidence lanes")
    pattern_search.add_argument("--top-k", type=int, default=5)
    pattern_search.add_argument("--store", type=Path)
    pattern_list = pattern_sub.add_parser("list", help="Show the pattern library summary")
    pattern_list.add_argument("--store", type=Path)
    pattern_add = pattern_sub.add_parser("add", help="Add one pattern from a JSON or YAML file")
    pattern_add.add_argument("--input", type=Path, required=True)
    pattern_add.add_argument("--store", type=Path)
    pattern_curate = pattern_sub.add_parser("curate", help="Mark a corpus PMID as gold or rejected for pattern extraction")
    pattern_curate.add_argument("--pmid", required=True)
    pattern_curate.add_argument("--status", choices=["gold", "rejected"], required=True)
    pattern_curate.add_argument("--rationale", required=True)
    pattern_curate.add_argument("--role", choices=["life_science", "engineering", "lead"], default="lead")
    pattern_curate.add_argument("--curation", type=Path)

    pattern_nominate = pattern_sub.add_parser("nominate", help="Deterministically rank corpus candidates as advisory gold-paper nominations")
    pattern_nominate.add_argument("--corpus", type=Path, default=Path("paper_strategy") / "corpus" / "corpus.jsonl")
    pattern_nominate.add_argument("--out", type=Path, help="Nomination JSONL output (default: settings pattern_nomination_path)")
    pattern_nominate.add_argument("--limit", type=int, default=40)
    pattern_nominate.add_argument("--min-score", type=float, default=0.0)
    pattern_nominate.add_argument("--year-min", type=int, default=2021)
    pattern_extract = pattern_sub.add_parser("extract", help="Distill gold corpus papers into validated strategy patterns")
    pattern_extract.add_argument("--pmids", default="", help="Comma-separated PMIDs; defaults to all gold records")
    pattern_extract.add_argument("--corpus", type=Path, default=Path("paper_strategy") / "corpus" / "corpus.jsonl")
    pattern_extract.add_argument("--curation", type=Path)
    pattern_extract.add_argument("--store", type=Path)
    pattern_extract.add_argument("--audit", type=Path)

    pattern_review = pattern_sub.add_parser("review", help="Append an expert review decision for a pattern")
    pattern_review.add_argument("--pattern-id", required=True)
    pattern_review.add_argument("--role", choices=["life_science", "engineering"], required=True)
    pattern_review.add_argument("--status", choices=["approved", "rejected"], required=True)
    pattern_review.add_argument("--ledger", type=Path)
    corpus_cmd = pattern_sub.add_parser("corpus", help="Maintain the PubMed candidate corpus for pattern distillation")
    corpus_sub = corpus_cmd.add_subparsers(dest="paper_corpus_command", required=True)
    corpus_refresh = corpus_sub.add_parser("refresh", help="Fetch and append E-utilities candidate records")
    corpus_refresh.add_argument("--store", type=Path, default=Path("paper_strategy") / "corpus" / "corpus.jsonl")
    corpus_refresh.add_argument("--email", default="", help="NCBI E-utilities contact email (or NCBI_EMAIL env)")
    corpus_refresh.add_argument("--retmax", type=int, default=8, help="Records per journal per query bucket")
    corpus_refresh.add_argument("--max-candidates", type=int, default=200)
    corpus_refresh.add_argument("--year-min", type=int, default=2021)
    corpus_refresh.add_argument("--year-max", type=int, default=2026)
    corpus_status = corpus_sub.add_parser("status", help="Show candidate corpus counts")
    corpus_status.add_argument("--store", type=Path, default=Path("paper_strategy") / "corpus" / "corpus.jsonl")

    rag_cmd = pattern_sub.add_parser("rag", help="Maintain the paper-abstract RAG store for planner few-shot")
    rag_sub = rag_cmd.add_subparsers(dest="paper_rag_command", required=True)
    rag_refresh = rag_sub.add_parser("refresh", help="Fetch and append bounded abstracts for corpus PMIDs")
    rag_refresh.add_argument("--store", type=Path, default=Path("paper_strategy") / "rag" / "chunks.jsonl")
    rag_refresh.add_argument("--corpus", type=Path, default=Path("paper_strategy") / "corpus" / "corpus.jsonl")
    rag_refresh.add_argument("--pmids", default="", help="Comma-separated PMIDs; defaults to candidate records")
    rag_refresh.add_argument("--limit", type=int, default=0, help="Max papers to fetch (0 = all)")
    rag_refresh.add_argument("--chunk-size", type=int, default=700)
    rag_refresh.add_argument("--overlap", type=int, default=90)
    rag_search = rag_sub.add_parser("search", help="Retrieve paper-abstract chunks for planner context")
    rag_search.add_argument("--disease", required=True)
    rag_search.add_argument("--query", default="")
    rag_search.add_argument("--lanes", default="", help="Comma-separated available evidence lanes")
    rag_search.add_argument("--top-k", type=int, default=5)
    rag_search.add_argument("--store", type=Path, default=Path("paper_strategy") / "rag" / "chunks.jsonl")
    rag_status = rag_sub.add_parser("status", help="Show RAG store card")
    rag_status.add_argument("--store", type=Path, default=Path("paper_strategy") / "rag" / "chunks.jsonl")

    diseases_cmd = sub.add_parser("diseases", help="List the OLS-verified disease library")
    diseases_cmd.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    run_disease = sub.add_parser("run-disease", help="Run one or more library diseases by id/name/synonym")

    skills_cmd = sub.add_parser("skills", help="Inspect the on-demand best-practice skill catalog")
    skills_sub = skills_cmd.add_subparsers(dest="skills_command", required=True)
    skills_sub.add_parser("list", help="List available skills with capability summaries")
    skills_search = skills_sub.add_parser("search", help="Search skills by query/lane/scope")
    skills_search.add_argument("--query", default="")
    skills_search.add_argument("--lanes", default="", help="Comma-separated evidence lanes")
    skills_search.add_argument("--scopes", default="", help="Comma-separated scopes")
    skills_search.add_argument("--top-k", type=int, default=5)
    skills_show = skills_sub.add_parser("show", help="Print the full SKILL.md body for one skill")
    skills_show.add_argument("--id", required=True)

    workflows_cmd = sub.add_parser("workflows", help="List and inspect executable workflow templates")
    workflows_sub = workflows_cmd.add_subparsers(dest="workflow_command", required=True)
    workflows_sub.add_parser("list", help="List executable workflow templates")
    workflows_show = workflows_sub.add_parser("show", help="Show one executable workflow template")
    workflows_show.add_argument("--id", required=True)

    kernel_cmd = sub.add_parser("kernel", help="Manage persistent Python/R analysis kernels")
    kernel_sub = kernel_cmd.add_subparsers(dest="kernel_command", required=True)
    kernel_start = kernel_sub.add_parser("start", help="Start a persistent kernel")
    kernel_start.add_argument("--language", choices=["python", "r"], default="python")
    kernel_start.add_argument("--cwd", type=Path, help="Working directory (default: projects dir)")
    kernel_exec_cmd = kernel_sub.add_parser("exec", help="Execute code in a running kernel")
    kernel_exec_cmd.add_argument("--kernel-id", required=True)
    kernel_exec_cmd.add_argument("--code", required=True)
    kernel_exec_cmd.add_argument("--timeout", type=float, help="Override the execution timeout in seconds")
    kernel_status = kernel_sub.add_parser("status", help="Show one kernel or all kernels")
    kernel_status.add_argument("--kernel-id")
    kernel_stop = kernel_sub.add_parser("stop", help="Stop one kernel")
    kernel_stop.add_argument("--kernel-id", required=True)
    kernel_sub.add_parser("stop-all", help="Stop every running kernel")
    kernel_serve = kernel_sub.add_parser("serve", help="Run the localhost kernel daemon in the foreground")
    kernel_serve.add_argument("--port", type=int, default=None, help="Override TARGET_AGENT_KERNEL_PORT")
    run_disease.add_argument("--disease", required=True,
                             help="Comma-separated disease ids, names or synonyms (e.g. uc,ra,ad)")
    run_disease.add_argument("--kind", choices=["normal", "missing_context", "conflicting_evidence", "trap"],
                             default="normal", help="Task template bucket from the disease library")
    run_disease.add_argument("--run-id", default="run-disease", help="Run-id prefix; each run is '<prefix>-<disease id>'")
    run_disease.add_argument("--runs-dir", type=Path)
    run_disease.add_argument("--cache-dir", type=Path)
    run_disease.add_argument("--runtime", choices=["legacy", "langgraph"], default="langgraph")
    run_disease.add_argument("--summary-out", type=Path, help="Optional JSON summary of the batch")

    serve = sub.add_parser("serve", help="Start the single-page research workbench with Waitress")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, required=True, help="Bind port supplied by the external deployment profile")
    serve.add_argument("--runs-dir", type=Path)
    serve.add_argument("--projects-dir", type=Path)
    serve.add_argument("--cache-dir", type=Path)
    serve.add_argument("--runtime", choices=["legacy", "langgraph"], default="langgraph")
    serve.add_argument("--dev", action="store_true", help="Use Flask's development server")

    up = sub.add_parser("up", help="One-command start: run capability checks, then serve the workbench")
    up.add_argument("--host", default="127.0.0.1")
    up.add_argument("--port", type=int, required=True, help="Bind port supplied by the external deployment profile")
    up.add_argument("--runs-dir", type=Path)
    up.add_argument("--projects-dir", type=Path)
    up.add_argument("--cache-dir", type=Path)
    up.add_argument("--runtime", choices=["legacy", "langgraph"], default="langgraph")
    up.add_argument("--dev", action="store_true", help="Use Flask's development server")

    secrets_cmd = sub.add_parser("secrets", help="Manage optional OS-keyring secrets; process env / .env still take priority")
    secrets_sub = secrets_cmd.add_subparsers(dest="secret_command", required=True)
    secrets_sub.add_parser("status", help="Show the keyring backend and which secrets are configured")
    secrets_set = secrets_sub.add_parser("set", help="Store one secret in the OS keyring")
    secrets_set.add_argument("name", help="Secret name, e.g. STEP_API_KEY")
    secrets_set.add_argument("--value", help="Secret value; if omitted, read from stdin")
    secrets_delete = secrets_sub.add_parser("delete", help="Delete one secret from the OS keyring")
    secrets_delete.add_argument("name", help="Secret name, e.g. STEP_API_KEY")

    ask_cmd = sub.add_parser("ask", help="Turn a natural-language research question into a reviewable project draft")
    ask_cmd.add_argument("--question", required=True)
    ask_cmd.add_argument("--disease", help="Authoritative hint; overrides extraction")
    ask_cmd.add_argument("--subtype")
    ask_cmd.add_argument("--tissue")
    ask_cmd.add_argument("--cell-type")
    ask_cmd.add_argument("--stage")
    ask_cmd.add_argument("--phenotype")
    ask_cmd.add_argument("--organism", default="Homo sapiens")
    ask_cmd.add_argument("--project-id")
    ask_cmd.add_argument("--autonomy", choices=["checkpointed", "autonomous", "supervised"], default="checkpointed")
    ask_cmd.add_argument("--output", type=Path, help="Write the draft spec as YAML")
    ask_cmd.add_argument("--create", action="store_true", help="Reserve the draft as an immutable project without executing it")
    init_cmd = sub.add_parser("init", help="Scaffold a durable target-research project workspace")
    init_cmd.add_argument("--output", type=Path, required=True)
    init_cmd.add_argument("--project-id")
    init_cmd.add_argument("--disease", help="Disease for target workflows; not required for literature_review")
    init_cmd.add_argument("--question", help="Defaults to a disease-to-target question")
    init_cmd.add_argument("--title")
    init_cmd.add_argument("--subtype")
    init_cmd.add_argument("--tissue")
    init_cmd.add_argument("--cell-type")
    init_cmd.add_argument("--stage")
    init_cmd.add_argument("--phenotype")
    init_cmd.add_argument("--organism", default="Homo sapiens")
    init_cmd.add_argument("--autonomy", choices=["checkpointed", "autonomous", "supervised"], default="checkpointed")
    init_cmd.add_argument("--workflow", default=None,
                          help="Executable workflow template id (default: legacy disease workflow; see `target-agent workflows list`)")

    export_cmd = sub.add_parser("project-export", help="Export a durable project to a portable zip package")
    export_cmd.add_argument("--project-id", required=True)
    export_cmd.add_argument("--output", type=Path)
    export_cmd.add_argument("--projects-dir", type=Path)

    import_cmd = sub.add_parser("project-import", help="Verify and import a portable project zip package")
    import_cmd.add_argument("--input", type=Path, required=True)
    import_cmd.add_argument("--projects-dir", type=Path)

    inspect_cmd = sub.add_parser("project-package-inspect", help="Show package metadata without importing")
    inspect_cmd.add_argument("--input", type=Path, required=True)

    share_cmd = sub.add_parser("share", help="Render a project or package into a read-only offline HTML review portal")
    share_cmd.add_argument("--project-id", default=None, help="Render a durable project by id")
    share_cmd.add_argument("--input", type=Path, default=None, help="Render a portable project package (.zip) instead of a live project")
    share_cmd.add_argument("--output", type=Path, required=True, help="Output single-file HTML path")
    share_cmd.add_argument("--projects-dir", type=Path)
    share_cmd.add_argument("--max-preview-bytes", type=int, default=65536, help="Max bytes of report/brief preview embedded in the page")

    mcp_serve = sub.add_parser(
        "mcp-serve",
        help="Expose durable Target project operations through MCP (stdio or streamable HTTP)",
    )
    mcp_serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio",
                           dest="mcp_transport")
    mcp_serve.add_argument("--host", default="127.0.0.1", dest="mcp_host")
    mcp_serve.add_argument("--port", type=int, default=8000, dest="mcp_port")
    mcp_serve.add_argument("--path", default="/mcp", dest="mcp_path")

    args = parser.parse_args()
    settings = load_settings(args.env_file)
    if args.command == "run":
        runtime_cls = LangGraphRuntime if args.runtime == "langgraph" else TargetDiscoveryRuntime
        runtime = runtime_cls(runs_dir=args.runs_dir, cache_dir=args.cache_dir, settings=settings)
        print(json.dumps(runtime.run(load_task(args.input), run_id=args.run_id, resume=args.resume), indent=2, ensure_ascii=False))
    elif args.command == "project-run":
        project_runtime = ResearchProjectRuntime(
            projects_dir=args.projects_dir, cache_dir=args.cache_dir, settings=settings,
        )
        print(json.dumps(project_runtime.run(load_research_project(args.input), resume=args.resume),
                         indent=2, ensure_ascii=False))
    elif args.command == "project-status":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        print(json.dumps(ResearchProjectService(runtime).snapshot(args.project_id), indent=2, ensure_ascii=False))
    elif args.command == "project-pause":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        try:
            result = ResearchProjectService(runtime).pause_project(
                project_id=args.project_id, actor=args.actor, rationale=args.rationale,
            )
        except (ResearchProjectNotFound, ResearchDecisionError, ValueError) as exc:
            _error_exit(exc)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "project-cancel":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        try:
            result = ResearchProjectService(runtime).cancel_project(
                project_id=args.project_id, actor=args.actor, rationale=args.rationale,
            )
        except (ResearchProjectNotFound, ResearchDecisionError, ValueError) as exc:
            _error_exit(exc)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "project-resume":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        try:
            result = ResearchProjectService(runtime).resume_project(
                project_id=args.project_id, actor=args.actor, rationale=args.rationale,
            )
        except (ResearchProjectNotFound, ResearchDecisionError, ValueError) as exc:
            _error_exit(exc)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "project-approve":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        result = ResearchProjectService(runtime).accept_checkpoint(
            project_id=args.project_id,
            target_id=args.target_id,
            actor=args.actor,
            rationale=args.rationale,
            resume=args.resume,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "project-repairs":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        print(json.dumps(ResearchProjectService(runtime).repairs(args.project_id), indent=2, ensure_ascii=False))
    elif args.command == "project-repair-decision":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        result = ResearchProjectService(runtime).decide_repair(
            project_id=args.project_id,
            repair_request_id=args.repair_request_id,
            trigger_snapshot_digest=args.snapshot_digest,
            approve=args.approve,
            actor=args.actor,
            rationale=args.rationale,
            resume=args.resume,
        )
    elif args.command == "project-branches":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        try:
            result = ResearchProjectService(runtime).branches(args.project_id)
        except (ResearchProjectNotFound, ResearchDecisionError, ValueError) as exc:
            _error_exit(exc)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "project-fork-propose":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        try:
            result = ResearchProjectService(runtime).propose_fork(
                project_id=args.project_id,
                target_work_item_id=args.target_work_item_id,
                mode=args.mode,
                rationale=args.rationale,
                actor=args.actor,
                rollback_to_attempt_id=args.rollback_to_attempt_id,
                input_overrides=_parse_json_overrides(args.input_overrides),
            )
        except (ResearchProjectNotFound, ResearchDecisionError, ValueError) as exc:
            _error_exit(exc)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "project-fork-decision":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        try:
            result = ResearchProjectService(runtime).decide_fork(
                project_id=args.project_id,
                branch_id=args.branch_id,
                approve=args.approve,
                actor=args.actor,
                rationale=args.rationale,
                resume=args.resume,
            )
        except (ResearchProjectNotFound, ResearchDecisionError, ValueError) as exc:
            _error_exit(exc)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "session":
        runtime = ResearchProjectRuntime(projects_dir=args.projects_dir, settings=settings)
        sessions = ResearchSessionService(runtime)
        try:
            if args.session_command == "create":
                result = sessions.create(args.project_id, title=args.title, role=args.role)
            elif args.session_command == "list":
                result = sessions.list(args.project_id)
            elif args.session_command == "read":
                result = sessions.messages(args.project_id, args.session_id)
            elif args.session_command == "post":
                result = sessions.post_message(
                    args.project_id, args.session_id, args.text,
                    ask_agent=args.ask_agent, actor=args.actor,
                )
            else:
                result = sessions.intervene(
                    project_id=args.project_id,
                    session_id=args.session_id,
                    action=args.action,
                    rationale=args.rationale,
                    actor=args.actor,
                    target_id=args.target_id,
                    approve=args.approve,
                    snapshot_digest=args.snapshot_digest,
                    mode=args.mode,
                    rollback_to_attempt_id=args.rollback_to_attempt_id,
                    input_overrides=_parse_json_overrides(args.input_overrides),
                )
        except (ResearchProjectNotFound, ResearchDecisionError, ValueError) as exc:
            _error_exit(exc)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "export-schemas":
        for path in export_schemas(args.output):
            print(path)
    elif args.command == "generate-alignment":
        print(json.dumps(generate(args.output), ensure_ascii=False))
    elif args.command == "doctor":
        print(json.dumps(_doctor(settings), indent=2, ensure_ascii=False))
    elif args.command == "llm-smoke-test":
        print(json.dumps(_smoke_test(settings), indent=2, ensure_ascii=False))
    elif args.command == "pattern":
        store_path = getattr(args, "store", None) or settings.pattern_store_path
        ledger_path = settings.pattern_review_ledger_path
        if args.pattern_command == "curate":
            from .pattern_extraction import CurationRecord, CurationStore

            curation_path = args.curation or settings.pattern_curation_path
            store = CurationStore(curation_path)
            added = store.add(CurationRecord(
                pmid=args.pmid,
                status=args.status,
                rationale=args.rationale,
                annotator_role=args.role,
            ))
            print(json.dumps({
                "added": added,
                "pmid": args.pmid,
                "status": args.status,
                "latest_status": store.latest_status(args.pmid),
                "path": str(store.path),
            }, indent=2, ensure_ascii=False))
        elif args.pattern_command == "nominate":
            from .gold_nomination import nominate_candidates, write_nominations
            from .paper_corpus import CorpusStore

            records = CorpusStore(args.corpus).all()
            nominations = nominate_candidates(
                records,
                limit=args.limit,
                min_score=args.min_score,
                year_min=args.year_min,
            )
            out_path = args.out or settings.pattern_nomination_path
            written = write_nominations(out_path, nominations)
            print(json.dumps({
                **written,
                "corpus": str(args.corpus),
                "candidate_records": len(records),
                "limit": args.limit,
                "min_score": args.min_score,
                "year_min": args.year_min,
                "nominations": [
                    {
                        "pmid": row.pmid,
                        "title": row.title,
                        "journal": row.journal,
                        "year": row.year,
                        "score": row.score,
                        "signal_lanes": row.signal_lanes,
                        "gap_diseases": row.gap_diseases,
                        "reasons": row.reasons,
                    }
                    for row in nominations
                ],
            }, indent=2, ensure_ascii=False))

        elif args.pattern_command == "extract":
            from .llm import StepClient
            from .paper_corpus import CorpusStore
            from .pattern_extraction import (
                CurationStore, EuropePmcMetaFetcher, ExtractionAuditStore,
                PatternExtractor, run_extraction,
            )
            from .paper_strategy import PatternStore as PatternStoreForExtraction

            client = StepClient.from_settings(settings)
            if client is None:
                raise SystemExit("Step API is not configured; pattern extraction requires a structured LLM backend")
            pattern_path = args.store or settings.pattern_store_path
            curation_path = args.curation or settings.pattern_curation_path
            audit_path = args.audit or settings.pattern_extraction_audit_path
            all_papers = CorpusStore(args.corpus).all()
            gold = set(CurationStore(curation_path).gold_pmids())
            pmid_values = [value.strip() for value in args.pmids.split(",") if value.strip()]
            if pmid_values:
                wanted = set(pmid_values)
                selected = [row for row in all_papers if row.pmid in wanted]
            else:
                selected = [row for row in all_papers if row.pmid in gold]
            extractor = PatternExtractor(
                backend=client,
                meta_fetcher=EuropePmcMetaFetcher(),
                pattern_store=PatternStoreForExtraction(pattern_path),
            )
            result = run_extraction(
                papers=selected,
                pattern_store=extractor.store,
                extractor=extractor,
                audit_store=ExtractionAuditStore(audit_path),
            )
            print(json.dumps({
                **result,
                "gold_available": sorted(gold),
                "audit_card": ExtractionAuditStore(audit_path).card(),
                "pattern_card": PatternStoreForExtraction(pattern_path).corpus_card(),
            }, indent=2, ensure_ascii=False))
        elif args.pattern_command == "review":
            from .paper_strategy import ReviewEntry, ReviewLedger

            review_path = args.ledger or ledger_path
            ledger = ReviewLedger(review_path)
            added = ledger.add(ReviewEntry(
                pattern_id=args.pattern_id,
                role=args.role,
                status=args.status,
            ))
            print(json.dumps({
                "added": added,
                "pattern_id": args.pattern_id,
                "status": ledger.status(args.pattern_id),
                "path": str(review_path),
            }, indent=2, ensure_ascii=False))
        elif args.pattern_command == "search":
            lanes = {item.strip().lower() for item in args.lanes.split(",") if item.strip()}
            hits = PatternStore(store_path).search(
                query=args.query, disease=args.disease,
                lanes_available=lanes or None, top_k=args.top_k,
            )
            print(json.dumps([
                {
                    "pattern_id": hit.pattern.pattern_id,
                    "name": hit.pattern.name,
                    "score": round(hit.score, 2),
                    "start_lane": hit.pattern.evidence_start_lane,
                    "ordered_lanes": hit.pattern.ordered_lanes,
                    "why_this_order": hit.pattern.mixed_method_rationale,
                    "matched_reason": hit.matched_reason,
                }
                for hit in hits
            ], indent=2, ensure_ascii=False))
        elif args.pattern_command == "list":
            print(json.dumps(PatternStore(store_path, review_ledger_path=ledger_path).corpus_card(), indent=2, ensure_ascii=False))
        elif args.pattern_command == "add":
            payload = yaml.safe_load(args.input.read_text(encoding="utf-8"))
            store = PatternStore(store_path)
            added = store.add(payload)
            print(json.dumps({
                "added": added,
                "pattern_id": payload.get("pattern_id") if isinstance(payload, dict) else None,
                "path": str(store.path),
            }, indent=2, ensure_ascii=False))
        elif args.pattern_command == "rag":
            from .paper_corpus import CorpusStore
            from .paper_rag import PaperRagStore, build_chunks
            from .pattern_extraction import EuropePmcMetaFetcher

            rag_store = PaperRagStore(args.store)
            if args.paper_rag_command == "refresh":
                fetcher = EuropePmcMetaFetcher()
                all_papers = CorpusStore(args.corpus).all()
                pmid_values = [value.strip() for value in args.pmids.split(",") if value.strip()]
                if pmid_values:
                    wanted = set(pmid_values)
                    papers = [row for row in all_papers if row.pmid in wanted]
                else:
                    papers = [row for row in all_papers if row.status == "candidate"]
                if args.limit and args.limit > 0:
                    papers = papers[: args.limit]
                existing = {chunk.pmid for chunk in rag_store.all()}
                added_chunks = 0
                skipped_chunks = 0
                failed: list[dict[str, str]] = []
                for row in papers:
                    if row.pmid in existing:
                        skipped_chunks += 1
                        continue
                    try:
                        meta = fetcher.fetch(row.pmid)
                        if meta is None:
                            failed.append({"pmid": row.pmid, "error": "no Europe PMC record"})
                            continue
                        chunks = build_chunks(
                            meta,
                            context_tags=row.query_buckets,
                            chunk_size=args.chunk_size,
                            overlap=args.overlap,
                        )
                        result = rag_store.add_many(chunks)
                        added_chunks += result["added"]
                        skipped_chunks += result["skipped"]
                    except Exception as exc:
                        failed.append({"pmid": row.pmid, "error": str(exc)[:200]})
                manifest = rag_store.write_manifest()
                print(json.dumps({
                    "added_chunks": added_chunks,
                    "skipped_chunks": skipped_chunks,
                    "failed_papers": failed,
                    "manifest_count": manifest["chunks"],
                    "card": rag_store.corpus_card(),
                }, indent=2, ensure_ascii=False))
            elif args.paper_rag_command == "search":
                lanes = {item.strip().lower() for item in args.lanes.split(",") if item.strip()}
                hits = rag_store.search(
                    query=args.query, disease=args.disease,
                    lanes_available=lanes or None, top_k=args.top_k,
                )
                print(json.dumps([
                    {
                        "chunk_id": hit.chunk.chunk_id,
                        "pmid": hit.chunk.pmid,
                        "title": hit.chunk.title,
                        "journal": hit.chunk.journal,
                        "year": hit.chunk.year,
                        "lane_tags": hit.chunk.lane_tags,
                        "score": round(hit.score, 2),
                        "snippet": (hit.chunk.text[:420] + "...") if len(hit.chunk.text) > 420 else hit.chunk.text,
                        "matched_reason": hit.matched_reason,
                    }
                    for hit in hits
                ], indent=2, ensure_ascii=False))
            else:
                print(json.dumps(rag_store.corpus_card(), indent=2, ensure_ascii=False))
        elif args.pattern_command == "corpus":
            from .paper_corpus import CorpusStore, RequestsEutilsClient, fetch_candidates

            if args.paper_corpus_command == "refresh":
                client = RequestsEutilsClient(
                    email=args.email or os.environ.get("NCBI_EMAIL") or None,
                    api_key=os.environ.get("NCBI_API_KEY") or None,
                )
                records = fetch_candidates(
                    client,
                    year_min=args.year_min,
                    year_max=args.year_max,
                    retmax_per_query=args.retmax,
                    max_candidates=args.max_candidates,
                )
                store = CorpusStore(args.store)
                result = store.add_many(records)
                manifest = store.write_manifest()
                print(json.dumps({
                    **result,
                    "manifest_count": manifest["count"],
                    "card": store.corpus_card(),
                }, indent=2, ensure_ascii=False))
            else:
                print(json.dumps(CorpusStore(args.store).corpus_card(), indent=2, ensure_ascii=False))
    elif args.command == "diseases":
        from .diseases import load_library

        library = load_library()
        rows = [
            {
                "id": entry.id, "name": entry.name, "name_zh": entry.name_zh,
                "ontology_id": entry.ontology_id, "category": entry.category,
                "reference_targets": len(entry.reference_targets),
                "tissue": entry.context.tissue, "cell_type": entry.context.cell_type,
            }
            for entry in library.diseases
        ]
        if args.json:
            print(json.dumps({"version": library.version, "diseases": rows}, indent=2, ensure_ascii=False))
        else:
            print(f"disease library v{library.version} — {len(rows)} entries (ontology ids OLS-verified)")
            print(f"{'id':<10} {'ontology':<16} {'category':<12} {'refs':>4}  name (zh)")
            for row in rows:
                print(f"{row['id']:<10} {row['ontology_id']:<16} {row['category']:<12} "
                      f"{row['reference_targets']:>4}  {row['name']} ({row['name_zh']})")
    elif args.command == "run-disease":
        from .diseases import load_library

        library = load_library()
        runtime_cls = LangGraphRuntime if args.runtime == "langgraph" else TargetDiscoveryRuntime
        runtime = runtime_cls(runs_dir=args.runs_dir, cache_dir=args.cache_dir, settings=settings)
        queries = [item.strip() for item in args.disease.split(",") if item.strip()]
        if not queries:
            raise SystemExit("--disease requires at least one id, name or synonym")
        summary_rows = []
        for query in queries:
            entry = library.find(query)
            task = library.to_task_spec(query, kind=args.kind)
            run_id = f"{args.run_id}-{entry.id}"
            print(f"[run-disease] {entry.id}: {task.question} (kind={args.kind}, run_id={run_id})")
            result = runtime.run(task, run_id=run_id)
            ranked_path = runtime.runs_dir / run_id / "ranked_targets.json"
            top_genes: list[str] = []
            if ranked_path.exists():
                ranked = json.loads(ranked_path.read_text(encoding="utf-8"))
                top_genes = [row.get("gene", "?") for row in ranked[:3]]
            summary_rows.append({
                "disease_id": entry.id, "disease": entry.name, "kind": args.kind,
                "run_id": run_id, "terminal_status": result.get("terminal_status"),
                "highlighted_targets": top_genes,
                "tool_calls": result.get("detail", {}).get("tool_calls"),
            })
        print(f"\n{'disease':<10} {'terminal_status':<22} top-3 targets")
        for row in summary_rows:
            print(f"{row['disease_id']:<10} {str(row['terminal_status']):<22} {', '.join(row['highlighted_targets'])}")
        if args.summary_out:
            args.summary_out.parent.mkdir(parents=True, exist_ok=True)
            args.summary_out.write_text(json.dumps(
                {"library_version": library.version, "kind": args.kind, "runs": summary_rows},
                indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"summary written to {args.summary_out}")
    elif args.command == "workflows":
        from .workflow_catalog import WorkflowCatalogError

        service = ResearchProjectService(ResearchProjectRuntime(settings=settings))
        if args.workflow_command == "list":
            try:
                print(json.dumps(service.workflow_templates(), indent=2, ensure_ascii=False))
            except WorkflowCatalogError as exc:
                raise SystemExit(str(exc)) from exc
        else:
            try:
                template = service.workflow_catalog.get(args.id)
            except WorkflowCatalogError as exc:
                raise SystemExit(str(exc)) from exc
            print(json.dumps(template.model_dump(mode="json"), indent=2, ensure_ascii=False))

    elif args.command == "skills":
        from .skill_catalog import SkillCatalog

        catalog = SkillCatalog(settings.skill_catalog_path)
        if args.skills_command == "list":
            print(json.dumps(catalog.public_summary(), indent=2, ensure_ascii=False))
        elif args.skills_command == "search":
            lanes = {item.strip().lower() for item in args.lanes.split(",") if item.strip()} or None
            scopes = {item.strip().lower() for item in args.scopes.split(",") if item.strip()} or None
            hits = catalog.search(query=args.query, lanes=lanes, scopes=scopes, top_k=args.top_k)
            print(json.dumps([
                {
                    "id": hit.skill.skill_id,
                    "name": hit.skill.name,
                    "score": round(hit.score, 3),
                    "evidence_lanes": hit.skill.evidence_lanes,
                    "reason": hit.matched_reason,
                }
                for hit in hits
            ], indent=2, ensure_ascii=False))
        elif args.skills_command == "show":
            loaded = catalog.load(args.id)
            if loaded is None:
                raise SystemExit(f"skill not found: {args.id}")
            print(f"# {loaded['name']} (v{loaded['version']}, sha256 {loaded['sha256'][:12]})")
            print()
            print(loaded["description"])
            print()
            print(loaded["content"])
    elif args.command == "kernel":
        from .kernel import (
            KernelDaemon, KernelDaemonClient, KernelDisabledError,
            KernelNotFoundError, KernelTimeoutError, KernelUnavailableError,
        )

        try:
            if args.kernel_command == "serve":
                KernelDaemon(settings).run(port=args.port)
            else:
                client = KernelDaemonClient(settings)
                if args.kernel_command == "start":
                    info = client.start(language=args.language, cwd=args.cwd)
                    print(json.dumps(info, indent=2, ensure_ascii=False))
                elif args.kernel_command == "exec":
                    result = client.execute(args.kernel_id, args.code, timeout=args.timeout)
                    print(json.dumps(result, indent=2, ensure_ascii=False))
                elif args.kernel_command == "status":
                    print(json.dumps(client.status(args.kernel_id), indent=2, ensure_ascii=False))
                elif args.kernel_command == "stop":
                    print(json.dumps(client.stop(args.kernel_id), indent=2, ensure_ascii=False))
                elif args.kernel_command == "stop-all":
                    print(json.dumps({"stopped": client.stop_all()}, indent=2, ensure_ascii=False))
        except (KernelDisabledError, KernelNotFoundError, KernelUnavailableError,
                KernelTimeoutError) as exc:
            raise SystemExit(f"{exc.__class__.__name__}: {exc}")
    elif args.command == "serve":
        _start_workbench(settings, args)
    elif args.command == "up":
        doctor = _doctor(settings)
        missing_required = [
            name for name, ok in doctor["required_dependencies"].items() if not ok
        ]
        if missing_required:
            raise SystemExit("missing required dependencies: " + ", ".join(missing_required))
        print(json.dumps({
            "start": "up",
            "llm_configured": doctor["settings"]["llm_configured"],
            "keyring_backend": (doctor.get("keyring") or {}).get("backend"),
            "projects_dir_writable": doctor["settings"]["projects_dir_writable"],
            "dependencies_ok": True,
        }, indent=2, ensure_ascii=False), flush=True)
        _start_workbench(settings, args)
    elif args.command == "secrets":
        from . import secret_store
        if args.secret_command == "status":
            print(json.dumps({
                "backend": secret_store.keyring_backend_name(),
                "secrets": {
                    name: "configured" if secret_store.get_secret(name) else "not_configured"
                    for name in secret_store.SECRET_NAMES
                },
            }, indent=2, ensure_ascii=False))
        elif args.secret_command == "set":
            value = args.value
            if value is None:
                value = sys.stdin.read().strip()
            secret_store.set_secret(args.name, value)
            print(json.dumps({"stored": True, "name": args.name}, ensure_ascii=False))
        elif args.secret_command == "delete":
            removed = secret_store.delete_secret(args.name)
            print(json.dumps({"deleted": removed, "name": args.name}, ensure_ascii=False))
    elif args.command == "ask":
        from .llm import StepClient
        from .question_intake import QuestionNeedsInput, build_draft, reserve_draft

        client = StepClient.from_settings(settings)
        hints = {
            "disease": args.disease,
            "disease_subtype": args.subtype,
            "tissue": args.tissue,
            "cell_type": args.cell_type,
            "disease_stage": args.stage,
            "desired_phenotype": args.phenotype,
            "organism": args.organism,
        }
        try:
            draft = build_draft(
                args.question,
                hints=hints,
                client=client,
                project_id=args.project_id,
                autonomy_mode=args.autonomy,
            )
        except QuestionNeedsInput as exc:
            raise SystemExit(f"question intake needs input: {exc}")
        payload = {
            "draft_version": draft.draft_version,
            "question": draft.question,
            "disease_resolution": draft.disease_resolution,
            "extracted": draft.extracted,
            "confidence": draft.confidence,
            "sources": draft.sources,
            "needs_review": draft.needs_review,
            "review_notes": draft.review_notes,
            "spec": draft.spec,
        }
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                yaml.safe_dump(draft.spec, allow_unicode=True, sort_keys=False), encoding="utf-8",
            )
            payload["output"] = str(output)
        if args.create:
            reserved = reserve_draft(draft, settings)
            project_id = draft.spec["project_id"]
            payload["created"] = True
            payload["project_id"] = project_id
            payload["next"] = [
                "target-agent serve --port 8888  # approve checkpoints and view results in the workbench",
                "target-agent project-export --project-id " + project_id + " --output " + project_id + ".target-project.zip",
            ]
            if args.output:
                payload["next"].insert(0, "target-agent project-run --input " + str(args.output.expanduser().resolve()))
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    elif args.command == "init":
        service = ResearchProjectService(ResearchProjectRuntime(settings=settings))
        if args.workflow != "literature_review" and not args.disease:
            raise SystemExit("--disease is required unless --workflow literature_review is selected")
        if args.workflow == "literature_review":
            spec = service.build_generic_project(
                question=args.question or f"Review the literature on {args.disease}",
                title=args.title,
                project_id=args.project_id,
                workflow=args.workflow,
                autonomy_mode=args.autonomy,
            )
        else:
            spec = service.build_disease_project(
                question=args.question or f"Discover drug targets for {args.disease}",
                disease=args.disease,
                title=args.title,
                project_id=args.project_id,
                disease_subtype=args.subtype,
                tissue=args.tissue,
                cell_type=args.cell_type,
                disease_stage=args.stage,
                desired_phenotype=args.phenotype,
                organism=args.organism,
                autonomy_mode=args.autonomy,
                workflow_template=args.workflow,
            )
        output = args.output.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        (output / "project.yaml").write_text(
            yaml.safe_dump(spec.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        env_example = Path(__file__).resolve().parents[2] / ".env.example"
        if env_example.exists():
            shutil.copy2(env_example, output / ".env.example")
        (output / "README.md").write_text(_init_readme(spec), encoding="utf-8")
        print(json.dumps({
            "project_id": spec.project_id,
            "title": spec.title,
            "output": str(output),
            "next": [
                "target-agent project-run --input " + str(output / "project.yaml"),
                "target-agent serve --port 8888",
            ],
        }, indent=2, ensure_ascii=False))
    elif args.command == "project-export":
        from .project_package import export_project as _export_package

        summary = _export_package(
            projects_dir=args.projects_dir or settings.projects_dir,
            project_id=args.project_id,
            output=args.output,
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    elif args.command == "project-import":
        from .project_package import import_project as _import_package

        summary = _import_package(
            projects_dir=args.projects_dir or settings.projects_dir,
            archive=args.input,
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    elif args.command == "project-package-inspect":
        from .project_package import inspect_package

        print(json.dumps(inspect_package(args.input), indent=2, ensure_ascii=False))
    elif args.command == "share":
        from .share_portal import render_share_portal_for_project, render_share_portal_from_package

        if args.input is not None:
            html = render_share_portal_from_package(
                args.input,
                output=args.output,
                max_preview_bytes=args.max_preview_bytes,
            )
        elif args.project_id:
            html = render_share_portal_for_project(
                args.projects_dir or settings.projects_dir,
                args.project_id,
                output=args.output,
                max_preview_bytes=args.max_preview_bytes,
            )
        else:
            raise SystemExit("share requires --project-id or --input")
        print(json.dumps({
            "rendered": True,
            "output": str(Path(args.output).expanduser().resolve()),
            "bytes": len(html.encode("utf-8")),
        }, indent=2, ensure_ascii=False))

    elif args.command == "mcp-serve":
        try:
            from .mcp_server import _serve, create_mcp_server

            _serve(
                create_mcp_server(runtime=ResearchProjectRuntime(settings=settings)),
                transport=args.mcp_transport,
                host=args.mcp_host,
                port=args.mcp_port,
                path=args.mcp_path,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc


__all__ = ["main", "load_task", "load_research_project", "_doctor", "_smoke_test"]


if __name__ == "__main__":
    main()

