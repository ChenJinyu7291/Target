"""Reports rendered exclusively from validated structured run artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import CONTRACT_VERSION, ReviewerFinding, TargetCard, TaskSpec, TerminalStatus, ToolResult


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _append_stage_provenance(trace: dict[str, Any], result: ToolResult) -> None:
    artifacts = [artifact.model_dump(mode="json") for artifact in result.artifacts]
    trace["selected_tool_runs"][result.tool_name] = result.tool_run_id
    trace["provenance"].setdefault(result.tool_name, []).append({
        "tool_run_id": result.tool_run_id,
        "artifacts": artifacts,
        "artifact_checksums": {
            artifact.name: artifact.sha256
            for artifact in result.artifacts
            if artifact.sha256
        },
    })


def build_disease_report(
    task: TaskSpec,
    status: TerminalStatus,
    ranked: list[dict[str, Any]],
    cards: list[TargetCard],
    findings: list[ReviewerFinding],
    results: list[ToolResult],
    tier_section: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    datasets = []
    genetics_trace: dict[str, Any] = {
        "input_audit": [], "credible_sets": [], "colocalizations": [], "locus_to_gene_links": [],
        "selected_tool_runs": {}, "provenance": {},
    }
    for result in results:
        if result.tool_name == "geo_metadata_audit":
            datasets = result.outputs.get("selection_trace", [])
        elif result.tool_name == "genetics_input_audit":
            genetics_trace["input_audit"] = result.outputs.get("assets", [])
            genetics_trace["failed_assets"] = result.outputs.get("failed_assets", [])
            _append_stage_provenance(genetics_trace, result)
        elif result.tool_name == "fine_mapping_audit":
            genetics_trace["credible_sets"] = result.outputs.get("credible_sets", [])
            _append_stage_provenance(genetics_trace, result)
        elif result.tool_name == "eqtl_colocalization_audit":
            genetics_trace["colocalizations"] = result.outputs.get("colocalizations", [])
            _append_stage_provenance(genetics_trace, result)
        elif result.tool_name == "genetics_candidate_extraction":
            genetics_trace["locus_to_gene_links"] = result.outputs.get("locus_to_gene_links", [])
            genetics_trace["unresolved_gwas_loci"] = result.outputs.get("unresolved_gwas_loci", [])
            _append_stage_provenance(genetics_trace, result)
    report = {
        "contract_version": CONTRACT_VERSION,
        "task_id": task.task_id,
        "status": status.value,
        "question": task.question,
        "context": task.context.model_dump(mode="json"),
        "dataset_selection_trace": datasets,
        "genetics_selection_trace": genetics_trace,
        "ranked_targets": ranked,
        "highlighted_targets": [row["gene"] for row in ranked[:3]],
        "target_cards": [card.model_dump(mode="json") for card in cards],
        "reviewer_findings": [finding.model_dump(mode="json") for finding in findings],
        "tool_runs": [result.tool_run_id for result in results],
        "report_policy": "All numeric values are copied from structured artifacts; scores are not success probabilities.",
    }
    lines = [
        "# TargetDiscovery Agent 研究报告", "",
        f"- 任务：{task.question}", f"- 终态：`{status.value}`",
        f"- 疾病：{task.context.disease}",
        f"- 组织 / 细胞：{task.context.tissue or '未限定'} / {task.context.cell_type or '未限定'}",
        "- 科学表达：FACT / OBSERVED / PREDICTED / INFERRED 全程分离；总分仅用于排序。",
        "", "## 数据集选择", "",
    ]
    if datasets:
        for item in datasets:
            reasons = "；".join(item.get("reasons") or []) or "通过自动资格检查"
            lines.append(f"- {item.get('accession')}：`{item.get('decision')}`；{reasons}")
    else:
        lines.append("- 当前运行没有合格的动态组学数据集，其他证据链仍继续执行。")
    lines.extend(["", "## 候选靶点排名", "", "| 排名 | 靶点 | 总分 | 决策 | 关键缺口 |", "|---:|---|---:|---|---|"])
    for row in ranked:
        gaps = "；".join(row["evidence_gaps"][:2]) or "无"
        lines.append(f"| {row['rank']} | {row['gene']} | {_fmt(row['scores']['total'])} | {row['decision']} | {gaps} |")
    lines.extend(["", "## 重点 TargetCard", ""])
    for card in cards:
        lines.extend([
            f"### {card.rank}. {card.gene_symbol} — {card.decision}", "",
            f"- 六维得分：遗传 {_fmt(card.scores.human_genetics)}；组学 {_fmt(card.scores.disease_omics)}；扰动 {_fmt(card.scores.perturbation)}；机制 {_fmt(card.scores.mechanism)}；可成药性 {_fmt(card.scores.druggability)}；安全转化 {_fmt(card.scores.safety_translation)}。",
            f"- 支持证据：{', '.join(card.supporting_evidence_ids) or '无'}",
            f"- 反方或混合证据：{', '.join(card.opposing_evidence_ids) or '无'}",
            f"- 证据缺口：{'；'.join(card.evidence_gaps) or '无'}",
            f"- 可证伪假设：{card.experiment_plan.hypothesis}",
            f"- 信息价值最高的下一实验：{card.experiment_plan.highest_information_next_experiment}",
            f"- 停止条件：{'；'.join(card.experiment_plan.stop_conditions)}", "",
        ])
    if tier_section:
        lines.extend(tier_section)
    lines.extend(["## Reviewer 结论", ""])
    if findings:
        for finding in findings:
            lines.append(f"- `{finding.severity}` / `{finding.category}`：{finding.message} 处理：{finding.required_action}")
    else:
        lines.append("- 未发现 blocking / major / minor 问题。")
    lines.extend([
        "", "## 使用边界", "",
        "- 组学差异、数据库关联和扰动相关均不能单独证明疾病因果。",
        "- 低上下文匹配的预测不得进入正式得分。",
        "- 实验方案是可证伪的研究建议，不替代伦理、临床或药物安全决策。",
    ])
    return report, "\n".join(lines) + "\n"


def build_mch_report(
    task: TaskSpec,
    status: TerminalStatus,
    result: ToolResult | None,
    findings: list[ReviewerFinding],
) -> tuple[dict[str, Any], str]:
    outputs = result.outputs if result else {"covered": False}
    report = {
        "contract_version": CONTRACT_VERSION, "task_id": task.task_id, "status": status.value,
        "question": task.question, "mch": outputs,
        "reviewer_findings": [item.model_dump(mode="json") for item in findings],
    }
    if not outputs.get("covered"):
        return report, f"# MCH 因果建模金样板\n\n- 终态：`{status.value}`\n- 当前工具仅覆盖MCH；非MCH输入不会生成固定图。\n"
    paper = outputs["paper_result"]
    project = outputs["project_replication"]
    text = (
        "# MCH 因果建模金样板\n\n"
        f"- 终态：`{status.value}`\n"
        f"- 论文配置：方向预测 {paper['direction_prediction']['correct']}/{paper['direction_prediction']['total']}；Fig.3a β={paper['fig3a']['beta']}，P={paper['fig3a']['p_value']}。\n"
        f"- 项目扩展配置：方向预测 {project['direction_prediction']['correct']}/{project['direction_prediction']['total']}；置换 P={project['direction_prediction']['permutation_p']}。\n"
        f"- 项目 Fig.3a：β={project['fig3a']['beta']}，P={project['fig3a']['p_value']}。\n\n"
        "论文43/59与项目94/147使用不同命中集合，必须分开报告。该图只适用于K562→程序→MCH配置，不能作为疾病主Demo的因果证据。\n"
    )
    return report, text


def write_report(run_dir: Path, payload: dict[str, Any], markdown: str) -> None:
    (run_dir / "report.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
