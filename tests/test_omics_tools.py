"""P1-3 and P1-7: DiseaseResolver failure is explicit; pathway versioning is honest."""
from __future__ import annotations

from pathlib import Path

from target_agent.contracts import CoverageStatus, TaskContext, TaskSpec, ToolStatus
from target_agent.settings import Settings
from target_agent.tools.base import ToolContext
from target_agent.tools.omics import DiseaseResolverTool, PathwayEnrichmentTool


def _context(tmp_path: Path, disease: str) -> ToolContext:
    return ToolContext(
        task=TaskSpec(
            task_type="disease_to_target", question=f"Find targets for {disease}",
            context=TaskContext(disease=disease, tissue="colon"),
        ),
        run_dir=tmp_path / "run", cache_dir=tmp_path / "cache",
        candidate_genes=["GENE1"],
        settings=Settings(_env_file=None, TARGET_AGENT_CACHE_ONLY=True),
    )


def test_resolver_unresolved_is_partial_not_covered_zero_score(tmp_path):
    execution = DiseaseResolverTool().run(_context(tmp_path, "not-a-real-disease-xyz"))
    result = execution.result
    assert result.outputs["identifier_source"] == "unresolved"
    assert result.status == ToolStatus.PARTIAL
    assert result.coverage_status == CoverageStatus.NOT_COVERED
    assert result.context_match_score == 0.0
    assert result.outputs["warnings"] == ["disease_identifier_unresolved"]
    assert result.warnings == ["disease_identifier_unresolved"]


def test_resolver_success_path_is_preserved(tmp_path):
    context = _context(tmp_path, "ulcerative colitis")
    context.task.context.disease_id = "MONDO_0005101"
    result = DiseaseResolverTool().run(context).result
    assert result.outputs["identifier_source"] == "user"
    assert result.outputs["covered"] is True
    assert result.status == ToolStatus.SUCCESS
    assert result.coverage_status == CoverageStatus.COVERED
    assert result.context_match_score == 1.0
    assert "warnings" not in result.outputs


def test_pathway_data_version_is_local_gseapy_and_not_retrieved(tmp_path):
    execution = PathwayEnrichmentTool().run(_context(tmp_path, "ulcerative colitis"))
    result = execution.result
    assert result.data_version.startswith("MSigDB_Hallmark_2020:gseapy:")
    assert "gseapy" in result.data_version
    assert "retrieved:" not in result.data_version
    assert any("no online retrieval" in limitation for limitation in result.limitations)
    assert result.outputs["context_score_origin"] == "tool_estimate"