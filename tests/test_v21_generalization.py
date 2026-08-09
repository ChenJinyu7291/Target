import gzip
import json

import pytest

from target_agent.contracts import (
    AnalysisRecipe, CoverageStatus, DatasetCandidate, OmicsInput, TaskContext, TaskSpec,
    ToolCapability, ToolResult, ToolStatus,
)
from target_agent.llm import StepClient
from target_agent.planner import Planner
from target_agent.settings import load_settings
from target_agent.tools.base import ToolContext, ToolRegistry
from target_agent.tools.omics import (
    DiseaseResolverTool, GEOMetadataAuditTool, GEOSearchTool,
    OmicsRecipeBuilderTool, SingleCellAnalysisTool, _prepare_counts,
    _analysis_cache_key, _analysis_cache_legacy_key, _analysis_cache_locate,
)


class FakeResponse:
    def __init__(self, payload=None, content=b"", text="", status=200, headers=None):
        self._payload = payload
        self.content = content
        self.text = text
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class GeoSearchSession:
    def __init__(self):
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        if url.endswith("esearch.fcgi"):
            return FakeResponse({"esearchresult": {"idlist": ["1"]}})
        return FakeResponse({"result": {"uids": ["1"], "1": {
            "accession": "GSE53697", "title": "Alzheimer disease and control brain RNA-seq",
            "summary": "Case control expression profiling", "taxon": "Homo sapiens",
            "gdstype": "Expression profiling by high throughput sequencing", "n_samples": 17,
        }}})

    def post(self, url, data=None, timeout=None):
        return self.get(url, params=data, timeout=timeout)


def task(disease="Alzheimer disease"):
    return TaskSpec(
        task_type="disease_to_target", question=f"Find targets for {disease}",
        context=TaskContext(disease=disease, organism="Homo sapiens", tissue="brain", assay="RNA-seq"),
    )


def context(tmp_path, spec=None, prior=None):
    return ToolContext(
        task=spec or task(), run_dir=tmp_path / "run", cache_dir=tmp_path / "cache",
        candidate_genes=[], prior_results=prior or [], settings=load_settings(),
    )


def test_generic_fallback_plan_contains_dynamic_discovery(tmp_path):
    registry = ToolRegistry([DiseaseResolverTool(), GEOSearchTool(session=GeoSearchSession())])
    plan = Planner(None, registry).create_plan(task())
    assert [step.tool for step in plan.steps if step.tool] == ["disease_resolver", "geo_search"]
    assert "uc_omics_snapshot" not in plan.model_dump_json()


def test_planner_respects_declared_omics_modes(tmp_path):
    spec = task().model_copy(deep=True)
    spec.constraints.dataset_selection.omics_modes = ["geo_bulk"]
    plan = Planner(None, ToolRegistry([
        DiseaseResolverTool(), GEOSearchTool(session=GeoSearchSession()),
    ])).create_plan(spec)
    tools = [step.tool for step in plan.steps if step.tool]
    assert "geo_search" in tools
    assert "cellxgene_discovery" not in tools
    assert "single_cell_analysis" not in tools


def test_step_planner_repairs_one_invalid_structured_response():
    class RepairingClient:
        model = "step-test"
        last_request_meta = {}

        def __init__(self):
            self.calls = 0

        def json_completion(self, system, user):
            self.calls += 1
            if self.calls == 1:
                return {"unexpected": "invalid"}
            return {"steps": [
                {"step_id": "scope", "name": "Normalize disease", "tool": "disease_resolver"},
                {"step_id": "geo", "name": "Search GEO", "tool": "geo_search", "dependencies": ["scope"]},
            ]}

    client = RepairingClient()
    registry = ToolRegistry([DiseaseResolverTool(), GEOSearchTool(session=GeoSearchSession())])
    plan = Planner(client, registry).create_plan(task())
    assert client.calls == 2
    assert plan.planner_backend == "step:step-test:repaired"
    assert plan.fallback_used is False
    assert client.last_request_meta == {"structured_attempts": 2, "repair_used": True}


def test_step_client_records_response_body_request_id():
    class CompletionResponse:
        status_code = 200
        headers = {}

        def json(self):
            return {
                "id": "chatcmpl-test-request",
                "choices": [{"message": {"content": '{"steps": []}'}}],
            }

    class CompletionSession:
        def post(self, *args, **kwargs):
            return CompletionResponse()

    client = StepClient(
        api_key="test-secret-not-real", model="step-test", base_url="https://example.invalid/v1",
        session=CompletionSession(),
    )
    assert client.json_completion("system", "user") == {"steps": []}
    assert client.last_request_meta["request_id"] == "chatcmpl-test-request"


def test_geo_search_is_disease_driven_and_not_accession_hardcoded(tmp_path):
    resolver = DiseaseResolverTool().run(context(tmp_path)).result
    execution = GEOSearchTool(session=GeoSearchSession()).run(context(tmp_path, prior=[resolver]))
    assert execution.result.status == ToolStatus.SUCCESS
    assert execution.result.outputs["dataset_candidates"][0]["accession"] == "GSE53697"
    assert "Alzheimer" in execution.result.inputs["query"]


class GeoAuditSession:
    def __init__(self, matrix_text):
        self.headers = {}
        self.matrix = gzip.compress(matrix_text.encode())

    def get(self, url, timeout=None):
        if url.endswith("series_matrix.txt.gz"):
            return FakeResponse(content=self.matrix)
        return FakeResponse(text='<a href="GSE53697_counts.tsv.gz">counts</a>')


def test_metadata_audit_enforces_three_biological_replicates(tmp_path):
    ids = [f"GSM{i}" for i in range(1, 7)]
    titles = ["control 1", "control 2", "control 3", "Alzheimer case 1", "Alzheimer case 2", "Alzheimer case 3"]
    matrix = "\n".join([
        "!Sample_geo_accession\t" + "\t".join(f'\"{value}\"' for value in ids),
        "!Sample_title\t" + "\t".join(f'\"{value}\"' for value in titles),
        "!Sample_source_name_ch1\t" + "\t".join('"brain"' for _ in ids),
        "!series_matrix_table_begin", '"ID_REF"\t' + "\t".join(f'\"{value}\"' for value in ids),
        '"GENE1"\t1\t2\t3\t8\t9\t10', "!series_matrix_table_end",
    ])
    candidate = DatasetCandidate(
        accession="GSE53697", source="GEO", title="AD", organism="Homo sapiens",
        disease="Alzheimer disease", source_uri="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE53697",
        context_match_score=1.0,
    )
    search = ToolResult(
        tool_name="geo_search", tool_version="test", status=ToolStatus.SUCCESS,
        coverage_status=CoverageStatus.COVERED, context_match_score=1,
        outputs={"dataset_candidates": [candidate.model_dump(mode="json")]}, capability=ToolCapability(),
    )
    execution = GEOMetadataAuditTool(session=GeoAuditSession(matrix)).run(context(tmp_path, prior=[search]))
    selected = execution.result.outputs["selected_datasets"]
    assert selected[0]["candidate"]["case_count"] == 3
    assert selected[0]["candidate"]["control_count"] == 3
    assert selected[0]["candidate"]["eligibility"] == "eligible"


def test_metadata_audit_collapses_technical_libraries(tmp_path):
    ids = [f"GSM{i}" for i in range(1, 9)]
    titles = [
        "control rep1 cDNA", "control rep1 oligo", "control rep2", "control rep3",
        "Alzheimer case rep1 cDNA", "Alzheimer case rep1 oligo", "Alzheimer case rep2", "Alzheimer case rep3",
    ]
    matrix = "\n".join([
        "!Sample_geo_accession\t" + "\t".join(f'"{value}"' for value in ids),
        "!Sample_title\t" + "\t".join(f'"{value}"' for value in titles),
        "!Sample_source_name_ch1\t" + "\t".join('"brain"' for _ in ids),
        "!series_matrix_table_begin", '"ID_REF"\t' + "\t".join(f'"{value}"' for value in ids),
        '"GENE1"\t' + "\t".join(str(value) for value in range(1, 9)), "!series_matrix_table_end",
    ])
    candidate = DatasetCandidate(
        accession="GSE53697", source="GEO", title="AD bulk", organism="Homo sapiens",
        disease="Alzheimer disease", source_uri="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE53697",
        context_match_score=1.0,
    )
    search = ToolResult(
        tool_name="geo_search", tool_version="test", status=ToolStatus.SUCCESS,
        coverage_status=CoverageStatus.COVERED, context_match_score=1,
        outputs={"dataset_candidates": [candidate.model_dump(mode="json")]}, capability=ToolCapability(),
    )
    execution = GEOMetadataAuditTool(session=GeoAuditSession(matrix)).run(context(tmp_path, prior=[search]))
    selected = execution.result.outputs["selected_datasets"][0]
    assert selected["candidate"]["case_count"] == 3
    assert selected["candidate"]["control_count"] == 3


def test_recipe_selects_pydeseq2_for_count_file(tmp_path):
    candidate = DatasetCandidate(
        accession="GSE104854", source="GEO", title="LUAD counts", source_uri="https://example.org/GSE104854",
        processed_files=["https://ftp.ncbi.nlm.nih.gov/geo/series/GSE104nnn/GSE104854/suppl/GSE104854_All_Counts.txt.gz"],
        eligibility="eligible", metadata_confidence=1, context_match_score=1,
    )
    audit = ToolResult(
        tool_name="geo_metadata_audit", tool_version="test", status=ToolStatus.SUCCESS,
        coverage_status=CoverageStatus.COVERED, context_match_score=1,
        outputs={"selected_datasets": [{"candidate": candidate.model_dump(mode="json"), "group_mapping": {
            "GSM1": "control", "GSM2": "control", "GSM3": "control", "GSM4": "case", "GSM5": "case", "GSM6": "case",
        }, "sample_aliases": {}, "series_matrix_uri": ""}]}, capability=ToolCapability(),
    )
    execution = OmicsRecipeBuilderTool().run(context(tmp_path, spec=task("lung adenocarcinoma"), prior=[audit]))
    recipe = execution.result.outputs["analysis_recipes"][0]
    assert recipe["backend"] == "pydeseq2"
    assert recipe["data_kind"] == "bulk_counts"


def test_pydeseq2_preflight_rejects_normalized_expression():
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({
        "gene": ["A", "B"], "control_1": [1.2, 2.3], "control_2": [1.1, 2.4], "control_3": [1.0, 2.1],
        "case_1": [3.4, 4.2], "case_2": [3.2, 4.1], "case_3": [3.3, 4.0],
    })
    recipe = AnalysisRecipe(
        accession="GSETEST", data_kind="bulk_counts", backend="pydeseq2",
        input_uri="https://ftp.ncbi.nlm.nih.gov/test.tsv", group_mapping={}, design="~condition",
        contrast=["condition", "case", "control"],
    )
    with pytest.raises(TypeError, match="integer counts"):
        _prepare_counts(frame, recipe, 3)


def test_pydeseq2_preserves_unlabelled_gene_index():
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame(
        [[10, 11, 9, 30, 31, 29], [5, 4, 6, 7, 8, 7]],
        index=["GENE_A", "GENE_B"],
        columns=["control_1", "control_2", "control_3", "case_1", "case_2", "case_3"],
    )
    recipe = AnalysisRecipe(
        accession="GSETEST", data_kind="bulk_counts", backend="pydeseq2",
        input_uri="https://ftp.ncbi.nlm.nih.gov/test.tsv", group_mapping={}, design="~condition",
        contrast=["condition", "case", "control"],
    )
    counts, _, gene_column = _prepare_counts(frame, recipe, 3)
    assert list(counts.columns) == ["GENE_A", "GENE_B"]
    assert gene_column == "__index__"


def test_analysis_cache_key_ignores_per_run_recipe_identity(tmp_path):
    first = AnalysisRecipe(
        accession="GSETEST", data_kind="bulk_counts", backend="pydeseq2",
        input_uri="https://ftp.ncbi.nlm.nih.gov/test.tsv", design="~condition",
        contrast=["condition", "case", "control"],
    )
    second = first.model_copy(update={"recipe_id": "recipe-another-run"})
    tool_context = context(tmp_path)
    assert _analysis_cache_key(tool_context, first, "abc", "2.1.1") == _analysis_cache_key(
        tool_context, second, "abc", "2.1.1"
    )


def test_dotenv_auto_load_and_process_env_precedence(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("STEP_MODEL=from-dotenv\nTARGET_AGENT_WEB_WORKERS=3\n", encoding="utf-8")
    monkeypatch.setenv("STEP_MODEL", "from-process")
    settings = load_settings(dotenv)
    assert settings.step_model == "from-process"
    assert settings.web_workers == 3


def test_single_cell_missing_donor_metadata_is_not_formal_evidence(tmp_path):
    ad = pytest.importorskip("anndata")
    np = pytest.importorskip("numpy")
    pytest.importorskip("scanpy")
    pd = pytest.importorskip("pandas")
    data = ad.AnnData(
        X=np.ones((12, 5), dtype=int),
        obs=pd.DataFrame({"cell_type": ["T cell"] * 12, "condition": ["control"] * 6 + ["case"] * 6}),
    )
    path = tmp_path / "missing_donor.h5ad"
    data.write_h5ad(path)
    spec = TaskSpec(
        task_type="disease_to_target", question="Find disease targets",
        context=TaskContext(disease="Alzheimer disease", cell_type="T cell"),
        omics_inputs=[OmicsInput(uri=str(path), data_kind="h5ad")],
    )
    execution = SingleCellAnalysisTool().run(context(tmp_path, spec=spec))
    assert execution.result.coverage_status == CoverageStatus.NOT_COVERED
    assert execution.result.outputs["formal_score_eligible"] is False
    assert any("missing donor_id" in warning for warning in execution.result.warnings)


def test_single_cell_runs_donor_level_pseudobulk(tmp_path):
    ad = pytest.importorskip("anndata")
    np = pytest.importorskip("numpy")
    pytest.importorskip("scanpy")
    pd = pytest.importorskip("pandas")
    rng = np.random.default_rng(7)
    donors = [f"d{index}" for index in range(6)]
    conditions = ["control"] * 3 + ["case"] * 3
    obs_rows = []
    matrices = []
    for donor, condition in zip(donors, conditions):
        block = rng.poisson(8, size=(20, 30))
        block[:, 0] += rng.poisson(2 if condition == "control" else 35, size=20)
        matrices.append(block)
        obs_rows.extend({"cell_type": "T cell", "donor_id": donor, "condition": condition} for _ in range(20))
    counts = np.vstack(matrices).astype(int)
    data = ad.AnnData(
        X=counts.copy(), obs=pd.DataFrame(obs_rows),
        var=pd.DataFrame(index=[f"GENE{index}" for index in range(30)]),
    )
    data.layers["counts"] = counts.copy()
    path = tmp_path / "pseudobulk.h5ad"
    data.write_h5ad(path)
    spec = TaskSpec(
        task_type="disease_to_target", question="Find disease targets",
        context=TaskContext(disease="Alzheimer disease", cell_type="T cell"),
        omics_inputs=[OmicsInput(uri=str(path), data_kind="h5ad")],
    )
    execution = SingleCellAnalysisTool().run(context(tmp_path, spec=spec))
    assert execution.result.status == ToolStatus.SUCCESS, execution.result.warnings
    assert execution.result.outputs["formal_score_eligible"] is True
    assert execution.result.outputs["omics_results"][0]["donors_per_condition"] == {"control": 3, "case": 3}
    assert execution.result.artifacts

def test_analysis_cache_key_is_task_context_free(tmp_path):
    recipe = AnalysisRecipe(
        accession="GSETEST", data_kind="bulk_counts", backend="pydeseq2",
        input_uri="https://ftp.ncbi.nlm.nih.gov/test.tsv", design="~condition",
        contrast=["condition", "case", "control"],
    )
    alz = context(tmp_path)
    pd_ctx = context(tmp_path, spec=task(disease="Parkinson disease"))
    assert _analysis_cache_key(alz, recipe, "abc", "2.1.1") == _analysis_cache_key(
        pd_ctx, recipe, "abc", "2.1.1"
    )
    assert _analysis_cache_legacy_key(alz, recipe, "abc", "2.1.1") != _analysis_cache_legacy_key(
        pd_ctx, recipe, "abc", "2.1.1"
    )


def test_analysis_cache_locate_migrates_legacy_key(tmp_path):
    recipe = AnalysisRecipe(
        accession="GSETEST", data_kind="bulk_counts", backend="pydeseq2",
        input_uri="https://ftp.ncbi.nlm.nih.gov/test.tsv", design="~condition",
        contrast=["condition", "case", "control"],
    )
    tool_context = context(tmp_path)
    legacy_key = _analysis_cache_legacy_key(tool_context, recipe, "abc", "2.1.1")
    legacy_dir = tmp_path / "cache" / "analysis" / "bulk" / legacy_key
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "differential.csv").write_text("gene,baseMean\nIL2,10\n", encoding="utf-8")
    path, key, mode = _analysis_cache_locate(tool_context, recipe, "abc", "2.1.1")
    assert mode == "migrated"
    assert path.is_file()
    assert path.parent.name == key
    path2, key2, mode2 = _analysis_cache_locate(tool_context, recipe, "abc", "2.1.1")
    assert mode2 == "new" and key2 == key


def test_metadata_audit_rejects_disease_mismatch_and_unverifiable_units(tmp_path):
    ids = [f"GSM{i}" for i in range(1, 7)]
    titles = [
        "control brain", "control brain", "control brain",
        "Alzheimer brain", "Alzheimer brain", "Alzheimer brain",
    ]
    matrix = "\n".join([
        "!Sample_geo_accession\t" + "\t".join(f'"{value}"' for value in ids),
        "!Sample_title\t" + "\t".join(f'"{value}"' for value in titles),
        "!Sample_source_name_ch1\t" + "\t".join('"brain"' for _ in ids),
        "!series_matrix_table_begin", '"ID_REF"\t' + "\t".join(f'"{value}"' for value in ids),
        '"GENE1"\t1\t2\t3\t8\t9\t10', "!series_matrix_table_end",
    ])
    candidate = DatasetCandidate(
        accession="GSE53697", source="GEO", title="Crohn study", organism="Homo sapiens",
        disease="Crohn disease",
        source_uri="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE53697",
        context_match_score=1.0,
    )
    search = ToolResult(
        tool_name="geo_search", tool_version="test", status=ToolStatus.SUCCESS,
        coverage_status=CoverageStatus.COVERED, context_match_score=1,
        outputs={"dataset_candidates": [candidate.model_dump(mode="json")]}, capability=ToolCapability(),
    )
    execution = GEOMetadataAuditTool(session=GeoAuditSession(matrix)).run(context(tmp_path, prior=[search]))
    rejected = execution.result.outputs["rejected_datasets"]
    assert rejected
    reasons = rejected[0]["candidate"]["exclusion_reasons"]
    assert "disease_context_mismatch" in reasons
    assert "biological_unit_unverifiable" in reasons
    assert rejected[0]["biological_unit_coverage"] < 1.0
    assert rejected[0]["biological_unit_fallback_samples"]


def test_continuous_expression_rejects_raw_counts_and_marks_log_scale(tmp_path):
    import pandas as pd

    from target_agent.tools.omics import _prepare_continuous_expression

    recipe = AnalysisRecipe(
        accession="GSETEST", data_kind="bulk_continuous_expression", backend="limma",
        input_uri="https://ftp.ncbi.nlm.nih.gov/test.tsv",
        group_mapping={
            "control_1": "control", "control_2": "control", "control_3": "control",
            "case_1": "case", "case_2": "case", "case_3": "case",
        },
        design="~condition", contrast=["condition", "case", "control"],
        parameters={"sample_aliases": {}, "biological_units": {}},
    )
    raw_counts = pd.DataFrame(
        [[10000, 11000, 9000, 30000, 31000, 29000], [5000, 4000, 6000, 7000, 8000, 7000]],
        index=["GENE_A", "GENE_B"],
        columns=["control_1", "control_2", "control_3", "case_1", "case_2", "case_3"],
    )
    with pytest.raises(ValueError, match="raw or unlogged"):
        _prepare_continuous_expression(raw_counts, recipe, 3)
    log_scale = pd.DataFrame(
        [[-1.2, -1.1, -1.0, 1.4, 1.3, 1.2], [0.5, 0.4, 0.6, 2.1, 2.2, 2.0]],
        index=["GENE_A", "GENE_B"],
        columns=["control_1", "control_2", "control_3", "case_1", "case_2", "case_3"],
    )
    expression, metadata, gene_column, normalization_unverified = _prepare_continuous_expression(
        log_scale, recipe, 3
    )
    assert normalization_unverified is False
    assert expression.shape[0] == 6
    assert gene_column == "__index__"
