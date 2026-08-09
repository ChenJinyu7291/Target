"""Typed, allowlisted public-omics discovery and analysis tools."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from ..contracts import (
    CONTRACT_VERSION, AnalysisRecipe, ArtifactRef, ClaimClass, CoverageStatus, DatasetCandidate,
    EvidenceContext, EvidenceItem, SkillRef, SourceLocator, Stance,
    ToolCapability, ToolDescriptor, ToolResult, ToolStatus, new_id, utc_now,
)
from ..llm import LLMUnavailable, StepClient
from .base import ScientificTool, ToolContext, ToolExecution


EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
GEO_FTP = "https://ftp.ncbi.nlm.nih.gov/geo/series"
OLS_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"
SKILL_COMMIT = "ad21a3868923628330734375dddbf7b86ea84222"
SKILL_VERSION = "2.62.0"
SKILL_SHA256 = {
    "bulk-rnaseq": "d772d1a3bfc5592efe26bad83abee1a0fcdcac602648183fe897106334b934d6",
    "pydeseq2": "37b4f9f73a1159af3899bb172dc935fdd6474d2bb0080471de30352ad4914a0a",
    "pathway-enrichment": "d830887a58d4c967563f0427d16e545c327d5660bf9c4547db8722ddc173e15e",
    "scanpy": "dd17164b3bc609c486c9bfc6a68ecb43dd9a17b73f530b41f4f110e3f0b0ee96",
    "anndata": "ce9f6f50c5aefc4c86218ebfa361f40c219653a4a69e82273535cb91bde0875e",
    "cellxgene-census": "54e49ca66e7b464cd46c70bb2a2c910c4af38b35eac441a19ba2cea362f2a5e9",
}


def _skill(name: str, note: str | None = None) -> SkillRef:
    return SkillRef(
        name=name,
        upstream_version=SKILL_VERSION,
        upstream_commit=SKILL_COMMIT,
        source_uri=f"https://github.com/K-Dense-AI/scientific-agent-skills/tree/{SKILL_COMMIT}/scientific_skills/{name}",
        sha256=SKILL_SHA256.get(name),
        adaptation_note=note,
    )


def _capability(scope: str, *, cells: bool = False) -> ToolCapability:
    return ToolCapability(
        supported_organisms=["Homo sapiens", "Mus musculus"],
        supported_tissues=["public-data dependent"],
        supported_cell_types=["standardized metadata required"] if cells else ["bulk or metadata dependent"],
        training_scope="not applicable",
        validation_scope=scope,
    )


def _latest(context: ToolContext, tool_name: str) -> ToolResult | None:
    return next((item for item in reversed(context.prior_results) if item.tool_name == tool_name), None)


def _safe_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _analysis_cache_key(context: ToolContext, recipe: AnalysisRecipe, source_sha256: str, tool_version: str) -> str:
    # recipe_id is a per-run trace handle, not part of the scientific recipe.
    # Including it would force an identical dataset/contrast to recompute on
    # every run and defeat checksum-bound analysis caching.
    # The key is deliberately task-context-free: the computed differential
    # result is a property of the dataset, recipe and source bytes, not of the
    # query disease. Evidence generated from a cached result still carries the
    # current run's disease/tissue context.
    payload = {
        "contract_version": CONTRACT_VERSION,
        "tool_version": tool_version,
        "source_sha256": source_sha256,
        "recipe": recipe.model_dump(mode="json", exclude={"recipe_id"}),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _analysis_cache_legacy_key(context: ToolContext, recipe: AnalysisRecipe, source_sha256: str, tool_version: str) -> str:
    """Key used by caches that predate the task-context-free analysis key."""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "tool_version": tool_version,
        "source_sha256": source_sha256,
        "recipe": recipe.model_dump(mode="json", exclude={"recipe_id"}),
        "task_context": context.task.context.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _analysis_cache_locate(context: ToolContext, recipe: AnalysisRecipe, source_sha256: str, tool_version: str) -> tuple[Path, str, str]:
    """Return (result_path, key, mode) with mode in new|migrated|miss.

    New keys are task-context-free; legacy keys (which included task context)
    are still honored and copied into the new layout so already-warm caches
    keep working during the migration.
    """
    key = _analysis_cache_key(context, recipe, source_sha256, tool_version)
    result_path = context.cache_dir / "analysis" / "bulk" / key / "differential.csv"
    if result_path.is_file():
        return result_path, key, "new"
    legacy_key = _analysis_cache_legacy_key(context, recipe, source_sha256, tool_version)
    legacy_path = context.cache_dir / "analysis" / "bulk" / legacy_key / "differential.csv"
    if legacy_path.is_file():
        result_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, result_path)
        return result_path, key, "migrated"
    return result_path, key, "miss"


def _geo_bucket(accession: str) -> str:
    match = re.fullmatch(r"GSE(\d+)", accession.upper())
    if not match:
        raise ValueError("invalid GEO Series accession")
    digits = match.group(1)
    return f"GSE{digits[:-3]}nnn" if len(digits) > 3 else "GSEnnn"


def _geo_paths(accession: str) -> tuple[str, str]:
    base = f"{GEO_FTP}/{_geo_bucket(accession)}/{accession}"
    return f"{base}/matrix/{accession}_series_matrix.txt.gz", f"{base}/suppl/"


def _quoted_values(line: str) -> list[str]:
    return [value.strip('"') for value in next(csv.reader([line], delimiter="\t"))[1:]]


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _package(name: str) -> str | None:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return None


class DiseaseResolverTool(ScientificTool):
    name = "disease_resolver"
    version = "2.1.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="scope",
        description="Normalize disease labels with user, curated or EBI OLS ontology identifiers and stable search synonyms.",
        output_types=["normalized_disease", "search_synonyms"], critical=True,
        execution_policy="typed_wrapper",
    )

    ALIASES = {
        "ulcerative colitis": ("ulcerative colitis", ["ulcerative colitis", "UC", "inflammatory bowel disease"], "MONDO_0005101"),
        "uc": ("ulcerative colitis", ["ulcerative colitis", "UC", "inflammatory bowel disease"], "MONDO_0005101"),
        "阿尔茨海默病": ("Alzheimer disease", ["Alzheimer disease", "Alzheimer's disease", "AD"], "MONDO_0004975"),
        "alzheimer disease": ("Alzheimer disease", ["Alzheimer disease", "Alzheimer's disease", "AD"], "MONDO_0004975"),
        "alzheimer's disease": ("Alzheimer disease", ["Alzheimer disease", "Alzheimer's disease", "AD"], "MONDO_0004975"),
        "肺腺癌": ("lung adenocarcinoma", ["lung adenocarcinoma", "LUAD", "non-small cell lung cancer"], "EFO_0000571"),
        "lung adenocarcinoma": ("lung adenocarcinoma", ["lung adenocarcinoma", "LUAD", "non-small cell lung cancer"], "EFO_0000571"),
        "帕金森病": ("Parkinson disease", ["Parkinson disease", "Parkinson's disease", "PD"], "MONDO_0005180"),
        "parkinson disease": ("Parkinson disease", ["Parkinson disease", "Parkinson's disease", "PD"], "MONDO_0005180"),
    }

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    @classmethod
    def aliases(cls) -> dict[str, tuple[str, list[str], str]]:
        """Curated aliases, extended with the OLS-verified disease library when present.

        Hard-coded entries win on conflict so existing behaviour never regresses.
        """
        merged = dict(cls.ALIASES)
        try:
            from ..diseases import load_library

            for key, value in load_library().resolver_aliases().items():
                merged.setdefault(key, value)
        except Exception:
            pass
        return merged

    def _ontology(self, context: ToolContext, disease: str) -> tuple[str, list[str], str | None, str, bool]:
        key = hashlib.sha256(disease.casefold().encode("utf-8")).hexdigest()[:20]
        cache_path = context.cache_dir / "disease_ontology" / f"{key}.json"
        cached = False
        try:
            if context.settings.cache_only:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                cached = True
            else:
                response = self.session.get(
                    OLS_SEARCH,
                    params={"q": disease, "ontology": "mondo,efo", "rows": 10, "exact": "false"},
                    timeout=(10, 30),
                )
                response.raise_for_status()
                payload = response.json()
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            docs = payload.get("response", {}).get("docs", [])
            exact = next((row for row in docs if str(row.get("label") or "").casefold() == disease.casefold()), None)
            selected = exact or (docs[0] if docs else None)
            if selected:
                label = str(selected.get("label") or disease)
                synonyms = [label, disease, *(selected.get("synonym") or [])]
                return label, list(dict.fromkeys(str(item) for item in synonyms if item))[:8], selected.get("obo_id"), "EBI_OLS", cached
        except (requests.RequestException, ValueError, OSError, FileNotFoundError):
            pass
        return disease, [disease], None, "unresolved", cached

    def run(self, context: ToolContext) -> ToolExecution:
        disease = (context.task.context.disease or "").strip()
        aliases = self.aliases()
        cached = False
        if context.task.context.disease_id:
            alias = aliases.get(disease.casefold())
            normalized, synonyms = (alias[0], alias[1]) if alias else (disease, [disease])
            known_id = context.task.context.disease_id
            identifier_source = "user"
        elif disease.casefold() in aliases:
            normalized, synonyms, known_id = aliases[disease.casefold()]
            identifier_source = "curated_alias"
        elif disease:
            normalized, synonyms, known_id, identifier_source, cached = self._ontology(context, disease)
        else:
            normalized, synonyms, known_id, identifier_source = disease, [], None, "unresolved"
        resolver_unresolved = identifier_source == "unresolved"
        outputs = {
            "covered": not resolver_unresolved, "normalized_disease": normalized,
            "search_synonyms": list(dict.fromkeys([value for value in synonyms if value])),
            "disease_id": context.task.context.disease_id or known_id,
            "identifier_source": identifier_source,
        }
        if resolver_unresolved:
            outputs["warnings"] = ["disease_identifier_unresolved"]
        result = ToolResult(
            tool_name=self.name, tool_version=self.version,
            status=ToolStatus.PARTIAL if resolver_unresolved and disease else (
                ToolStatus.SUCCESS if disease else ToolStatus.OUT_OF_SCOPE
            ),
            coverage_status=CoverageStatus.NOT_COVERED if resolver_unresolved else CoverageStatus.COVERED,
            context_match_score=0.0 if resolver_unresolved else 1.0,
            inputs={"disease": disease}, outputs=outputs, capability=_capability("Disease-label normalization"),
            data_version="EBI-OLS-live-or-cache", code_version="2.1.0",
            parameters={"alias_table_version": "2026-08-03", "ontology_endpoint": OLS_SEARCH}, cached=cached,
            warnings=["disease_identifier_unresolved"] if resolver_unresolved else [],
        )
        return ToolExecution(result=result, evidence=[])


class GEOSearchTool(ScientificTool):
    name = "geo_search"
    version = "2.1.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="dataset_discovery",
        description="Search NCBI GEO Series through E-Utils and rank public expression datasets.",
        input_types=["TaskSpec"], output_types=["DatasetCandidate[]"], critical=False,
        execution_policy="read_only_connector", skills=[_skill("bulk-rnaseq")],
    )

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.setdefault("User-Agent", "TargetDiscoveryAgent/0.3 (NCBI GEO discovery)")

    @staticmethod
    def _score(row: dict[str, Any], disease_terms: list[str], tissue: str | None, assay: str | None) -> float:
        text = " ".join(str(row.get(key) or "") for key in ("title", "summary", "gdstype", "taxon")).casefold()
        disease = 1.0 if any(term.casefold() in text for term in disease_terms if len(term) > 2) else 0.0
        tissue_score = 1.0 if tissue and tissue.casefold() in text else (0.5 if not tissue else 0.0)
        assay_score = 1.0 if assay and assay.casefold() in text else (0.7 if "expression profiling" in text else 0.3)
        design = 1.0 if any(token in text for token in ("control", "case", "tumor", "normal", "disease")) else 0.5
        processed = 0.5
        count = int(row.get("n_samples") or row.get("n_samples_total") or 0)
        sample_score = min(1.0, count / 20.0) if count else 0.3
        return round(0.30 * disease + 0.20 * tissue_score + 0.15 * assay_score + 0.15 * design + 0.10 * processed + 0.10 * sample_score, 4)

    def _fetch(self, context: ToolContext, query: str, cache_path: Path) -> tuple[list[dict[str, Any]], bool]:
        if context.settings.cache_only:
            return json.loads(cache_path.read_text(encoding="utf-8")), True
        search_pool = min(200, max(50, context.task.constraints.dataset_selection.max_geo_candidates * 20))
        params: dict[str, Any] = {
            "db": "gds", "term": query, "retmode": "json",
            "retmax": search_pool, "sort": "relevance",
        }
        if context.settings.ncbi_api_key:
            params["api_key"] = context.settings.ncbi_api_key.get_secret_value()
        if context.settings.ncbi_email:
            params["email"] = context.settings.ncbi_email
        try:
            search = self.session.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=(10, 60))
            search.raise_for_status()
            ids = search.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                rows: list[dict[str, Any]] = []
            else:
                rows = []
                for start in range(0, len(ids), 200):
                    batch = ids[start:start + 200]
                    summary_params = {"db": "gds", "id": ",".join(batch), "retmode": "json"}
                    if "api_key" in params:
                        summary_params["api_key"] = params["api_key"]
                    summary = self.session.post(f"{EUTILS}/esummary.fcgi", data=summary_params, timeout=(10, 60))
                    summary.raise_for_status()
                    payload = summary.json().get("result", {})
                    rows.extend(payload[item] for item in payload.get("uids", batch) if item in payload)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            return rows, False
        except (requests.RequestException, ValueError, OSError):
            if cache_path.exists():
                return json.loads(cache_path.read_text(encoding="utf-8")), True
            raise

    def run(self, context: ToolContext) -> ToolExecution:
        started = time.perf_counter()
        if "geo_bulk" not in context.task.constraints.dataset_selection.omics_modes:
            return ToolExecution(result=ToolResult(
                tool_name=self.name, tool_version=self.version, status=ToolStatus.OUT_OF_SCOPE,
                coverage_status=CoverageStatus.NOT_COVERED, context_match_score=0.0,
                inputs={"omics_modes": context.task.constraints.dataset_selection.omics_modes},
                outputs={"dataset_candidates": []}, capability=_capability("NCBI GEO E-Utils Series metadata"),
                warnings=["geo_bulk_mode_disabled"],
            ), evidence=[])
        resolver = _latest(context, "disease_resolver")
        disease = (resolver.outputs.get("normalized_disease") if resolver else None) or context.task.context.disease or ""
        terms = (resolver.outputs.get("search_synonyms") if resolver else None) or [disease]
        preferred = context.task.constraints.dataset_selection.preferred_dataset_accessions
        if preferred:
            disease_clause = " OR ".join(f"{item}[ACCN]" for item in preferred)
        else:
            informative_terms = [item for item in terms if len(_normal(item)) >= 4] or [disease]
            disease_clause = " OR ".join(f'"{item}"[All Fields]' for item in informative_terms[:4])
        organism = context.task.context.organism or "Homo sapiens"
        # GEO's GDS index uses many assay-specific DataSet Type values. Restricting
        # the query to one literal value silently drops RNA-seq and many arrays, so
        # assay suitability is scored after Series retrieval instead.
        query = f"({disease_clause}) AND \"{organism}\"[Organism] AND gse[Entry Type]"
        key = hashlib.sha256(f"search-pool-v3|{query}".encode("utf-8")).hexdigest()[:20]
        cache_path = context.cache_dir / "geo" / "search" / f"{key}.json"
        try:
            rows, cached = self._fetch(context, query, cache_path)
        except (requests.RequestException, ValueError, OSError, FileNotFoundError) as exc:
            result = ToolResult(
                tool_name=self.name, tool_version=self.version, status=ToolStatus.FAILED,
                coverage_status=CoverageStatus.UNKNOWN, context_match_score=0.0,
                inputs={"query": query}, outputs={"dataset_candidates": []},
                capability=_capability("NCBI GEO E-Utils Series metadata"),
                error=f"GEO search failed: {exc.__class__.__name__}",
                limitations=["No GEO result was inferred when both live retrieval and cache were unavailable."],
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            return ToolExecution(result=result, evidence=[])
        excluded = set(context.task.constraints.dataset_selection.excluded_dataset_accessions)
        candidates: list[DatasetCandidate] = []
        for row in rows:
            accession = str(row.get("accession") or row.get("Accession") or "").upper()
            if not accession.startswith("GSE") or accession in excluded:
                continue
            title = str(row.get("title") or row.get("Title") or "")
            summary = str(row.get("summary") or row.get("Summary") or "")
            dataset_type = str(row.get("gdstype") or row.get("GDS Type") or "")
            if not any(token in dataset_type.casefold() for token in ("expression profiling", "transcript", "rna")):
                continue
            candidate = DatasetCandidate(
                accession=accession, source="GEO", title=title,
                organism=str(row.get("taxon") or row.get("Taxon") or organism),
                disease=disease, tissue=context.task.context.tissue,
                assay=dataset_type or context.task.context.assay or "expression profiling",
                sample_count=int(row.get("n_samples") or row.get("n_samples_total") or 0) or None,
                context_match_score=self._score(row, terms, context.task.context.tissue, context.task.context.assay),
                source_uri=f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}",
                source_version="NCBI-GEO-live-or-cache",
                exclusion_reasons=[] if summary or title else ["missing_title_and_summary"],
            )
            candidates.append(candidate)
        candidates.sort(key=lambda item: (-item.context_match_score, item.accession))
        payload = [item.model_dump(mode="json") for item in candidates[: context.task.constraints.dataset_selection.max_geo_candidates]]
        result = ToolResult(
            tool_name=self.name, tool_version=self.version,
            status=ToolStatus.SUCCESS if payload else ToolStatus.PARTIAL,
            coverage_status=CoverageStatus.COVERED if payload else CoverageStatus.NOT_COVERED,
            context_match_score=max((item.context_match_score for item in candidates), default=0.0),
            inputs={"query": query, "preferred_accessions": preferred},
            outputs={"dataset_candidates": payload, "query_backend": "NCBI E-Utils", "search_hit_count": len(payload)},
            capability=_capability("NCBI GEO E-Utils Series metadata"), data_version="NCBI-GEO-live-or-cache",
            code_version="2.1.0", parameters={
                "return_limit": context.task.constraints.dataset_selection.max_geo_candidates,
                "search_pool": min(200, max(50, context.task.constraints.dataset_selection.max_geo_candidates * 20)),
            },
            warnings=[] if payload else ["no_geo_series_found"],
            limitations=["Search hits are dataset candidates, not biological evidence."],
            cached=cached, elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=[])


class GEOMetadataAuditTool(ScientificTool):
    name = "geo_metadata_audit"
    version = "2.1.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="dataset_discovery",
        description="Audit GEO Series Matrix metadata, biological replication and processed-file availability.",
        input_types=["DatasetCandidate[]"], output_types=["DatasetCandidate[]", "sample_group_mapping"],
        execution_policy="typed_wrapper", skills=[_skill("bulk-rnaseq", "Replication and confounding gates")],
    )

    CONTROL = ("control", "healthy", "normal", "non tumor", "non-tumor", "adjacent normal", "unaffected", "no dementia")
    CASE = ("tumor", "cancer", "carcinoma", "disease", "patient", "alzheimer", "parkinson", "colitis", "case")

    def __init__(self, session: requests.Session | None = None, llm: StepClient | None = None):
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.setdefault("User-Agent", "TargetDiscoveryAgent/0.3 (NCBI GEO metadata audit)")
        self.llm = llm
        self._last_ncbi_request = 0.0

    def _get(self, url: str, timeout: tuple[int, int]) -> requests.Response:
        # NCBI permits no more than three requests per second without an API key.
        delay = 0.35 - (time.monotonic() - self._last_ncbi_request)
        if delay > 0:
            time.sleep(delay)
        last_error: requests.RequestException | None = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=timeout)
                self._last_ncbi_request = time.monotonic()
                if response.status_code in {429, 500, 502, 503, 504}:
                    time.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        assert last_error is not None
        raise last_error

    def _download_text(self, url: str, max_mb: int) -> str:
        response = self._get(url, timeout=(10, 90))
        if len(response.content) > max_mb * 1024 * 1024:
            raise ValueError("metadata file exceeds download limit")
        raw = gzip.decompress(response.content) if url.endswith(".gz") else response.content
        return raw.decode("utf-8", errors="replace")

    def _directory_files(self, url: str, pattern: str) -> list[str]:
        try:
            response = self._get(url, timeout=(10, 60))
        except requests.RequestException:
            return []
        links = re.findall(r'href=["\']([^"\']+)["\']', response.text, flags=re.I)
        return sorted({urljoin(url, link) for link in links if re.search(pattern, link, re.I)})

    def _supplementary_files(self, url: str) -> list[str]:
        return [
            item for item in self._directory_files(url, r"\.(?:txt|tsv|csv|xlsx?|gz|zip)$")
            if not item.casefold().endswith("/filelist.txt")
        ]

    def _matrix_files(self, matrix_uri: str) -> list[str]:
        directory = matrix_uri.rsplit("/", 1)[0] + "/"
        return self._directory_files(directory, r"_series_matrix\.txt\.gz$")

    @staticmethod
    def _samples(text: str) -> list[dict[str, str]]:
        fields: dict[str, list[str]] = {}
        characteristics: list[list[str]] = []
        for line in text.splitlines():
            if line.startswith("!Sample_geo_accession"):
                fields["accession"] = _quoted_values(line)
            elif line.startswith("!Sample_title"):
                fields["title"] = _quoted_values(line)
            elif line.startswith("!Sample_source_name_ch1"):
                fields["source"] = _quoted_values(line)
            elif line.startswith("!Sample_characteristics_ch1"):
                characteristics.append(_quoted_values(line))
        ids = fields.get("accession", [])
        samples = []
        for index, sample_id in enumerate(ids):
            attrs = [row[index] for row in characteristics if index < len(row)]
            samples.append({
                "sample_id": sample_id,
                "title": fields.get("title", [""] * len(ids))[index] if index < len(fields.get("title", [])) else "",
                "source": fields.get("source", [""] * len(ids))[index] if index < len(fields.get("source", [])) else "",
                "characteristics": " | ".join(attrs),
            })
        return samples

    def _deterministic_groups(self, samples: list[dict[str, str]], disease: str) -> tuple[dict[str, str], float]:
        mapping: dict[str, str] = {}
        disease_tokens = [token for token in re.split(r"\W+", disease.casefold()) if len(token) >= 4]
        for sample in samples:
            text = " ".join(sample.values()).casefold().replace("_", " ")
            if any(token in text for token in self.CONTROL):
                mapping[sample["sample_id"]] = "control"
            elif any(token in text for token in self.CASE) or any(token in text for token in disease_tokens):
                mapping[sample["sample_id"]] = "case"
        classified = len(mapping) / max(1, len(samples))
        confidence = 0.9 if classified == 1.0 and len(set(mapping.values())) == 2 else round(0.65 * classified, 3)
        return mapping, confidence

    @staticmethod
    def _biological_units(samples: list[dict[str, str]], mapping: dict[str, str]) -> dict[str, str]:
        units: dict[str, str] = {}
        label_pattern = re.compile(
            r"(?:donor|patient|subject|individual|sample|rep(?:licate)?|case|control|disease|normal|tumor|ctrl|healthy|wildtype|wt|knockout|ko)\s*[:#_-]?\s*([a-z0-9]+)",
            flags=re.I,
        )
        barcode_pattern = re.compile(r"\b(?:TCGA|TARGET|GTEX|GDC)-[A-Z0-9]{2,4}-[A-Z0-9]{4}\b", flags=re.I)
        for sample in samples:
            text = " | ".join((sample["title"], sample["source"], sample["characteristics"]))
            match = label_pattern.search(text) or barcode_pattern.search(text)
            if match:
                source = _safe_token(sample["source"].casefold()) or "sample"
                group = mapping.get(sample["sample_id"], "unclassified")
                unit_id = match.group(1).casefold() if match.lastindex else match.group(0).casefold()
                units[sample["sample_id"]] = f"{group}:{source}:{unit_id}"
            else:
                units[sample["sample_id"]] = sample["sample_id"]
        return units

    def _llm_groups(self, samples: list[dict[str, str]], disease: str) -> tuple[dict[str, str], float] | None:
        if not self.llm or not samples:
            return None
        system = (
            "Classify GEO samples using only supplied metadata. Return JSON with keys groups and confidence. "
            "groups maps every unambiguous sample_id to case, control, or exclude. Do not infer missing labels."
        )
        try:
            payload = self.llm.json_completion(system, json.dumps({"disease": disease, "samples": samples[:80]}, ensure_ascii=False))
        except LLMUnavailable:
            return None
        if not isinstance(payload, dict):
            return None
        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, dict):
            return None
        known = {sample["sample_id"] for sample in samples}
        groups = {str(k): str(v) for k, v in raw_groups.items() if k in known and v in {"case", "control", "exclude"}}
        confidence_raw = payload.get("confidence")
        if isinstance(confidence_raw, dict):  # some models nest e.g. {"score": 0.9, "rationale": ...}
            confidence_raw = next((v for v in confidence_raw.values() if isinstance(v, (int, float))), None)
            if confidence_raw is None:
                return None  # confidence was provided but unusable; distrust the whole payload
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else 0.0
        except (TypeError, ValueError):
            return None
        if not groups or not 0 <= confidence <= 1:
            return None
        return groups, confidence

    def run(self, context: ToolContext) -> ToolExecution:
        started = time.perf_counter()
        search = _latest(context, "geo_search")
        raw_candidates = (search.outputs.get("dataset_candidates") if search else []) or []
        resolver = _latest(context, "disease_resolver")
        disease = (resolver.outputs.get("normalized_disease") if resolver else None) or context.task.context.disease or ""
        minimum = context.task.constraints.dataset_selection.min_biological_replicates_per_group
        confidence_gate = context.task.constraints.dataset_selection.min_metadata_confidence
        max_mb = min(100, context.task.constraints.dataset_selection.max_download_mb)
        selected: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        audit_rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        used_llm = False
        cache_hits = 0
        for raw in raw_candidates:
            candidate = DatasetCandidate.model_validate(raw)
            matrix_url, suppl_url = _geo_paths(candidate.accession)
            disease_terms = [disease]
            if resolver is not None:
                disease_terms.extend(resolver.outputs.get("search_synonyms") or [])
            disease_hit = any(
                len(str(term).strip()) > 2
                and (
                    _normal(str(term)) in _normal(candidate.disease or "")
                    or _normal(str(term)) in _normal(candidate.title or "")
                )
                for term in disease_terms
            )
            audit_cache_key = hashlib.sha256(json.dumps({
                "contract_version": CONTRACT_VERSION,
                "tool_version": self.version,
                "accession": candidate.accession,
                "disease": disease,
                "tissue": context.task.context.tissue,
                "assay": context.task.context.assay,
                "minimum": minimum,
                "confidence_gate": confidence_gate,
                "metadata_model": context.settings.step_model if self.llm else "deterministic",
                "unit_parser": "v2",
            }, sort_keys=True).encode("utf-8")).hexdigest()
            audit_cache_path = context.cache_dir / "geo" / "metadata_audit" / f"{audit_cache_key}.json"
            if audit_cache_path.is_file():
                detail = json.loads(audit_cache_path.read_text(encoding="utf-8"))
                cached_candidate = DatasetCandidate.model_validate(detail["candidate"]).model_copy(update={
                    "title": candidate.title, "disease": candidate.disease, "tissue": candidate.tissue,
                    "assay": candidate.assay, "context_match_score": candidate.context_match_score,
                    "source_uri": candidate.source_uri,
                })
                detail["candidate"] = cached_candidate.model_dump(mode="json")
                audit_rows.append(detail)
                if cached_candidate.eligibility == "eligible":
                    if len(selected) < context.task.constraints.dataset_selection.max_datasets_to_analyze:
                        selected.append(detail)
                else:
                    rejected.append(detail)
                cache_hits += 1
                continue
            if context.settings.cache_only:
                detail = {
                    "candidate": candidate.model_copy(update={
                        "eligibility": "ineligible", "exclusion_reasons": ["metadata_audit_cache_missing"],
                    }).model_dump(mode="json"),
                    "group_mapping": {}, "sample_aliases": {}, "biological_units": {},
                    "series_matrix_uri": matrix_url,
                }
                audit_rows.append(detail)
                rejected.append(detail)
                continue
            try:
                matrix_files = self._matrix_files(matrix_url)
                if matrix_files:
                    matrix_url = matrix_files[0]
                text = self._download_text(matrix_url, max_mb)
                samples = self._samples(text)
                files = self._supplementary_files(suppl_url)
                llm_groups = self._llm_groups(samples, disease)
                if llm_groups:
                    mapping, confidence = llm_groups
                    used_llm = True
                else:
                    mapping, confidence = self._deterministic_groups(samples, disease)
                mapping = {key: value for key, value in mapping.items() if value in {"case", "control"}}
                biological_units = self._biological_units(samples, mapping)
                fallback_unit_samples = [
                    sample_id for sample_id in mapping
                    if biological_units.get(sample_id, sample_id) == sample_id
                ]
                unit_coverage = 1.0 - len(fallback_unit_samples) / max(1, len(mapping))
                grouped_units: dict[str, set[str]] = {"case": set(), "control": set()}
                unit_groups: dict[str, set[str]] = {}
                for sample_id, group in mapping.items():
                    unit = biological_units.get(sample_id, sample_id)
                    grouped_units[group].add(unit)
                    unit_groups.setdefault(unit, set()).add(group)
                counts = {group: len(units) for group, units in grouped_units.items()}
                reasons = []
                if disease and not disease_hit:
                    reasons.append("disease_context_mismatch")
                if mapping and unit_coverage < 1.0:
                    reasons.append("biological_unit_unverifiable")
                if counts["case"] < minimum or counts["control"] < minimum:
                    reasons.append(f"requires_at_least_{minimum}_biological_replicates_per_group")
                if confidence < confidence_gate:
                    reasons.append("metadata_confidence_below_threshold")
                if any(len(groups) > 1 for groups in unit_groups.values()):
                    reasons.append("biological_unit_assigned_to_multiple_conditions")
                if re.search(r"single[- ]cell|single[- ]nucleus|scRNA|snRNA", candidate.title, re.I):
                    reasons.append("single_cell_series_not_supported_by_bulk_template")
                if not files and not samples:
                    reasons.append("no_processed_matrix_or_sample_metadata")
                aliases = {
                    sample["sample_id"]: [sample["sample_id"], sample["title"], sample["source"]]
                    for sample in samples if sample["sample_id"] in mapping
                }
                audited = candidate.model_copy(update={
                    "case_count": counts["case"], "control_count": counts["control"],
                    "sample_count": len(samples), "processed_files": files or [matrix_url],
                    "metadata_confidence": confidence,
                    "eligibility": "eligible" if not reasons else "ineligible",
                    "exclusion_reasons": reasons,
                })
                detail = {
                    "candidate": audited.model_dump(mode="json"), "group_mapping": mapping,
                    "sample_aliases": aliases, "biological_units": biological_units,
                    "biological_unit_coverage": round(unit_coverage, 3),
                    "biological_unit_fallback_samples": fallback_unit_samples,
                    "series_matrix_uri": matrix_url,
                }
                audit_cache_path.parent.mkdir(parents=True, exist_ok=True)
                audit_cache_path.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
                audit_rows.append(detail)
                if reasons:
                    rejected.append(detail)
                elif len(selected) < context.task.constraints.dataset_selection.max_datasets_to_analyze:
                    selected.append(detail)
            except (requests.RequestException, ValueError, OSError, EOFError) as exc:
                detail = {
                    "candidate": candidate.model_copy(update={
                        "eligibility": "ineligible", "exclusion_reasons": [f"metadata_retrieval_failed:{exc.__class__.__name__}"],
                    }).model_dump(mode="json"),
                    "group_mapping": {}, "sample_aliases": {}, "series_matrix_uri": matrix_url,
                }
                audit_rows.append(detail)
                rejected.append(detail)
        selection_trace = []
        for row in audit_rows:
            if row in selected:
                decision, reasons = "selected", row["candidate"]["exclusion_reasons"]
            elif row["candidate"]["eligibility"] == "eligible":
                decision, reasons = "eligible_not_selected_limit", ["max_datasets_to_analyze_reached"]
            else:
                decision, reasons = "rejected", row["candidate"]["exclusion_reasons"]
            selection_trace.append({"accession": row["candidate"]["accession"], "decision": decision, "reasons": reasons})
        status = ToolStatus.SUCCESS if selected else ToolStatus.PARTIAL
        coverage = CoverageStatus.COVERED if selected else CoverageStatus.NOT_COVERED
        if rejected and selected:
            warnings.append("higher_ranked_or_additional_datasets_rejected_by_metadata_gate")
        result = ToolResult(
            tool_name=self.name, tool_version=self.version, status=status, coverage_status=coverage,
            context_match_score=max((row["candidate"]["context_match_score"] for row in selected), default=0.0),
            inputs={"candidate_count": len(raw_candidates)},
            outputs={
                "audited_datasets": audit_rows, "selected_datasets": selected,
                "rejected_datasets": rejected, "selection_trace": selection_trace,
                "metadata_classifier": "step+deterministic_gate" if used_llm else "deterministic_gate",
                "retry_performed": bool(rejected and selected),
            },
            capability=_capability("GEO Series Matrix sample metadata and official supplementary-file listing"),
            data_version="NCBI-GEO-live-or-cache", code_version="2.1.0",
            parameters={"min_replicates_per_group": minimum, "min_metadata_confidence": confidence_gate},
            warnings=warnings if selected else ["no_dataset_passed_metadata_gate"],
            limitations=["Automated group labels are accepted only after deterministic replication and confidence gates."],
            cached=bool(raw_candidates) and cache_hits == len(raw_candidates),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=[])


class OmicsRecipeBuilderTool(ScientificTool):
    name = "omics_recipe_builder"
    version = "2.1.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="omics",
        description="Instantiate only reviewed bulk-count or continuous-expression analysis templates.",
        input_types=["DatasetCandidate[]"], output_types=["AnalysisRecipe[]"],
        execution_policy="typed_wrapper", skills=[_skill("bulk-rnaseq"), _skill("pydeseq2")],
    )

    @staticmethod
    def _choose_file(files: list[str], matrix_uri: str) -> tuple[str, str, str] | None:
        supported = [url for url in files if re.search(r"\.(?:txt|tsv|csv)(?:\.gz)?$", url, re.I)]
        count_files = [url for url in supported if re.search(r"count|readcount|raw[_-]?matrix", url, re.I) and not re.search(r"tpm|fpkm|rpkm|normalized|norm", url, re.I)]
        if count_files:
            return count_files[0], "bulk_counts", "pydeseq2"
        continuous = [url for url in supported if re.search(r"tpm|fpkm|rpkm|normalized|norm|expression", url, re.I)]
        if continuous:
            return continuous[0], "bulk_continuous_expression", "limma"
        if matrix_uri:
            return matrix_uri, "bulk_continuous_expression", "limma"
        return None

    def run(self, context: ToolContext) -> ToolExecution:
        audit = _latest(context, "geo_metadata_audit")
        selected = (audit.outputs.get("selected_datasets") if audit else []) or []
        recipes: list[AnalysisRecipe] = []
        skipped: list[dict[str, str]] = []
        for item in selected:
            candidate = DatasetCandidate.model_validate(item["candidate"])
            chosen = self._choose_file(candidate.processed_files, item.get("series_matrix_uri", ""))
            if not chosen:
                skipped.append({"accession": candidate.accession, "reason": "no_supported_processed_matrix"})
                continue
            uri, kind, backend = chosen
            if backend == "limma" and not context.settings.enable_limma:
                skipped.append({"accession": candidate.accession, "reason": "limma_backend_not_enabled"})
                continue
            recipe = AnalysisRecipe(
                accession=candidate.accession, data_kind=kind, backend=backend, input_uri=uri,
                group_mapping=item.get("group_mapping", {}), design="~condition",
                contrast=["condition", "case", "control"],
                qc_thresholds={"min_total_gene_count": 10, "fdr": 0.05, "min_replicates_per_group": context.task.constraints.dataset_selection.min_biological_replicates_per_group},
                parameters={
                    "sample_aliases": item.get("sample_aliases", {}),
                    "biological_units": item.get("biological_units", {}),
                    "dataset_context_match_score": candidate.context_match_score,
                    "random_seed": context.settings.random_seed,
                },
                skill_refs=[_skill("bulk-rnaseq"), _skill("pydeseq2")],
                stop_conditions=["non_integer_matrix_for_pydeseq2", "group_mapping_below_minimum", "design_not_full_rank"],
                degradation_conditions=["optional_limma_backend_missing", "gene_identifier_mapping_incomplete"],
            )
            recipes.append(recipe)
        result = ToolResult(
            tool_name=self.name, tool_version=self.version,
            status=ToolStatus.SUCCESS if recipes else ToolStatus.PARTIAL,
            coverage_status=CoverageStatus.COVERED if recipes else CoverageStatus.NOT_COVERED,
            context_match_score=max((row["candidate"]["context_match_score"] for row in selected), default=0.0),
            inputs={"selected_datasets": len(selected)},
            outputs={"analysis_recipes": [recipe.model_dump(mode="json") for recipe in recipes], "skipped": skipped},
            capability=_capability("Reviewed bulk-count and optional limma templates"), code_version="2.1.0",
            warnings=[row["reason"] for row in skipped],
            limitations=["Raw FASTQ/SRA and arbitrary generated analysis code are outside this release."],
        )
        return ToolExecution(result=result, evidence=[])


def _download_file(context: ToolContext, accession: str, uri: str) -> tuple[Path, str, bool]:
    parsed = urlparse(uri)
    if parsed.scheme != "https" or parsed.netloc not in {"ftp.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
        raise ValueError("omics download host is not allowlisted")
    name = _safe_token(Path(parsed.path).name) or f"{accession}.matrix"
    destination = context.cache_dir / "geo" / "files" / accession / name
    if destination.exists():
        return destination, _sha256(destination), True
    if context.settings.cache_only:
        raise FileNotFoundError("omics file absent in cache-only mode")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".part")
    attempts: list[tuple[str, dict[str, str] | None]] = [(uri, None)]
    if parsed.netloc == "ftp.ncbi.nlm.nih.gov" and "/suppl/" in parsed.path:
        attempts.append((
            "https://www.ncbi.nlm.nih.gov/geo/download/",
            {"acc": accession, "file": Path(parsed.path).name, "format": "file"},
        ))
    last_error: requests.RequestException | None = None
    for download_url, params in attempts:
        try:
            with requests.get(download_url, params=params, stream=True, timeout=(10, 180)) as response:
                response.raise_for_status()
                declared = int(response.headers.get("content-length") or 0)
                limit = context.task.constraints.dataset_selection.max_download_mb * 1024 * 1024
                if declared and declared > limit:
                    raise ValueError("omics download exceeds configured size limit")
                written = 0
                with temp.open("wb") as handle:
                    for block in response.iter_content(1024 * 1024):
                        written += len(block)
                        if written > limit:
                            raise ValueError("omics download exceeded configured size limit")
                        handle.write(block)
            last_error = None
            break
        except requests.RequestException as exc:
            last_error = exc
            temp.unlink(missing_ok=True)
        except ValueError:
            temp.unlink(missing_ok=True)
            raise
    if last_error is not None:
        raise last_error
    temp.replace(destination)
    return destination, _sha256(destination), False


def _read_expression_table(path: Path):
    import pandas as pd

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        lines = []
        in_matrix = False
        for line in handle:
            if line.startswith("!series_matrix_table_begin"):
                in_matrix = True
                continue
            if line.startswith("!series_matrix_table_end"):
                break
            if line.startswith("!") and not in_matrix:
                continue
            if in_matrix or not line.startswith("!"):
                lines.append(line)
    if not lines:
        raise ValueError("no expression matrix rows found")
    sample = "".join(lines[:5])
    delimiter = "\t" if sample.count("\t") >= sample.count(",") else ","
    from io import StringIO
    frame = pd.read_csv(StringIO("".join(lines)), sep=delimiter, low_memory=False)
    if frame.shape[1] < 3:
        raise ValueError("expression matrix has fewer than two sample columns")
    return frame


def _gene_labels(frame):
    import pandas as pd

    if not isinstance(frame.index, pd.RangeIndex):
        return pd.Series(frame.index.astype(str), index=frame.index), "__index__"
    gene_column = next(
        (column for column in frame.columns if re.search(r"symbol|gene[_ ]?name", str(column), re.I)),
        next((column for column in frame.columns if re.search(r"gene|id", str(column), re.I)), frame.columns[0]),
    )
    return frame[gene_column].astype(str), str(gene_column)


def _prepare_counts(frame, recipe: AnalysisRecipe, minimum: int):
    import numpy as np
    import pandas as pd

    aliases: dict[str, list[str]] = recipe.parameters.get("sample_aliases", {})
    biological_units: dict[str, str] = recipe.parameters.get("biological_units", {})
    mapping = recipe.group_mapping
    resolved: dict[str, str] = {}
    expression_columns = [
        column for column in frame.columns
        if not re.search(r"tpm|fpkm|rpkm|normalized|norm", str(column), re.I)
    ]
    raw_named = [column for column in expression_columns if re.search(r"raw|count", str(column), re.I)]
    if raw_named:
        expression_columns = raw_named
    for column in expression_columns:
        normalized = _normal(str(column))
        for sample_id, values in aliases.items():
            if any(_normal(str(value)) and (_normal(str(value)) == normalized or _normal(str(value)) in normalized) for value in values):
                resolved[str(column)] = mapping[sample_id]
                break
        if str(column) in mapping:
            resolved[str(column)] = mapping[str(column)]
    if Counter(resolved.values())["case"] < minimum or Counter(resolved.values())["control"] < minimum:
        for column in expression_columns:
            if str(column) in resolved:
                continue
            numbers = set(re.findall(r"(?<!\d)\d+(?!\d)", str(column)))
            matched_groups = {
                mapping[sample_id]
                for sample_id, values in aliases.items()
                if any(numbers & set(re.findall(r"(?<!\d)\d+(?!\d)", str(value))) for value in values[1:])
            }
            if len(matched_groups) == 1:
                resolved[str(column)] = next(iter(matched_groups))
    if Counter(resolved.values())["case"] < minimum or Counter(resolved.values())["control"] < minimum:
        for column in expression_columns:
            text = str(column).casefold().replace("_", " ")
            if any(token in text for token in GEOMetadataAuditTool.CONTROL):
                resolved[str(column)] = "control"
            elif any(token in text for token in GEOMetadataAuditTool.CASE):
                resolved[str(column)] = "case"
    groups = Counter(resolved.values())
    if groups["case"] < minimum or groups["control"] < minimum:
        raise ValueError("processed matrix columns cannot be mapped to eligible case/control groups")
    labels, gene_column = _gene_labels(frame)
    numeric = frame[list(resolved)].apply(pd.to_numeric, errors="coerce")
    keep = numeric.notna().mean(axis=1) >= 0.95
    numeric = numeric.loc[keep].fillna(0)
    numeric.index = labels.loc[keep].astype(str).str.replace(r"\.\d+$", "", regex=True)
    numeric = numeric[~numeric.index.str.startswith("!")]
    numeric = numeric.groupby(level=0).sum()
    if (numeric.to_numpy() < 0).any():
        raise ValueError("count matrix contains negative values")
    if not np.allclose(numeric.to_numpy(), np.rint(numeric.to_numpy()), atol=1e-8):
        raise TypeError("PyDESeq2 requires non-negative integer counts; normalized expression was detected")
    counts = numeric.round().astype(int).T
    sample_units: dict[str, str] = {}
    for column in counts.index:
        matched_sample = next(
            (
                sample_id for sample_id, values in aliases.items()
                if any(
                    _normal(str(value))
                    and (_normal(str(value)) == _normal(str(column)) or _normal(str(value)) in _normal(str(column)))
                    for value in values
                )
            ),
            str(column),
        )
        sample_units[str(column)] = biological_units.get(matched_sample, matched_sample)
    condition_by_unit: dict[str, str] = {}
    for column, group in resolved.items():
        unit = sample_units[column]
        if unit in condition_by_unit and condition_by_unit[unit] != group:
            raise ValueError("biological unit maps to conflicting conditions")
        condition_by_unit[unit] = group
    counts["__biological_unit"] = [sample_units[str(index)] for index in counts.index]
    counts = counts.groupby("__biological_unit", sort=True).sum()
    metadata = pd.DataFrame({"condition": [condition_by_unit[str(index)] for index in counts.index]}, index=counts.index)
    groups = Counter(metadata["condition"])
    if groups["case"] < minimum or groups["control"] < minimum:
        raise ValueError("fewer than required independent biological units after technical-replicate aggregation")
    metadata["condition"] = pd.Categorical(metadata["condition"], categories=["control", "case"])
    return counts, metadata, gene_column


def _prepare_continuous_expression(frame, recipe: AnalysisRecipe, minimum: int):
    import numpy as np
    import pandas as pd

    aliases: dict[str, list[str]] = recipe.parameters.get("sample_aliases", {})
    biological_units: dict[str, str] = recipe.parameters.get("biological_units", {})
    mapping = recipe.group_mapping
    resolved: dict[str, str] = {}
    matched_samples: dict[str, str] = {}
    for column in frame.columns:
        normalized = _normal(str(column))
        for sample_id, values in aliases.items():
            if any(_normal(str(value)) and (_normal(str(value)) == normalized or _normal(str(value)) in normalized) for value in values):
                resolved[str(column)] = mapping[sample_id]
                matched_samples[str(column)] = sample_id
                break
        if str(column) in mapping:
            resolved[str(column)] = mapping[str(column)]
            matched_samples[str(column)] = str(column)
    groups = Counter(resolved.values())
    if groups["case"] < minimum or groups["control"] < minimum:
        raise ValueError("continuous matrix columns cannot be mapped to eligible case/control groups")
    labels, gene_column = _gene_labels(frame)
    numeric = frame[list(resolved)].apply(pd.to_numeric, errors="coerce")
    keep = numeric.notna().mean(axis=1) >= 0.95
    numeric = numeric.loc[keep]
    numeric.index = labels.loc[keep].astype(str).str.replace(r"\.\d+$", "", regex=True)
    numeric = numeric[~numeric.index.str.startswith("!")]
    numeric = numeric.groupby(level=0).mean().T
    unit_by_column = {
        column: biological_units.get(matched_samples.get(column, column), matched_samples.get(column, column))
        for column in numeric.index
    }
    condition_by_unit: dict[str, str] = {}
    for column, group in resolved.items():
        unit = unit_by_column[column]
        if unit in condition_by_unit and condition_by_unit[unit] != group:
            raise ValueError("biological unit maps to conflicting conditions")
        condition_by_unit[unit] = group
    numeric["__biological_unit"] = [unit_by_column[str(index)] for index in numeric.index]
    expression = numeric.groupby("__biological_unit", sort=True).mean()
    metadata = pd.DataFrame({"condition": [condition_by_unit[str(index)] for index in expression.index]}, index=expression.index)
    independent = Counter(metadata["condition"])
    if independent["case"] < minimum or independent["control"] < minimum:
        raise ValueError("fewer than required independent biological units after technical-replicate aggregation")
    values = expression.to_numpy(dtype=float)
    has_negative = bool((values < 0).any()) if values.size else False
    max_value = float(values.max()) if values.size else 0.0
    all_integer = bool(np.allclose(values, np.rint(values), atol=1e-6)) if values.size else True
    if not has_negative and all_integer and max_value > 1000:
        raise ValueError(
            "continuous expression matrix appears to contain raw or unlogged integer counts; "
            "fixed limma requires log-scale normalized expression"
        )
    normalization_unverified = not has_negative
    return expression, metadata, gene_column, normalization_unverified


def _run_pydeseq2(counts, metadata, recipe: AnalysisRecipe, n_cpus: int):
    from pydeseq2.dds import DeseqDataSet
    from pydeseq2.default_inference import DefaultInference
    from pydeseq2.ds import DeseqStats

    keep = counts.columns[counts.sum(axis=0) >= int(recipe.qc_thresholds.get("min_total_gene_count", 10))]
    counts = counts[keep]
    inference = DefaultInference(n_cpus=max(1, min(n_cpus, 8)))
    dds = DeseqDataSet(counts=counts, metadata=metadata, design=recipe.design, refit_cooks=True, inference=inference)
    dds.deseq2()
    stats = DeseqStats(dds, contrast=recipe.contrast, alpha=0.05, cooks_filter=True, independent_filter=True, inference=inference)
    stats.summary()
    return stats.results_df, dds


def _write_bulk_qc(counts, run_dir: Path, accession: str) -> tuple[dict[str, Any], list[ArtifactRef]]:
    import numpy as np
    import pandas as pd
    from sklearn.decomposition import PCA

    run_dir.mkdir(parents=True, exist_ok=True)
    library_sizes = counts.sum(axis=1).astype(float)
    log_cpm = np.log1p(counts.div(library_sizes.replace(0, np.nan), axis=0).fillna(0) * 1_000_000)
    n_components = min(2, log_cpm.shape[0], log_cpm.shape[1])
    coordinates = PCA(n_components=n_components, random_state=0).fit_transform(log_cpm)
    pca = pd.DataFrame(
        coordinates,
        index=counts.index,
        columns=[f"PC{index + 1}" for index in range(n_components)],
    )
    correlation = log_cpm.T.corr(method="pearson")
    q1, q3 = library_sizes.quantile([0.25, 0.75])
    iqr = q3 - q1
    lower, upper = max(0.0, q1 - 1.5 * iqr), q3 + 1.5 * iqr
    outliers = [str(index) for index, value in library_sizes.items() if value < lower or value > upper]

    pca_path = run_dir / f"{accession}_pca.csv"
    correlation_path = run_dir / f"{accession}_sample_correlation.csv"
    qc_path = run_dir / f"{accession}_sample_qc.json"
    pca.to_csv(pca_path)
    correlation.to_csv(correlation_path)
    qc_payload = {
        "library_sizes": {str(key): int(value) for key, value in library_sizes.items()},
        "library_size_iqr_bounds": [float(lower), float(upper)],
        "flagged_outliers": outliers,
        "excluded_samples": [],
        "exclusion_policy": "flag-only; automatic exclusion is prohibited without a reviewed reason",
    }
    qc_path.write_text(json.dumps(qc_payload, indent=2), encoding="utf-8")
    artifacts = [
        ArtifactRef(name=path.name, uri=path.name, sha256=_sha256(path), media_type=media)
        for path, media in (
            (pca_path, "text/csv"),
            (correlation_path, "text/csv"),
            (qc_path, "application/json"),
        )
    ]
    return qc_payload, artifacts


def _write_expression_qc(expression, run_dir: Path, accession: str) -> tuple[dict[str, Any], list[ArtifactRef]]:
    import pandas as pd
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    run_dir.mkdir(parents=True, exist_ok=True)
    standardized = StandardScaler().fit_transform(expression.fillna(expression.median()).T).T
    n_components = min(2, standardized.shape[0], standardized.shape[1])
    coordinates = PCA(n_components=n_components, random_state=0).fit_transform(standardized)
    pca = pd.DataFrame(coordinates, index=expression.index, columns=[f"PC{index + 1}" for index in range(n_components)])
    correlation = expression.T.corr(method="pearson")
    distributions = {
        str(sample): {
            "min": float(values.min()), "q25": float(values.quantile(0.25)),
            "median": float(values.median()), "q75": float(values.quantile(0.75)),
            "max": float(values.max()),
        }
        for sample, values in expression.iterrows()
    }
    pca_path = run_dir / f"{accession}_pca.csv"
    correlation_path = run_dir / f"{accession}_sample_correlation.csv"
    qc_path = run_dir / f"{accession}_sample_qc.json"
    pca.to_csv(pca_path)
    correlation.to_csv(correlation_path)
    qc_payload = {"sample_expression_distributions": distributions, "flagged_outliers": [], "excluded_samples": []}
    qc_path.write_text(json.dumps(qc_payload, indent=2), encoding="utf-8")
    artifacts = [
        ArtifactRef(name=path.name, uri=path.name, sha256=_sha256(path), media_type=media)
        for path, media in (
            (pca_path, "text/csv"),
            (correlation_path, "text/csv"),
            (qc_path, "application/json"),
        )
    ]
    return qc_payload, artifacts


class BulkExpressionAnalysisTool(ScientificTool):
    name = "bulk_expression_analysis"
    version = "2.1.1"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="omics",
        description="Execute pinned PyDESeq2 or fixed limma recipes after deterministic matrix validation.",
        input_types=["AnalysisRecipe[]"], output_types=["OmicsResult[]", "EvidenceItem[]", "candidate_genes"],
        execution_policy="typed_wrapper", skills=[_skill("bulk-rnaseq"), _skill("pydeseq2")],
    )

    def run(self, context: ToolContext) -> ToolExecution:
        started = time.perf_counter()
        run_id = new_id("tool")
        builder = _latest(context, "omics_recipe_builder")
        recipes = [AnalysisRecipe.model_validate(item) for item in ((builder.outputs.get("analysis_recipes") if builder else []) or [])]
        summaries: list[dict[str, Any]] = []
        evidence: list[EvidenceItem] = []
        artifacts: list[ArtifactRef] = []
        candidates: list[str] = []
        warnings: list[str] = []
        for recipe in recipes:
            if recipe.backend == "limma":
                if not context.settings.enable_limma or not shutil.which("Rscript"):
                    warnings.append(f"{recipe.accession}:limma_backend_unavailable")
                    continue
                try:
                    import pandas as pd

                    path, checksum, cached = _download_file(context, recipe.accession, recipe.input_uri)
                    frame = _read_expression_table(path)
                    expression, metadata, gene_column, normalization_unverified = _prepare_continuous_expression(
                        frame, recipe, context.task.constraints.dataset_selection.min_biological_replicates_per_group
                    )
                    qc_summary, qc_artifacts = _write_expression_qc(expression, context.run_dir, recipe.accession)
                    artifacts.extend(qc_artifacts)
                    matrix_path = context.run_dir / f"{recipe.accession}_limma_input.csv"
                    metadata_path = context.run_dir / f"{recipe.accession}_limma_metadata.csv"
                    result_path = context.run_dir / f"{recipe.accession}_limma_results.csv"
                    expression.T.to_csv(matrix_path)
                    metadata.to_csv(metadata_path)
                    script = Path(__file__).resolve().parents[3] / "scripts" / "limma_expression.R"
                    completed = subprocess.run(
                        [shutil.which("Rscript"), str(script), str(matrix_path), str(metadata_path), str(result_path)],
                        capture_output=True, text=True, timeout=1800, check=False,
                    )
                    if completed.returncode != 0:
                        raise ValueError("fixed limma script failed")
                    results = pd.read_csv(result_path).set_index("feature_id").rename(columns={
                        "logFC": "log2FoldChange", "adj.P.Val": "padj", "t": "stat",
                    })
                    results.to_csv(result_path)
                    artifact = ArtifactRef(
                        name=result_path.name, uri=result_path.name, sha256=_sha256(result_path), media_type="text/csv"
                    )
                    artifacts.append(artifact)
                    valid = results.dropna(subset=["padj", "log2FoldChange"]).copy()
                    valid["abs_lfc"] = valid["log2FoldChange"].abs()
                    valid = valid.sort_values(["padj", "abs_lfc"], ascending=[True, False])
                    for gene, row in valid.loc[valid["padj"] <= 0.05].head(context.task.constraints.max_initial_candidates).iterrows():
                        symbol = str(gene).upper()
                        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{1,30}", symbol) or re.fullmatch(r"ENS[A-Z]*G\d+", symbol):
                            continue
                        candidates.append(symbol)
                        padj, lfc = float(row["padj"]), float(row["log2FoldChange"])
                        strength = min(1.0, abs(lfc) / 2.0) * min(1.0, -math.log10(max(padj, 1e-300)) / 5.0)
                        evidence.append(EvidenceItem(
                            tool_run_id=run_id, gene_symbol=symbol, claim_class=ClaimClass.OBSERVED,
                            statement=f"{symbol} was differentially expressed in {recipe.accession} by fixed limma (case vs control; log2FC={lfc:.3g}, FDR={padj:.3g}).",
                            source=SourceLocator(
                                uri=f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={recipe.accession}",
                                source_id=recipe.accession, version=checksum, section="fixed limma case-control result",
                                chunk_id=f"{recipe.accession}-limma-{_safe_token(symbol)}",
                            ),
                            source_span=f"accession={recipe.accession}|gene={symbol}|log2FC={lfc:.8g}|FDR={padj:.8g}|sha256={checksum}",
                            context=EvidenceContext(
                                organism=context.task.context.organism, tissue=context.task.context.tissue,
                                disease=context.task.context.disease, assay="bulk continuous expression limma",
                            ),
                            stance=Stance.SUPPORTS, effect_direction="increase" if lfc > 0 else "decrease",
                            effect={"log2fc": lfc, "fdr": padj, "omics_strength": round(strength, 6), "accession": recipe.accession},
                            uncertainty=(
                                "Differential expression is observational and platform annotation may be incomplete."
                                + (" Normalization/log status could not be verified from the matrix itself."
                                   if normalization_unverified else "")
                            ),
                            quality_flags=["observational_not_causal", "continuous_expression"]
                            + (["normalization_unverified"] if normalization_unverified else []),
                            context_match_score=float(recipe.parameters.get("dataset_context_match_score", 0.85)),
                        ))
                    summaries.append({
                        "accession": recipe.accession, "backend": "limma",
                        "input_sha256": checksum, "cached_input": cached,
                        "n_samples": int(expression.shape[0]), "n_tested_genes": int(results.shape[0]),
                        "n_fdr_significant_genes": int((valid["padj"] <= 0.05).sum()),
                        "group_counts": {str(k): int(v) for k, v in metadata["condition"].value_counts().items()},
                        "design": recipe.design, "contrast": recipe.contrast, "qc_summary": qc_summary,
                        "qc_artifacts": [item.model_dump(mode="json") for item in qc_artifacts],
                        "gene_column": str(gene_column), "result_artifact": artifact.model_dump(mode="json"),
                        "normalization_unverified": normalization_unverified,
                        "software_versions": {"limma": "R package runtime-checked"},
                    })
                except (requests.RequestException, ValueError, TypeError, OSError, ImportError, subprocess.SubprocessError) as exc:
                    warnings.append(f"{recipe.accession}:{exc.__class__.__name__}:{str(exc)[:160]}")
                continue
            try:
                path, checksum, cached = _download_file(context, recipe.accession, recipe.input_uri)
                frame = _read_expression_table(path)
                counts, metadata, gene_column = _prepare_counts(
                    frame, recipe, context.task.constraints.dataset_selection.min_biological_replicates_per_group
                )
                qc_summary, qc_artifacts = _write_bulk_qc(counts, context.run_dir, recipe.accession)
                artifacts.extend(qc_artifacts)
                context.run_dir.mkdir(parents=True, exist_ok=True)
                result_path = context.run_dir / f"{recipe.accession}_deseq2_results.csv"
                cached_result_path, analysis_cache_key, analysis_cache_mode = _analysis_cache_locate(
                    context, recipe, checksum, self.version)
                cached_analysis = analysis_cache_mode != "miss"
                if cached_analysis:
                    import pandas as pd

                    results = pd.read_csv(cached_result_path, index_col=0)
                else:
                    results, _ = _run_pydeseq2(counts, metadata, recipe, os.cpu_count() or 1)
                    cached_result_path.parent.mkdir(parents=True, exist_ok=True)
                    results.to_csv(cached_result_path)
                results.to_csv(result_path)
                artifact = ArtifactRef(
                    name=result_path.name, uri=result_path.name, sha256=_sha256(result_path), media_type="text/csv"
                )
                artifacts.append(artifact)
                valid = results.dropna(subset=["padj", "log2FoldChange"]).copy()
                valid["abs_lfc"] = valid["log2FoldChange"].abs()
                valid = valid.sort_values(["padj", "abs_lfc"], ascending=[True, False])
                top = valid.loc[valid["padj"] <= 0.05].head(context.task.constraints.max_initial_candidates)
                identifier_mapping_required = False
                for gene, row in top.iterrows():
                    gene_symbol = str(gene).upper()
                    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{1,30}", gene_symbol):
                        continue
                    if re.fullmatch(r"ENS[A-Z]*G\d+(?:\.\d+)?", gene_symbol):
                        identifier_mapping_required = True
                        continue
                    candidates.append(gene_symbol)
                    padj = float(row["padj"])
                    lfc = float(row["log2FoldChange"])
                    strength = min(1.0, abs(lfc) / 2.0) * min(1.0, -math.log10(max(padj, 1e-300)) / 5.0)
                    span = f"accession={recipe.accession}|gene={gene_symbol}|log2FC={lfc:.8g}|FDR={padj:.8g}|sha256={checksum}"
                    evidence.append(EvidenceItem(
                        tool_run_id=run_id, gene_symbol=gene_symbol, claim_class=ClaimClass.OBSERVED,
                        statement=f"{gene_symbol} was differentially expressed in {recipe.accession} (case vs control; log2FC={lfc:.3g}, FDR={padj:.3g}).",
                        source=SourceLocator(
                            uri=f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={recipe.accession}",
                            source_id=recipe.accession, version=checksum, section="PyDESeq2 case-control result",
                            chunk_id=f"{recipe.accession}-de-{_safe_token(gene_symbol)}",
                        ),
                        source_span=span,
                        context=EvidenceContext(
                            organism=context.task.context.organism, tissue=context.task.context.tissue,
                            disease=context.task.context.disease, assay="bulk RNA-seq PyDESeq2",
                        ),
                        stance=Stance.SUPPORTS, effect_direction="increase" if lfc > 0 else "decrease",
                        effect={"log2fc": lfc, "fdr": padj, "omics_strength": round(strength, 6), "accession": recipe.accession},
                        uncertainty="Differential expression is observational and cohort-specific, not causal evidence.",
                        quality_flags=["observational_not_causal"],
                        context_match_score=float(recipe.parameters.get("dataset_context_match_score", 0.85)),
                        context_match={
                            "matched_disease": [context.task.context.disease] if context.task.context.disease else [],
                            "matched_tissue": [context.task.context.tissue] if context.task.context.tissue else [],
                            "matched_cell": [context.task.context.cell_type] if context.task.context.cell_type else [],
                            "matched_assay": ["bulk RNA-seq PyDESeq2"],
                            "source_fields": ["GEO series matrix", "PyDESeq2 case-control result"],
                            "context_score_origin": "tool_estimate",
                            "tool_estimate": float(recipe.parameters.get("dataset_context_match_score", 0.85)),
                        },
                    ))
                if identifier_mapping_required:
                    warnings.append(f"{recipe.accession}:gene_identifier_mapping_required")
                summaries.append({
                    "accession": recipe.accession, "backend": "pydeseq2",
                    "input_sha256": checksum, "cached_input": cached,
                    "analysis_cache_key": analysis_cache_key, "analysis_cache_mode": analysis_cache_mode,
                    "cached_analysis": cached_analysis,
                    "n_samples": int(counts.shape[0]), "n_tested_genes": int(results.shape[0]),
                    "n_fdr_significant_genes": int((valid["padj"] <= 0.05).sum()),
                    "group_counts": {str(k): int(v) for k, v in metadata["condition"].value_counts().items()},
                    "design": recipe.design, "contrast": recipe.contrast,
                    "qc_summary": qc_summary,
                    "qc_artifacts": [item.model_dump(mode="json") for item in qc_artifacts],
                    "gene_column": str(gene_column), "result_artifact": artifact.model_dump(mode="json"),
                    "software_versions": {"pydeseq2": _package("pydeseq2"), "pandas": _package("pandas")},
                })
            except (requests.RequestException, ValueError, TypeError, OSError, ImportError) as exc:
                warnings.append(f"{recipe.accession}:{exc.__class__.__name__}:{str(exc)[:160]}")
        candidates = list(dict.fromkeys(candidates))[: context.task.constraints.max_initial_candidates]
        success = bool(summaries)
        result = ToolResult(
            tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
            status=ToolStatus.SUCCESS if success else ToolStatus.PARTIAL,
            coverage_status=CoverageStatus.COVERED if success else CoverageStatus.NOT_COVERED,
            context_match_score=max(
                (float(recipe.parameters.get("dataset_context_match_score", 0.85)) for recipe in recipes),
                default=0.0,
            ) if success else 0.0,
            inputs={"recipe_ids": [recipe.recipe_id for recipe in recipes]},
            outputs={"omics_results": summaries, "formal_score_eligible": success,
                     "context_match": {
                         "matched_disease": [context.task.context.disease] if context.task.context.disease else [],
                         "matched_tissue": [context.task.context.tissue] if context.task.context.tissue else [],
                         "matched_cell": [context.task.context.cell_type] if context.task.context.cell_type else [],
                         "matched_assay": ["bulk RNA-seq"],
                         "source_fields": ["GEO series matrix"],
                     },
                     "context_score_origin": "tool_estimate"},
            candidate_genes=candidates, capability=_capability("Processed GEO bulk matrices with explicit case-control metadata"),
            data_version="per-source-sha256", code_version="2.1.0",
            parameters={
                "fdr": 0.05, "max_candidates": context.task.constraints.max_initial_candidates,
                "designs": {recipe.accession: recipe.design for recipe in recipes},
                "contrasts": {recipe.accession: recipe.contrast for recipe in recipes},
                "random_seed": context.settings.random_seed,
            },
            artifacts=artifacts, evidence_ids=[item.evidence_id for item in evidence], warnings=warnings,
            limitations=["Automatic bulk analysis covers processed count matrices; raw FASTQ/SRA remains out of scope."],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return ToolExecution(result=result, evidence=evidence)


class CellxgeneDiscoveryTool(ScientificTool):
    name = "cellxgene_discovery"
    version = "2.1.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="dataset_discovery",
        description="Size-first, versioned CELLxGENE Census disease metadata discovery.",
        input_types=["TaskSpec"], output_types=["DatasetCandidate[]"],
        execution_policy="read_only_connector", skills=[_skill("cellxgene-census")],
    )

    def run(self, context: ToolContext) -> ToolExecution:
        if "cellxgene" not in context.task.constraints.dataset_selection.omics_modes:
            return ToolExecution(result=ToolResult(
                tool_name=self.name, tool_version=self.version, status=ToolStatus.OUT_OF_SCOPE,
                coverage_status=CoverageStatus.NOT_COVERED, context_match_score=0,
                outputs={"dataset_candidates": []}, capability=_capability("CELLxGENE Census 2025-11-08", cells=True),
                warnings=["cellxgene_mode_disabled"],
            ), evidence=[])
        try:
            import cellxgene_census
        except ImportError:
            return ToolExecution(result=ToolResult(
                tool_name=self.name, tool_version=self.version, status=ToolStatus.PARTIAL,
                coverage_status=CoverageStatus.NOT_COVERED, context_match_score=0,
                outputs={"dataset_candidates": []}, capability=_capability("CELLxGENE Census 2025-11-08", cells=True),
                warnings=["cellxgene_census_dependency_missing"],
                limitations=["Install the omics-single-cell extra to enable Census discovery."],
            ), evidence=[])
        disease = (context.task.context.disease or "").replace("'", "\\'")
        tissue = (context.task.context.tissue or "").replace("'", "\\'")
        filters = [f"disease == '{disease}'", "is_primary_data == True"]
        if tissue:
            filters.insert(1, f"tissue_general == '{tissue}'")
        value_filter = " and ".join(filters)
        try:
            with cellxgene_census.open_soma(census_version="2025-11-08") as census:
                metadata = cellxgene_census.get_obs(
                    census, "homo_sapiens", value_filter=value_filter,
                    column_names=["dataset_id", "donor_id", "cell_type", "tissue_general", "disease", "assay", "is_primary_data"],
                )
            rows = []
            for dataset_id, group in metadata.groupby("dataset_id"):
                cells = int(len(group))
                donors = int(group["donor_id"].nunique())
                candidate = DatasetCandidate(
                    accession=str(dataset_id), source="CELLxGENE", title=f"CELLxGENE dataset {dataset_id}",
                    organism="Homo sapiens", disease=context.task.context.disease,
                    tissue=str(group["tissue_general"].mode().iloc[0]) if not group.empty else context.task.context.tissue,
                    assay=str(group["assay"].mode().iloc[0]) if not group.empty else "single-cell RNA-seq",
                    sample_count=donors, metadata_confidence=1.0,
                    context_match_score=0.9,
                    eligibility="eligible" if cells <= context.task.constraints.dataset_selection.max_cells else "needs_confirmation",
                    exclusion_reasons=[] if cells <= context.task.constraints.dataset_selection.max_cells else ["cell_count_exceeds_automatic_limit"],
                    source_uri=f"https://cellxgene.cziscience.com/collections/{dataset_id}", source_version="Census:2025-11-08",
                )
                rows.append({**candidate.model_dump(mode="json"), "cell_count": cells, "donor_count": donors})
            rows.sort(key=lambda row: (-row["context_match_score"], row["cell_count"]))
            rows = rows[: context.task.constraints.dataset_selection.max_geo_candidates]
            status = ToolStatus.SUCCESS if rows else ToolStatus.PARTIAL
            coverage = CoverageStatus.COVERED if rows else CoverageStatus.NOT_COVERED
            warnings = [] if rows else ["no_exact_census_disease_tissue_match"]
        except Exception as exc:
            rows, status, coverage = [], ToolStatus.PARTIAL, CoverageStatus.NOT_COVERED
            warnings = [f"census_query_failed:{exc.__class__.__name__}"]
        result = ToolResult(
            tool_name=self.name, tool_version=self.version, status=status, coverage_status=coverage,
            context_match_score=max((row["context_match_score"] for row in rows), default=0.0),
            inputs={"value_filter": value_filter},
            outputs={"dataset_candidates": rows, "size_checked_before_expression": True,
                     "context_match": {
                         "matched_disease": [context.task.context.disease] if context.task.context.disease else [],
                         "matched_tissue": [context.task.context.tissue] if context.task.context.tissue else [],
                         "matched_cell": [context.task.context.cell_type] if context.task.context.cell_type else [],
                         "matched_assay": ["CELLxGENE Census query"],
                         "source_fields": ["Census:2025-11-08"],
                     },
                     "context_score_origin": "tool_estimate"},
            capability=_capability("CELLxGENE Census 2025-11-08 primary data", cells=True),
            data_version="Census:2025-11-08", code_version="2.1.0",
            parameters={"is_primary_data": True, "max_cells": context.task.constraints.dataset_selection.max_cells},
            warnings=warnings,
            limitations=["Disease fields may contain multiple ontology labels; exact-match misses are returned as coverage gaps."],
        )
        return ToolExecution(result=result, evidence=[])


class SingleCellAnalysisTool(ScientificTool):
    name = "single_cell_analysis"
    version = "2.1.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="omics",
        description="Validate standard H5AD/10x inputs and require donor-level pseudobulk metadata for formal DE.",
        input_types=["OmicsInput[]", "DatasetCandidate[]"], output_types=["OmicsResult[]", "candidate_genes"],
        execution_policy="typed_wrapper", skills=[_skill("scanpy", "Pinned to 1.11.5 for Python 3.11"), _skill("anndata"), _skill("cellxgene-census")],
    )

    CONTROL_LABELS = ("control", "healthy", "normal", "unaffected", "no disease")
    CASE_LABELS = ("case", "disease", "tumor", "cancer", "carcinoma", "alzheimer", "parkinson", "colitis")

    @staticmethod
    def _path_size(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    def _load_local(self, item, max_bytes: int):
        import pandas as pd
        import scanpy as sc

        path = Path(item.uri).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError("single-cell input does not exist")
        if self._path_size(path) > max_bytes:
            raise ValueError("single-cell input exceeds configured download limit")
        if item.data_kind == "h5ad":
            data = sc.read_h5ad(path)
        elif item.data_kind == "10x_mtx":
            if not path.is_dir():
                raise ValueError("10x_mtx input must be a directory")
            data = sc.read_10x_mtx(path, var_names="gene_symbols", make_unique=True, cache=False)
        elif item.data_kind == "10x_h5":
            data = sc.read_10x_h5(path)
            data.var_names_make_unique()
        else:
            raise ValueError("unsupported single-cell input kind")

        if item.data_kind != "h5ad":
            if not item.metadata_uri:
                raise ValueError("10x inputs require a barcode metadata table")
            metadata_path = Path(item.metadata_uri).expanduser().resolve()
            if not metadata_path.is_file():
                raise FileNotFoundError("single-cell metadata table does not exist")
            metadata = pd.read_csv(metadata_path)
            barcode_column = next(
                (column for column in metadata.columns if str(column).casefold() in {"barcode", "cell_id", "cell"}),
                metadata.columns[0],
            )
            metadata = metadata.set_index(barcode_column)
            shared = data.obs_names.intersection(metadata.index.astype(str))
            if len(shared) != data.n_obs:
                raise ValueError("10x barcode metadata does not cover every cell")
            data = data[shared].copy()
            metadata.index = metadata.index.astype(str)
            for key in (item.cell_type_key, item.donor_key, item.condition_key):
                if key not in metadata:
                    raise ValueError(f"single-cell metadata is missing {key}")
                data.obs[key] = metadata.loc[data.obs_names, key].astype(str).to_numpy()

        required = [item.cell_type_key, item.donor_key, item.condition_key]
        missing = [key for key in required if key not in data.obs]
        if missing:
            raise ValueError("single-cell metadata is missing " + ",".join(missing))
        matrix = data.layers[item.counts_layer] if item.counts_layer in data.layers else data.X
        return data, matrix, {
            "source": str(path), "source_sha256": _sha256(path) if path.is_file() else None,
            "cell_type_key": item.cell_type_key, "donor_key": item.donor_key,
            "condition_key": item.condition_key, "counts_source": item.counts_layer if item.counts_layer in data.layers else "X",
        }

    def _load_census(self, context: ToolContext, candidate: dict[str, Any]):
        import cellxgene_census

        dataset_id = str(candidate["accession"]).replace("'", "")
        value_filter = f"dataset_id == '{dataset_id}' and is_primary_data == True"
        columns = ["dataset_id", "donor_id", "cell_type", "disease", "is_primary_data"]
        with cellxgene_census.open_soma(census_version="2025-11-08") as census:
            metadata = cellxgene_census.get_obs(
                census, "homo_sapiens", value_filter=value_filter, column_names=columns,
            )
            if len(metadata) > context.task.constraints.dataset_selection.max_cells:
                raise ValueError("Census dataset exceeds configured cell limit")
            data = cellxgene_census.get_anndata(
                census, organism="Homo sapiens", measurement_name="RNA",
                obs_value_filter=value_filter, obs_column_names=columns,
            )
        return data, data.X, {
            "source": f"CELLxGENE:{dataset_id}", "source_sha256": None,
            "cell_type_key": "cell_type", "donor_key": "donor_id",
            "condition_key": "disease", "counts_source": "Census RNA raw X",
            "census_version": "2025-11-08", "value_filter": value_filter,
        }

    def _condition_mapping(self, values, disease: str) -> dict[str, str]:
        disease_tokens = [token for token in re.split(r"\W+", disease.casefold()) if len(token) >= 4]
        mapping: dict[str, str] = {}
        for raw in sorted({str(value) for value in values}):
            label = raw.casefold().replace("_", " ")
            if any(token in label for token in self.CONTROL_LABELS):
                mapping[raw] = "control"
            elif any(token in label for token in self.CASE_LABELS) or any(token in label for token in disease_tokens):
                mapping[raw] = "case"
        return mapping

    def _analyze(self, context: ToolContext, data, matrix, info: dict[str, Any], run_id: str):
        import numpy as np
        import pandas as pd
        from scipy import sparse

        if data.n_obs > context.task.constraints.dataset_selection.max_cells:
            raise ValueError("single-cell input exceeds configured cell limit")
        if matrix.shape != data.shape:
            raise ValueError("raw count matrix shape does not match AnnData")
        probe = matrix.data if sparse.issparse(matrix) else np.asarray(matrix)
        if np.size(probe) and ((probe < 0).any() or not np.allclose(probe, np.rint(probe), atol=1e-8)):
            raise TypeError("single-cell formal pseudobulk requires non-negative integer raw counts")

        cell_key, donor_key, condition_key = info["cell_type_key"], info["donor_key"], info["condition_key"]
        obs = data.obs[[cell_key, donor_key, condition_key]].astype(str).copy()
        condition_mapping = self._condition_mapping(obs[condition_key], context.task.context.disease or "")
        obs["__condition"] = obs[condition_key].map(condition_mapping)
        if obs["__condition"].isna().any():
            raise ValueError("single-cell condition labels are ambiguous; explicit case/control labels are required")
        donor_conditions = obs.groupby(donor_key)["__condition"].nunique()
        if (donor_conditions > 1).any():
            raise ValueError("a donor is assigned to multiple conditions")

        minimum = context.task.constraints.dataset_selection.min_biological_replicates_per_group
        requested_cell = (context.task.context.cell_type or "").casefold()
        eligible: list[tuple[str, int]] = []
        for cell_type, group in obs.groupby(cell_key):
            if requested_cell and str(cell_type).casefold() != requested_cell:
                continue
            donors = group.groupby("__condition")[donor_key].nunique().to_dict()
            if donors.get("case", 0) >= minimum and donors.get("control", 0) >= minimum:
                eligible.append((str(cell_type), int(len(group))))
        eligible.sort(key=lambda row: (-row[1], row[0]))
        eligible = eligible[:5]
        if not eligible:
            raise ValueError(f"no cell type has at least {minimum} independent donors per group")

        context.run_dir.mkdir(parents=True, exist_ok=True)
        artifacts: list[ArtifactRef] = []
        evidence: list[EvidenceItem] = []
        candidate_genes: list[str] = []
        summaries: list[dict[str, Any]] = []
        for cell_type, cell_count in eligible:
            indices = np.flatnonzero(obs[cell_key].to_numpy() == cell_type)
            subset = obs.iloc[indices]
            groups = subset[[donor_key, "__condition"]].drop_duplicates().sort_values(["__condition", donor_key])
            rows = []
            row_names = []
            conditions = []
            for donor, condition in groups.itertuples(index=False, name=None):
                donor_indices = indices[
                    (subset[donor_key].to_numpy() == donor) & (subset["__condition"].to_numpy() == condition)
                ]
                aggregated = matrix[donor_indices].sum(axis=0)
                rows.append(sparse.csr_matrix(aggregated) if sparse.issparse(matrix) else np.asarray(aggregated).reshape(1, -1))
                row_names.append(f"{donor}|{condition}")
                conditions.append(condition)
            stacked = sparse.vstack(rows).toarray() if sparse.issparse(matrix) else np.vstack(rows)
            counts = pd.DataFrame(np.rint(stacked).astype(int), index=row_names, columns=data.var_names.astype(str))
            counts = counts.T.groupby(level=0).sum().T
            metadata = pd.DataFrame({"condition": conditions}, index=row_names)
            metadata["condition"] = pd.Categorical(metadata["condition"], categories=["control", "case"])
            recipe = AnalysisRecipe(
                accession=_safe_token(info["source"]), data_kind="single_cell_h5ad",
                backend="scanpy_pseudobulk", input_uri=info["source"], design="~condition",
                contrast=["condition", "case", "control"],
                qc_thresholds={"min_total_gene_count": 10},
            )
            differential, _ = _run_pydeseq2(counts, metadata, recipe, os.cpu_count() or 1)
            token = _safe_token(cell_type)
            result_path = context.run_dir / f"single_cell_{token}_pseudobulk_deseq2.csv"
            differential.to_csv(result_path)
            artifact = ArtifactRef(name=result_path.name, uri=result_path.name, sha256=_sha256(result_path), media_type="text/csv")
            artifacts.append(artifact)
            valid = differential.dropna(subset=["padj", "log2FoldChange"]).copy()
            valid = valid.loc[valid["padj"] <= 0.05]
            valid["abs_lfc"] = valid["log2FoldChange"].abs()
            valid = valid.sort_values(["padj", "abs_lfc"], ascending=[True, False])
            for gene, row in valid.head(context.task.constraints.max_initial_candidates).iterrows():
                symbol = str(gene).upper()
                if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{1,30}", symbol) or re.fullmatch(r"ENS[A-Z]*G\d+", symbol):
                    continue
                candidate_genes.append(symbol)
                padj, lfc = float(row["padj"]), float(row["log2FoldChange"])
                strength = min(1.0, abs(lfc) / 2.0) * min(1.0, -math.log10(max(padj, 1e-300)) / 5.0)
                evidence.append(EvidenceItem(
                    tool_run_id=run_id, gene_symbol=symbol, claim_class=ClaimClass.OBSERVED,
                    statement=f"{symbol} was differentially expressed in donor-level {cell_type} pseudobulk (case vs control; log2FC={lfc:.3g}, FDR={padj:.3g}).",
                    source=SourceLocator(
                        uri=info["source"], source_id=_safe_token(info["source"]),
                        version=info.get("source_sha256") or info.get("census_version"),
                        section=f"donor-level pseudobulk:{cell_type}", chunk_id=f"sc-{token}-{_safe_token(symbol)}",
                    ),
                    source_span=f"cell_type={cell_type}|gene={symbol}|log2FC={lfc:.8g}|FDR={padj:.8g}",
                    context=EvidenceContext(
                        organism=context.task.context.organism, disease=context.task.context.disease,
                        tissue=context.task.context.tissue, cell_type=cell_type,
                        assay="single-cell donor-level pseudobulk",
                    ),
                    stance=Stance.SUPPORTS, effect_direction="increase" if lfc > 0 else "decrease",
                    effect={"log2fc": lfc, "fdr": padj, "omics_strength": round(strength, 6)},
                    uncertainty="Pseudobulk differential expression is donor-aware but observational and not causal.",
                    quality_flags=["pseudobulk", "observational_not_causal"], context_match_score=0.9,
                    context_match={
                        "matched_disease": [context.task.context.disease] if context.task.context.disease else [],
                        "matched_tissue": [context.task.context.tissue] if context.task.context.tissue else [],
                        "matched_cell": [cell_type] if cell_type else [],
                        "matched_assay": ["single-cell donor-level pseudobulk"],
                        "source_fields": ["Census:2025-11-08", "local source matrix"],
                        "context_score_origin": "tool_estimate",
                        "tool_estimate": 0.9,
                    },
                ))
            summaries.append({
                "source": info["source"], "cell_type": cell_type, "n_cells": cell_count,
                "donors_per_condition": {
                    str(k): int(v) for k, v in metadata.groupby("condition", observed=True).size().items()
                },
                "n_tested_genes": int(len(differential)), "n_fdr_significant_genes": int(len(valid)),
                "result_artifact": artifact.model_dump(mode="json"), "counts_source": info["counts_source"],
            })
        return summaries, artifacts, evidence, list(dict.fromkeys(candidate_genes))

    def run(self, context: ToolContext) -> ToolExecution:
        run_id = new_id("tool")
        warnings: list[str] = []
        summaries: list[dict[str, Any]] = []
        artifacts: list[ArtifactRef] = []
        evidence: list[EvidenceItem] = []
        candidate_genes: list[str] = []
        inputs = list(context.task.omics_inputs)
        try:
            import anndata  # noqa: F401
            import scanpy  # noqa: F401
        except ImportError:
            return ToolExecution(result=ToolResult(
                tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
                status=ToolStatus.PARTIAL, coverage_status=CoverageStatus.NOT_COVERED, context_match_score=0,
                inputs={"omics_inputs": len(inputs)}, outputs={"omics_results": [], "formal_score_eligible": False},
                capability=_capability("Standard H5AD/10x donor-level pseudobulk", cells=True),
                warnings=["single_cell_dependencies_missing"],
                limitations=["Install the pinned omics-single-cell extra to enable this typed wrapper."],
            ), evidence=[])

        workloads: list[tuple[Any, Any, dict[str, Any]]] = []
        max_bytes = context.task.constraints.dataset_selection.max_download_mb * 1024 * 1024
        for item in inputs:
            try:
                workloads.append(self._load_local(item, max_bytes))
            except (OSError, ValueError, TypeError) as exc:
                warnings.append(f"{_safe_token(item.uri)}:{exc.__class__.__name__}:{str(exc)[:160]}")
        if not inputs and context.settings.enable_census_expression:
            census = _latest(context, "cellxgene_discovery")
            census_candidates = (census.outputs.get("dataset_candidates") if census else []) or []
            eligible = [row for row in census_candidates if row.get("eligibility") == "eligible"]
            if eligible:
                try:
                    workloads.append(self._load_census(context, eligible[0]))
                except Exception as exc:
                    warnings.append(f"census_expression:{exc.__class__.__name__}:{str(exc)[:160]}")
        for data, matrix, info in workloads:
            try:
                rows, row_artifacts, row_evidence, genes = self._analyze(context, data, matrix, info, run_id)
                summaries.extend(rows)
                artifacts.extend(row_artifacts)
                evidence.extend(row_evidence)
                candidate_genes.extend(genes)
            except (OSError, ValueError, TypeError, ImportError) as exc:
                warnings.append(f"{_safe_token(info['source'])}:{exc.__class__.__name__}:{str(exc)[:160]}")
        candidate_genes = list(dict.fromkeys(candidate_genes))[: context.task.constraints.max_initial_candidates]
        covered = bool(summaries)
        if not workloads and not warnings:
            warnings.append("no_selected_single_cell_input")
        result = ToolResult(
            tool_run_id=run_id, tool_name=self.name, tool_version=self.version,
            status=ToolStatus.SUCCESS if covered else ToolStatus.PARTIAL,
            coverage_status=CoverageStatus.COVERED if covered else CoverageStatus.NOT_COVERED,
            context_match_score=0.9 if covered else 0.0,
            inputs={"omics_inputs": len(inputs), "census_expression_enabled": context.settings.enable_census_expression},
            outputs={"omics_results": summaries, "formal_score_eligible": covered, "analysis_stage": "donor_level_pseudobulk",
                     "context_match": {
                         "matched_disease": [context.task.context.disease] if context.task.context.disease else [],
                         "matched_tissue": [context.task.context.tissue] if context.task.context.tissue else [],
                         "matched_cell": [context.task.context.cell_type] if context.task.context.cell_type else [],
                         "matched_assay": ["single-cell donor-level pseudobulk"],
                         "source_fields": ["Census:2025-11-08", "local source matrix"],
                     },
                     "context_score_origin": "tool_estimate"},
            candidate_genes=candidate_genes,
            capability=_capability("Standard H5AD/10x and optional Census donor-level pseudobulk", cells=True),
            data_version="Census:2025-11-08 or local-source-sha256", code_version="2.1.0",
            parameters={"max_cells": context.task.constraints.dataset_selection.max_cells, "min_donors_per_group": context.task.constraints.dataset_selection.min_biological_replicates_per_group},
            artifacts=artifacts, evidence_ids=[item.evidence_id for item in evidence], warnings=warnings,
            limitations=["Per-cell marker tests remain exploratory and are not emitted as formal disease evidence."],
        )
        return ToolExecution(result=result, evidence=evidence)


class PathwayEnrichmentTool(ScientificTool):
    name = "pathway_enrichment"
    version = "2.1.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="pathway",
        description="Run preranked GSEA and ORA with the tested-gene background, fixed seed and recorded local library version/checksum.",
        input_types=["differential_result_artifact"], output_types=["pathway_result_artifact"],
        execution_policy="typed_wrapper", skills=[_skill("pathway-enrichment")],
    )

    def run(self, context: ToolContext) -> ToolExecution:
        bulk = _latest(context, "bulk_expression_analysis")
        result_rows = (bulk.outputs.get("omics_results") if bulk else []) or []
        artifacts: list[ArtifactRef] = []
        warnings: list[str] = []
        summaries = []
        try:
            import gseapy as gp
            import pandas as pd
        except ImportError:
            gp = pd = None
        gseapy_version = "unknown"
        gene_set_sha256: str | None = None
        if gp is not None:
            gseapy_version = str(getattr(gp, "__version__", "") or _package("gseapy") or "unknown")
            try:
                from gseapy import LIBRARY
                gene_set_path = Path(LIBRARY) / "MSigDB_Hallmark_2020.gmt"
                if gene_set_path.is_file():
                    gene_set_sha256 = _sha256(gene_set_path)[:12]
            except Exception:
                gene_set_sha256 = None
        library_ref = gene_set_sha256 or "local-library"
        data_version = f"MSigDB_Hallmark_2020:gseapy:{gseapy_version}:{library_ref}"
        if gp is not None:
            for row in result_rows:
                source = context.run_dir / row["result_artifact"]["uri"]
                try:
                    pathway_cache_key = hashlib.sha256(json.dumps({
                        "contract_version": CONTRACT_VERSION,
                        "tool_version": self.version,
                        "source_sha256": _sha256(source),
                        "gene_set": "MSigDB_Hallmark_2020",
                        "permutations": context.settings.gsea_permutations,
                        "seed": context.settings.random_seed,
                    }, sort_keys=True).encode("utf-8")).hexdigest()
                    cache_root = context.cache_dir / "analysis" / "pathway" / pathway_cache_key
                    cached_gsea = cache_root / "gsea.csv"
                    cached_ora = cache_root / "ora.csv"
                    cached_background = cache_root / "ora_background.txt"
                    output = context.run_dir / f"{row['accession']}_gsea_results.csv"
                    ora_path = context.run_dir / f"{row['accession']}_ora_results.csv"
                    background_path = context.run_dir / f"{row['accession']}_ora_background.txt"
                    if cached_gsea.is_file() and cached_background.is_file():
                        shutil.copy2(cached_gsea, output)
                        shutil.copy2(cached_background, background_path)
                        if cached_ora.is_file():
                            shutil.copy2(cached_ora, ora_path)
                        gsea_artifact = ArtifactRef(name=output.name, uri=output.name, sha256=_sha256(output), media_type="text/csv")
                        background_artifact = ArtifactRef(name=background_path.name, uri=background_path.name, sha256=_sha256(background_path), media_type="text/plain")
                        ora_artifact = ArtifactRef(name=ora_path.name, uri=ora_path.name, sha256=_sha256(ora_path), media_type="text/csv") if ora_path.is_file() else None
                        artifacts.extend([gsea_artifact, background_artifact])
                        if ora_artifact:
                            artifacts.append(ora_artifact)
                        summaries.append({
                            "accession": row["accession"], "result_artifact": gsea_artifact.model_dump(mode="json"),
                            "gsea_artifact": gsea_artifact.model_dump(mode="json"),
                            "ora_artifact": ora_artifact.model_dump(mode="json") if ora_artifact else None,
                            "ora_background_artifact": background_artifact.model_dump(mode="json"),
                            "tested_gene_background_count": len(background_path.read_text(encoding="utf-8").splitlines()),
                            "n_terms": int(len(pd.read_csv(output))), "cached_analysis": True,
                            "analysis_cache_key": pathway_cache_key,
                        })
                        continue
                    table = pd.read_csv(source, index_col=0)
                    rank = table["stat"].dropna().sort_values(ascending=False)
                    rank.index = rank.index.astype(str).str.upper()
                    rank = rank[~rank.index.duplicated(keep="first")]
                    if len(rank) < 15:
                        raise ValueError("fewer than 15 ranked genes")
                    enriched = gp.prerank(
                        rnk=rank, gene_sets=["MSigDB_Hallmark_2020"], min_size=15, max_size=500,
                        permutation_num=context.settings.gsea_permutations, seed=context.settings.random_seed,
                        threads=max(1, min(os.cpu_count() or 1, 8)), outdir=None,
                    ).res2d
                    enriched.to_csv(output, index=False)
                    gsea_artifact = ArtifactRef(name=output.name, uri=output.name, sha256=_sha256(output), media_type="text/csv")
                    background = sorted({str(gene).upper() for gene in table.index if str(gene).strip()})
                    significant = sorted({
                        str(gene).upper() for gene, values in table.iterrows()
                        if values.get("padj") is not None and not pd.isna(values.get("padj")) and float(values["padj"]) <= 0.05
                    })
                    background_path.write_text("\n".join(background) + "\n", encoding="utf-8")
                    background_artifact = ArtifactRef(
                        name=background_path.name, uri=background_path.name,
                        sha256=_sha256(background_path), media_type="text/plain",
                    )
                    ora_artifact = None
                    if significant:
                        try:
                            ora = gp.enrichr(
                                gene_list=significant,
                                gene_sets=["MSigDB_Hallmark_2020"],
                                background=background,
                                outdir=None,
                            ).results
                            ora.to_csv(ora_path, index=False)
                            ora_artifact = ArtifactRef(
                                name=ora_path.name, uri=ora_path.name,
                                sha256=_sha256(ora_path), media_type="text/csv",
                            )
                        except Exception as exc:
                            warnings.append(f"{row['accession']}:ora:{exc.__class__.__name__}")
                    artifacts.extend([gsea_artifact, background_artifact])
                    if ora_artifact:
                        artifacts.append(ora_artifact)
                    cache_root.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(output, cached_gsea)
                    shutil.copy2(background_path, cached_background)
                    if ora_artifact:
                        shutil.copy2(ora_path, cached_ora)
                    summaries.append({
                        "accession": row["accession"],
                        "result_artifact": gsea_artifact.model_dump(mode="json"),
                        "gsea_artifact": gsea_artifact.model_dump(mode="json"),
                        "ora_artifact": ora_artifact.model_dump(mode="json") if ora_artifact else None,
                        "ora_background_artifact": background_artifact.model_dump(mode="json"),
                        "tested_gene_background_count": len(background),
                        "significant_gene_count": len(significant),
                        "n_terms": int(len(enriched)),
                        "cached_analysis": False, "analysis_cache_key": pathway_cache_key,
                    })
                except Exception as exc:
                    warnings.append(f"{row['accession']}:{exc.__class__.__name__}")
        elif result_rows:
            warnings.append("gseapy_dependency_missing")
        result = ToolResult(
            tool_name=self.name, tool_version=self.version,
            status=ToolStatus.SUCCESS if summaries else ToolStatus.PARTIAL,
            coverage_status=CoverageStatus.COVERED if summaries else CoverageStatus.NOT_COVERED,
            context_match_score=0.85 if summaries else 0.0,
            inputs={"bulk_result_count": len(result_rows)},
            outputs={"pathway_results": summaries,
                     "context_match": {
                         "matched_disease": [context.task.context.disease] if context.task.context.disease else [],
                         "matched_tissue": [context.task.context.tissue] if context.task.context.tissue else [],
                         "matched_cell": [context.task.context.cell_type] if context.task.context.cell_type else [],
                         "matched_assay": ["preranked GSEA/ORA on MSigDB_Hallmark_2020"],
                         "source_fields": ["gseapy local library"],
                     },
                     "context_score_origin": "tool_estimate"},
            capability=_capability("Preranked GSEA plus ORA with an explicit tested-gene background"),
            data_version=data_version, code_version="2.1.0",
            parameters={"permutations": context.settings.gsea_permutations, "seed": context.settings.random_seed, "min_size": 15, "max_size": 500},
            artifacts=artifacts, warnings=warnings,
            limitations=[
                "Pathway enrichment uses gseapy's local bundled MSigDB_Hallmark_2020 gene-set library; "
                "no online retrieval was performed and results depend on the installed gseapy library version "
                "and gene-set file checksum."
            ],
        )
        return ToolExecution(result=result, evidence=[])


class OmicsCandidateExtractionTool(ScientificTool):
    name = "omics_candidate_extraction"
    version = "2.1.0"
    descriptor = ToolDescriptor(
        tool_id=name, evidence_dimension="omics",
        description="Consolidate only candidate genes emitted by validated omics tool outputs.",
        input_types=["ToolResult[]"], output_types=["candidate_genes"], execution_policy="typed_wrapper",
    )

    def run(self, context: ToolContext) -> ToolExecution:
        genes: list[str] = []
        for result in context.prior_results:
            if result.tool_name in {"bulk_expression_analysis", "single_cell_analysis"}:
                genes.extend(result.candidate_genes)
        first_seen = {gene: index for index, gene in enumerate(genes)}
        recurrence = Counter(genes)
        genes = sorted(recurrence, key=lambda gene: (-recurrence[gene], first_seen[gene]))[
            : context.task.constraints.max_initial_candidates
        ]
        return ToolExecution(result=ToolResult(
            tool_name=self.name, tool_version=self.version,
            status=ToolStatus.SUCCESS if genes else ToolStatus.PARTIAL,
            coverage_status=CoverageStatus.COVERED if genes else CoverageStatus.NOT_COVERED,
            context_match_score=0.85 if genes else 0.0,
            inputs={"upstream_tool_runs": [item.tool_run_id for item in context.prior_results]},
            outputs={
                "candidate_count": len(genes),
                "cross_dataset_recurrence": {gene: recurrence[gene] for gene in genes},
            }, candidate_genes=genes,
            capability=_capability("Validated omics candidate consolidation"), code_version="2.1.0",
            warnings=[] if genes else ["no_validated_omics_candidates"],
        ), evidence=[])
