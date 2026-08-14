"""Systematic Agent benchmark runner.

Executes benchmark/goldset_v2.jsonl against the agent runtimes and scores every
machine-checkable assertion. Fake and unit modes are deterministic and CI-safe;
live mode hits real external APIs and is opt-in (--live).

Usage:
    python benchmark/runner.py [--goldset benchmark/goldset_v2.jsonl] [--out benchmark/results]
                               [--live] [--keep-runs]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from pydantic import ValidationError  # noqa: E402

from target_agent.ablations import parse as parse_ablations  # noqa: E402
from target_agent.contracts import ExecutionPlan, PlanStep, TaskSpec  # noqa: E402
from target_agent.legacy import parse_task_spec  # noqa: E402
from target_agent.planner import Planner  # noqa: E402
from target_agent.runtime import TargetDiscoveryRuntime  # noqa: E402
from target_agent.runtime_langgraph import LangGraphRuntime  # noqa: E402
from target_agent.schema_export import export_schemas  # noqa: E402
from target_agent.store import EvidenceStore  # noqa: E402
from target_agent.tools.base import ToolRegistry  # noqa: E402
from target_agent.tools.mch import MCHCausalGoldTool  # noqa: E402

from fakes import FakeGenericOmics, FakeLiterature, FakeOpenTargets  # noqa: E402

RUNTIMES = {"legacy": TargetDiscoveryRuntime, "langgraph": LangGraphRuntime}


# --------------------------------------------------------------------------- helpers
def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def observable(run_dir: Path) -> dict:
    evidence = jsonl(run_dir / "evidence_items.jsonl")
    findings = jsonl(run_dir / "reviewer_findings.jsonl")
    trace = jsonl(run_dir / "trace.jsonl")
    tool_results = jsonl(run_dir / "tool_results.jsonl")
    ranking_path = run_dir / "ranked_targets.json"
    return {
        "terminal_status": json.loads((run_dir / "status.json").read_text())["terminal_status"],
        "ranking": [(row["gene"], row["scores"], row["decision"]) for row in
                    (json.loads(ranking_path.read_text()) if ranking_path.exists() else [])],
        "evidence": sorted((item["gene_symbol"] or "", item["statement"], item["source_span"]) for item in evidence),
        "findings": sorted((f["category"], f["severity"]) for f in findings),
        "tool_results": [(r["tool_name"], r["status"], r["coverage_status"]) for r in tool_results],
        "trace_topology": [(e["event_type"], e["state"]) for e in trace],
    }


def build_runtime(engine: str, registry_kind: str, work: Path, cache_dir: Path | None = None):
    if registry_kind == "fake":
        registry = ToolRegistry([FakeGenericOmics(), FakeOpenTargets(), FakeLiterature()])
    elif registry_kind == "fake_mch":
        registry = ToolRegistry([FakeGenericOmics(), FakeOpenTargets(), FakeLiterature(), MCHCausalGoldTool()])
    else:
        registry = None  # live: default registry from settings
    return RUNTIMES[engine](
        runs_dir=work / "runs", cache_dir=cache_dir or work / "cache", planner=Planner(None), registry=registry,
    )


# --------------------------------------------------------------------------- unit checks
def unit_contract_version_gate() -> str | None:
    payload = {"task_type": "disease_to_target", "question": "gate check",
               "context": {"disease": "ulcerative colitis"}}
    try:
        TaskSpec(contract_version="2.0.0", **payload)
    except ValidationError:
        pass
    else:
        return "TaskSpec accepted contract_version 2.0.0"
    try:
        TaskSpec(**payload)
    except ValidationError as exc:
        return f"TaskSpec rejected the current contract version: {exc}"
    return None


def unit_planner_whitelist() -> str | None:
    registry = ToolRegistry([FakeGenericOmics(), FakeOpenTargets(), FakeLiterature()])
    planner = Planner(None, registry)
    for task_type, context in [
        ("disease_to_target", {"disease": "ulcerative colitis"}),
        ("trait_mechanism", {"desired_phenotype": "MCH"}),
    ]:
        task = TaskSpec(task_type=task_type, question="whitelist check", context=context)
        plan = planner.create_plan(task)
        rogue = [step.tool for step in plan.steps if step.tool and step.tool not in registry.names]
        if rogue:
            return f"deterministic plan emitted non-registered tools: {rogue}"
    bogus = ExecutionPlan(task_id="task-x", planner_backend="test", steps=[
        PlanStep(step_id="s1", name="rogue", tool="shell_executor"),
    ])
    try:
        planner._validate(TaskSpec(task_type="disease_to_target", question="q",
                                   context={"disease": "ulcerative colitis"}), bogus)
    except ValueError:
        return None
    return "planner accepted a non-whitelisted tool"


def unit_schema_export_valid() -> str | None:
    import jsonschema
    with tempfile.TemporaryDirectory() as tmp:
        paths = export_schemas(Path(tmp))
        if not paths:
            return "schema export produced no files"
        for path in paths:
            schema = json.loads(Path(path).read_text(encoding="utf-8"))
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.SchemaError as exc:
                return f"invalid schema {Path(path).name}: {exc.message}"
    return None



def unit_pattern_ablation_offline() -> str | None:
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            sys.executable, str(ROOT / "benchmark" / "pattern_ablation.py"),
            "--goldset", str(ROOT / "benchmark" / "goldset_diseases.jsonl"),
            "--store", str(ROOT / "paper_strategy" / "patterns.jsonl"),
            "--out", tmp,
        ]
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception as exc:
            return f"pattern ablation crashed: {exc.__class__.__name__}: {exc}"
        if completed.returncode != 0:
            return f"pattern ablation failed: {completed.stdout[-400:]}{completed.stderr[-400:]}"
        try:
            payload = json.loads(completed.stdout)
        except Exception as exc:
            return f"pattern ablation returned invalid JSON: {exc}"
        if payload.get("diseases_total", 0) < 10:
            return "pattern ablation considered too few diseases"
        if payload.get("diseases_with_hints", 0) < 3:
            return "pattern ablation coverage below the seed floor (3 diseases)"
        if payload.get("plan_valid", 0) != payload.get("diseases_total"):
            return "pattern ablation produced invalid deterministic plans"
    return None


def unit_paper_rag_graph_projection() -> str | None:
    from target_agent.contracts import (
        ClaimClass, EvidenceContext, EvidenceItem, SourceLocator, Stance,
        TaskContext,
    )
    from target_agent.graphs import synthesize_evidence_graph

    item = EvidenceItem(
        tool_run_id="tool-g1",
        gene_symbol="GENE1",
        claim_class=ClaimClass.FACT,
        statement="test evidence",
        source=SourceLocator(uri="https://example.org/x", source_id="s1"),
        source_span="GENE1",
        context=EvidenceContext(disease="test disease", tissue="lung", cell_type="T cell"),
        stance=Stance.SUPPORTS,
        effect_direction="unclear",
        effect={},
        uncertainty="fixture",
        context_match_score=0.9,
    )
    task = TaskSpec(
        task_type="disease_to_target",
        question="test question",
        context=TaskContext(disease="test disease", tissue="lung", cell_type="T cell"),
    )
    baseline = synthesize_evidence_graph(task, [item], ["GENE1"])
    result = synthesize_evidence_graph(
        task, [item], ["GENE1"],
        paper_evidence=[{
            "kind": "paper_rag",
            "chunk_id": "chunk-0-paper-0",
            "pmid": "12345678",
            "title": "GENE1 mechanism in test disease",
            "journal": "Nature",
            "year": 2025,
            "lane_tags": ["genetics", "omics"],
            "snippet": "GENE1 regulates test disease",
            "score": 4.0,
            "strategy_hint_not_evidence": True,
        }],
    )
    node_ids = {node.node_id for node in result.graph.nodes}
    if "strategy:paper:chunk-0-paper-0" not in node_ids:
        return "paper RAG strategy node missing"
    hint_edges = [edge for edge in result.graph.edges if edge.relation == "paper_strategy_hint"]
    if len(hint_edges) != 1:
        return f"expected 1 paper strategy edge, got {len(hint_edges)}"
    edge = hint_edges[0]
    if edge.claim_class != ClaimClass.INFERRED or edge.weight != 0.0:
        return "paper strategy edge must be INFERRED with weight 0"
    if edge.attributes.get("strategy_only") is not True or edge.attributes.get("not_evidence") is not True:
        return "paper strategy edge missing strategy_only/not_evidence markers"
    if edge.evidence_ids:
        return "paper strategy edge must carry no evidence ids"
    if result.lane_coverage != baseline.lane_coverage:
        return "paper RAG hit changed lane coverage"
    if result.pattern_links != baseline.pattern_links:
        return "paper RAG hit changed pattern links"
    if result.findings != baseline.findings:
        return "paper RAG hit changed synthesis findings"
    if result.graph.model_statistics.get("paper_strategy_hints") != 1:
        return "paper_strategy_hints statistic missing"
    return None


def unit_blind_protocol_synthetic() -> str | None:
    import importlib.util

    demo_path = ROOT / "benchmark" / "demo_blind_ranking.py"
    spec = importlib.util.spec_from_file_location("demo_blind_ranking", demo_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as tmp:
        public = module.run_demo(Path(tmp), ROOT / "benchmark" / "blind_demo_labels.json")
        if not public["passed"]:
            return f"blind protocol demo gates failed: {public['gates']}"
        summary = public["summary"]
        if summary["expert_adjudicated"]:
            return "blind protocol demo must stay a synthetic fixture"
        if summary["structurally_valid_cases"] != summary["cases"]:
            return "blind protocol demo structural integrity failed"
    return None


UNIT_CHECKS = {
    "contract_version_gate": unit_contract_version_gate,
    "planner_whitelist": unit_planner_whitelist,
    "schema_export_valid": unit_schema_export_valid,
    "pattern_ablation_offline": unit_pattern_ablation_offline,
    "paper_rag_graph_projection": unit_paper_rag_graph_projection,
    "blind_protocol_synthetic": unit_blind_protocol_synthetic,
}


# --------------------------------------------------------------------------- assertions
def check_assertion(assertion: dict, ctx: dict) -> str | None:
    """Return None on pass, failure message otherwise."""
    kind = assertion["type"]
    if kind == "unit":
        return UNIT_CHECKS[assertion["check"]]()
    run_dir: Path = ctx["run_dir"]
    if kind == "terminal_status":
        got = json.loads((run_dir / "status.json").read_text())["terminal_status"]
        return None if got == assertion["equals"] else f"terminal_status={got}, expected {assertion['equals']}"
    if kind == "terminal_status_in":
        got = json.loads((run_dir / "status.json").read_text())["terminal_status"]
        return None if got in assertion["values"] else f"terminal_status={got}, expected one of {assertion['values']}"
    if kind == "ranking_contains":
        genes = [row["gene"] for row in ctx["ranking"]]
        return None if assertion["gene"] in genes else f"{assertion['gene']} not in ranking {genes}"
    if kind == "ranking_length":
        return None if len(ctx["ranking"]) == assertion["equals"] else \
            f"ranking length {len(ctx['ranking'])}, expected {assertion['equals']}"
    if kind == "ranking_min_length":
        return None if len(ctx["ranking"]) >= assertion["value"] else \
            f"ranking length {len(ctx['ranking'])} < {assertion['value']}"
    if kind == "cards_length":
        cards = json.loads((run_dir / "target_cards.json").read_text())
        return None if len(cards) == assertion["equals"] else \
            f"{len(cards)} cards, expected {assertion['equals']}"
    if kind == "trace_contains":
        topology = {(e["event_type"], e["state"]) for e in ctx["trace"]}
        needle = (assertion["event_type"], assertion["state"])
        return None if needle in topology else f"trace lacks {needle}"
    if kind == "tool_status":
        matches = [r for r in ctx["tool_results"] if r["tool_name"] == assertion["tool"]]
        if not matches:
            return f"tool {assertion['tool']} never ran"
        return None if any(r["status"] == assertion["status"] for r in matches) else \
            f"{assertion['tool']} status {[r['status'] for r in matches]}, expected {assertion['status']}"
    if kind == "tool_ran":
        matches = [r for r in ctx["tool_results"] if r["tool_name"] == assertion["tool"]]
        return None if matches else f"tool {assertion['tool']} never ran"
    if kind == "file_exists":
        return None if (run_dir / assertion["path"]).exists() else f"missing artifact {assertion['path']}"
    if kind == "evidence_provenance":
        evidence = ctx["evidence_items"]
        bad = [e["evidence_id"] for e in evidence
               if not e.get("source_span") or not e.get("source", {}).get("uri")]
        return None if not bad else f"evidence without provenance: {bad[:3]}"
    if kind == "min_reference_genes_in_evidence":
        evidence = ctx["evidence_items"]
        genes = {str(e.get("gene_symbol") or "").strip().upper() for e in evidence if e.get("gene_symbol")}
        reference = {str(g).strip().upper() for g in assertion.get("reference_genes", [])}
        min_count = int(assertion.get("min_count", 1))
        hit = len(genes & reference)
        return None if hit >= min_count else \
            f"reference-gene coverage {hit} < {min_count}: {sorted(genes & reference)[:5]}"
    if kind == "deterministic":
        baseline = ctx["observable"]
        for index in range(assertion.get("runs", 2) - 1):
            rerun_dir = ctx["rerun"](f"{ctx['run_id']}-det{index}")
            if observable(rerun_dir) != baseline:
                return f"run {index + 2} diverged from the first run"
        return None
    if kind == "resume_idempotent":
        status_before = (run_dir / "status.json").read_text()
        trace_before = (run_dir / "trace.jsonl").read_text()
        ctx["resume"]()
        if (run_dir / "status.json").read_text() != status_before:
            return "resume changed status.json"
        if (run_dir / "trace.jsonl").read_text() != trace_before:
            return "resume appended trace events"
        return None
    if kind == "resume_completes":
        crashed = ctx["run_id"] + "-crashed"
        store = EvidenceStore(run_dir.parent / crashed)
        store.save_task(ctx["task"])
        store.checkpoint({"stage": "intake", "completed_steps": [], "candidate_genes": [], "tool_calls": 0})
        status = ctx["runtime"].run(ctx["task"], run_id=crashed, resume=True)
        expected = assertion["terminal_status"]
        return None if status["terminal_status"] == expected else \
            f"resumed run ended {status['terminal_status']}, expected {expected}"
    if kind == "parity":
        other_engine = "legacy" if ctx["engine"] == "langgraph" else "langgraph"
        other = build_runtime(other_engine, ctx["registry_kind"], ctx["work"] / f"parity-{other_engine}")
        other.run(ctx["task"], run_id=ctx["run_id"])
        left = observable(ctx["work"] / f"parity-{other_engine}" / "runs" / ctx["run_id"])
        return None if left == ctx["observable"] else "engines diverged on identical input"
    if kind == "no_causal_claims":
        import re
        causal = re.compile(r"caus(es|ed|al|ative)|drives? disease|proves?|is the cause", re.IGNORECASE)
        claims = jsonl(run_dir / "claims.jsonl")
        bad = [c.get("claim_id") for c in claims
               if c.get("claim_class") in {"FACT", "OBSERVED"} and causal.search(c.get("statement", ""))]
        evidence = jsonl(run_dir / "evidence_items.jsonl")
        bad.extend(
            e.get("evidence_id") for e in evidence
            if e.get("claim_class") in {"FACT", "OBSERVED"}
            and causal.search(str(e.get("statement") or ""))
        )
        return None if not bad else f"causal FACT/OBSERVED claims or evidence emitted: {bad[:3]}"
    if kind == "finding_message_contains":
        findings = jsonl(run_dir / "reviewer_findings.jsonl")
        needle = assertion["substring"].casefold()
        return None if any(needle in str(f.get("message", "")).casefold() for f in findings) else \
            f"no reviewer finding message contains {assertion['substring']!r}"
    if kind == "finding_category":
        findings = jsonl(run_dir / "reviewer_findings.jsonl")
        category = assertion["category"]
        return None if any(f.get("category") == category for f in findings) else \
            f"reviewer finding category {category!r} was not observed"
    return f"unknown assertion type {kind}"


# --------------------------------------------------------------------------- runner
def public_path_label(path: Path) -> str:
    """Keep benchmark reports portable and free of deployment-specific paths."""
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def run_task(entry: dict, work: Path, cache_dir: Path | None = None) -> dict:
    started = time.perf_counter()
    results = []
    if entry["mode"] == "unit":
        for assertion in entry["assertions"]:
            failure = check_assertion(assertion, {})
            results.append({"assertion": assertion, "passed": failure is None, "failure": failure})
        return {"id": entry["id"], "title": entry["title"], "category": entry["category"],
                "mode": entry["mode"], "results": results,
                "passed": all(r["passed"] for r in results),
                "elapsed_s": round(time.perf_counter() - started, 2)}

    engine = entry.get("runtime", "langgraph")
    registry_kind = entry.get("registry", "default")
    task = parse_task_spec(entry["task"])
    runtime = build_runtime(engine, registry_kind, work,
                            cache_dir=cache_dir if entry["mode"] == "live" else None)
    run_id = f"bm-{entry['id'].lower()}"
    runtime.run(task, run_id=run_id)
    run_dir = work / "runs" / run_id
    ctx = {
        "run_dir": run_dir, "run_id": run_id, "task": task, "runtime": runtime,
        "engine": engine, "registry_kind": registry_kind, "work": work,
        "ranking": json.loads((run_dir / "ranked_targets.json").read_text()) if (run_dir / "ranked_targets.json").exists() else [],
        "trace": jsonl(run_dir / "trace.jsonl"),
        "tool_results": jsonl(run_dir / "tool_results.jsonl"),
        "evidence_items": jsonl(run_dir / "evidence_items.jsonl"),
        "observable": observable(run_dir),
        "rerun": lambda rid: (lambda: (runtime.run(task, run_id=rid), work / "runs" / rid)[1])(),
        "resume": lambda: runtime.run(task, run_id=run_id, resume=True),
    }
    for assertion in entry["assertions"]:
        try:
            failure = check_assertion(assertion, ctx)
        except Exception as exc:  # an assertion crash is a failure, not a runner crash
            failure = f"assertion raised {exc.__class__.__name__}: {exc}"
        results.append({"assertion": assertion, "passed": failure is None, "failure": failure})
    return {"id": entry["id"], "title": entry["title"], "category": entry["category"],
            "mode": entry["mode"], "results": results,
            "passed": all(r["passed"] for r in results),
            "elapsed_s": round(time.perf_counter() - started, 2)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--goldset", type=Path, default=ROOT / "benchmark" / "goldset_v2.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "benchmark" / "results")
    parser.add_argument("--live", action="store_true", help="also run live external-API tasks")
    parser.add_argument("--shared-cache", action="store_true",
                        help="live mode only: share one cache directory across entries so repeated "
                             "contexts (e.g. disease-library buckets) reuse downloads")
    parser.add_argument("--keep-runs", action="store_true", help="keep run directories for inspection")
    parser.add_argument("--ablate", type=str, default="",
                        help="comma-separated mechanism ablations (see src/target_agent/ablations.py), "
                             "e.g. --ablate no_mechanism_bonus,no_planner_llm; evaluation-only, the "
                             "resulting score delta is descriptive — independent-contribution claims "
                             "require the blind-ranking protocol (benchmark/rubric.md)")
    args = parser.parse_args()

    if args.ablate:
        import os
        os.environ["TARGET_AGENT_EVALUATION_MODE"] = "1"  # ablations are evaluation-only
        os.environ["TARGET_AGENT_ABLATIONS"] = ",".join(sorted(parse_ablations(args.ablate)))

    entries = [json.loads(line) for line in args.goldset.read_text(encoding="utf-8").splitlines() if line.strip()]
    reports = []
    work_root = args.out / "runs_workspace"
    import shutil
    shutil.rmtree(work_root, ignore_errors=True)  # drop stale runs from a crashed session
    work_root.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry["mode"] == "live" and not args.live:
            reports.append({"id": entry["id"], "title": entry["title"], "category": entry["category"],
                            "mode": "live", "skipped": True, "results": [], "passed": None})
            continue
        print(f"[bench] {entry['id']} {entry['title']} ...", flush=True)
        shared = (args.out / "shared_cache") if args.shared_cache else None
        try:
            reports.append(run_task(entry, work_root / entry["id"], cache_dir=shared))
        except Exception as exc:  # one crashing entry must not kill the whole matrix
            reports.append({
                "id": entry["id"], "title": entry["title"], "category": entry["category"],
                "mode": entry["mode"],
                "results": [{"assertion": {"type": "_task_execution"}, "passed": False,
                             "failure": f"task crashed: {exc.__class__.__name__}: {exc}"}],
                "passed": False, "elapsed_s": 0.0,
            })
        print(f"[bench] {entry['id']} -> {'PASS' if reports[-1]['passed'] else 'FAIL'}"
              f" ({reports[-1]['elapsed_s']}s)", flush=True)

    executed = [r for r in reports if not r.get("skipped")]
    total = sum(len(r["results"]) for r in executed)
    passed = sum(sum(1 for a in r["results"] if a["passed"]) for r in executed)
    categories: dict[str, dict[str, int]] = {}
    for report in executed:
        bucket = categories.setdefault(report["category"], {"assertions": 0, "passed": 0})
        bucket["assertions"] += len(report["results"])
        bucket["passed"] += sum(1 for a in report["results"] if a["passed"])
    summary = {
        "goldset": public_path_label(args.goldset), "live": args.live,
        "ablations": sorted(parse_ablations(args.ablate)),
        "tasks": len(executed), "tasks_passed": sum(1 for r in executed if r["passed"]),
        "assertions": total, "assertions_passed": passed,
        "score": round(passed / total, 4) if total else None,
        "categories": {k: {**v, "score": round(v["passed"] / v["assertions"], 4)} for k, v in categories.items()},
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "benchmark_report.json").write_text(
        json.dumps({"summary": summary, "tasks": reports}, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = ["# Agent Benchmark Report", "",
             f"- Gold set: `{args.goldset.name}` (live mode: {args.live})",
             f"- Ablations: {args.ablate or 'none (full mechanism stack)'}",
             f"- Tasks: {summary['tasks_passed']}/{summary['tasks']} passed",
             f"- Assertions: {summary['assertions_passed']}/{summary['assertions']} passed"
             f" (score {summary['score']})", "",
             "| Task | Category | Result | Failed assertions |", "|---|---|---|---|"]
    for report in reports:
        if report.get("skipped"):
            lines.append(f"| {report['id']} {report['title']} | {report['category']} | SKIPPED (live) | - |")
            continue
        failed = [a["failure"] for a in report["results"] if not a["passed"]]
        lines.append(f"| {report['id']} {report['title']} | {report['category']} | "
                     f"{'PASS' if report['passed'] else 'FAIL'} | {'; '.join(failed) or '-'} |")
    lines += ["", "## Category scores", "", "| Category | Assertions | Passed | Score |", "|---|---|---|---|"]
    for name, bucket in summary["categories"].items():
        lines.append(f"| {name} | {bucket['assertions']} | {bucket['passed']} | {bucket['score']} |")
    (args.out / "benchmark_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if not args.keep_runs:
        import shutil
        shutil.rmtree(work_root, ignore_errors=True)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["score"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
