"""Score completed Agent runs against a separately supplied private label file."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from target_agent.blind_benchmark import (  # noqa: E402
    BlindBenchmarkManifest,
    BlindLabelSet,
    bundle_sha256,
    evaluate_benchmark,
    file_sha256,
    public_report,
    render_markdown,
)


def score(args: argparse.Namespace) -> int:
    manifest = BlindBenchmarkManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    labels = BlindLabelSet.model_validate_json(args.labels.read_text(encoding="utf-8"))
    report = evaluate_benchmark(manifest, labels, args.runs)
    report["manifest_sha256"] = file_sha256(args.manifest)
    report["labels_sha256"] = file_sha256(args.labels)
    public = public_report(report)
    public["manifest_sha256"] = report["manifest_sha256"]
    public["labels_sha256"] = report["labels_sha256"]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "blind_ranking_report.json").write_text(
        json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out / "blind_ranking_report.md").write_text(render_markdown(public), encoding="utf-8")
    if args.audit_out:
        args.audit_out.parent.mkdir(parents=True, exist_ok=True)
        args.audit_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def freeze(args: argparse.Namespace) -> int:
    cases = []
    for value in args.case:
        parts = value.split("=")
        if len(parts) != 3:
            raise ValueError(f"--case must be CASE_ID=RUN_ID=DISEASE_GROUP_ID, got {value!r}")
        case_id, run_id, disease_group_id = parts
        task_path = args.runs / run_id / "task_spec.json"
        ranking_path = args.runs / run_id / "ranked_targets.json"
        status_path = args.runs / run_id / "status.json"
        if not all(path.is_file() for path in (task_path, ranking_path, status_path)):
            raise ValueError(f"missing task, ranking or status artifact for {case_id}")
        task_digest = file_sha256(task_path)
        ranking_digest = file_sha256(ranking_path)
        status_digest = file_sha256(status_path)
        cases.append({"case_id": case_id, "disease_group_id": disease_group_id,
                      "run_id": run_id, "task_sha256": task_digest,
                      "ranking_sha256": ranking_digest, "status_sha256": status_digest,
                      "bundle_sha256": bundle_sha256(task_digest, ranking_digest, status_digest)})
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    manifest = BlindBenchmarkManifest(
        benchmark_id=args.benchmark_id, split_id=args.split_id, k=args.k, cases=cases,
        thresholds=policy, require_expert_adjudication=not args.allow_nonexpert_fixture,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(args.out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze_parser = subparsers.add_parser(
        "freeze", help="freeze completed task, ranking and terminal-status digests before labels are opened"
    )
    freeze_parser.add_argument("--benchmark-id", required=True)
    freeze_parser.add_argument("--split-id", required=True)
    freeze_parser.add_argument("--runs", type=Path, required=True)
    freeze_parser.add_argument(
        "--case", nargs="+", required=True,
        help="CASE_ID=RUN_ID=DISEASE_GROUP_ID; pass one or more cases after --case",
    )
    freeze_parser.add_argument("--k", type=int, default=10)
    freeze_parser.add_argument("--policy", type=Path, required=True,
                               help="suite-owned JSON object containing all release thresholds")
    freeze_parser.add_argument("--allow-nonexpert-fixture", action="store_true",
                               help="development only; final suites require expert adjudication")
    freeze_parser.add_argument("--out", type=Path, required=True)
    freeze_parser.set_defaults(handler=freeze)

    score_parser = subparsers.add_parser(
        "score", help="reference scorer for frozen runs; official scoring must run outside participant control"
    )
    score_parser.add_argument("--manifest", type=Path, required=True, help="public frozen case/run manifest")
    score_parser.add_argument("--labels", type=Path, required=True, help="private post-run adjudication labels")
    score_parser.add_argument("--runs", type=Path, required=True, help="root containing completed run directories")
    score_parser.add_argument("--out", type=Path, required=True)
    score_parser.add_argument("--audit-out", type=Path,
                              help="organizer-only per-case audit; public output remains aggregate-only")
    score_parser.set_defaults(handler=score)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
