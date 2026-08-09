"""Paper-level RAG store: bounded abstract chunks from recent CNS papers.

Stores only public bibliographic abstract text in small, provenance-bound
chunks with per-chunk digests. Retrieval is deterministic lexical scoring
(disease tokens, query tokens, data-availability lanes, recency, journal
premium); no embedding model and no network are required at retrieval time.

Full methods text is never persisted: the Europe PMC fetcher keeps methods in
memory only for pattern extraction, and the RAG store materializes abstracts
alone. Alignment-data generation and model training remain deferred (P3).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import utc_now

RAG_CONTRACT_VERSION = "0.1.0"

_LANE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "genetics": (
        "gwas", "snp", "variant", "locus", "eqtl", "colocal", "genome-wide",
        "fine-map", "heritab", "allele", "genetic association", "mendelian",
    ),
    "omics": (
        "rna-seq", "transcriptom", "proteom", "metabolom", "expression",
        "differentially expressed", "multi-omics", "bulk rna",
    ),
    "single_cell": (
        "single-cell", "single cell", "scrna", "spatial", "cell type",
        "cell types", "atlas", "pseudobulk", "cell state", "microenvironment",
    ),
    "perturbation": (
        "crispr", "perturb-seq", "knockout", "knockdown", "overexpression",
        "perturbation", "drug screen", "screen", "gain-of-function",
        "loss-of-function", "genetic perturbation",
    ),
    "drug": (
        "drug", "therapeutic", "inhibitor", "agonist", "antagonist",
        "small molecule", "compound", "pharmacolog", "drug target",
    ),
    "safety": ("safety", "toxicity", "adverse", "on-target", "off-target"),
    "trials": ("clinical trial", "phase 1", "phase 2", "phase 3", "trial"),
}

_JOURNAL_PREMIUM = {
    "nature": 1.5,
    "science": 1.5,
    "cell": 1.5,
    "nature genetics": 1.0,
    "nature medicine": 1.0,
    "nature immunology": 1.0,
    "nature neuroscience": 1.0,
    "nature cancer": 1.0,
    "nature metabolism": 1.0,
    "nature cell biology": 1.0,
}

_DISEASE_STOP_TOKENS = frozenset({
    "disease", "diseases", "disorder", "disorders", "syndrome", "syndromes",
    "type", "cell", "cells", "human", "patient", "patients", "study", "studies",
    "and", "the", "of", "in", "a", "an", "with", "for", "or", "to", "is", "are",
})
_WORD_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_TOKEN = re.compile(r"[\u4e00-\u9fff]+")


def _tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens = set(_WORD_TOKEN.findall(lowered))
    tokens.update(_CJK_TOKEN.findall(lowered))
    tokens.discard("")
    return tokens


def _signal_tokens(text: str) -> set[str]:
    return {
        token for token in _tokens(text)
        if token not in _DISEASE_STOP_TOKENS and not token.isdigit() and len(token) > 1
    }


def _normalize_journal(name: str | None) -> str:
    return re.split(r"\s*\(", str(name or "").strip().lower(), maxsplit=1)[0].strip()


def _journal_premium(journal: str) -> float:
    return float(_JOURNAL_PREMIUM.get(_normalize_journal(journal), 0.0))


def chunk_text(text: str, chunk_size: int = 700, overlap: int = 90) -> list[str]:
    """Split bounded text into overlapping sentence-aware chunks."""
    if chunk_size < 200:
        raise ValueError("chunk_size must be at least 200")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")
    normalized = " ".join((text or "").split())
    if not normalized:
        return []
    if len(normalized) <= chunk_size:
        return [normalized]
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        if end < len(normalized):
            cut = max(start, end - 180)
            boundary = max(
                normalized.rfind(". ", cut, end),
                normalized.rfind("; ", cut, end),
                normalized.rfind(": ", cut, end),
            )
            if boundary > cut:
                end = boundary + 1
            else:
                whitespace = normalized.rfind(" ", cut, end)
                if whitespace > cut:
                    end = whitespace
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(normalized):
            break
        next_start = max(end - overlap, start + 1)
        if next_start <= start:
            next_start = start + 1
        start = next_start
    return chunks


def infer_lane_tags(text: str, context_tags: Iterable[str] = ()) -> list[str]:
    """Infer evidence lanes mentioned in a chunk; context tags add explicit lanes."""
    lowered = (text or "").lower()
    tags: set[str] = set()
    for lane, keywords in _LANE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            tags.add(lane)
    for raw in context_tags or ():
        tag = str(raw).strip().lower().replace(" ", "_")
        if tag in _LANE_KEYWORDS:
            tags.add(tag)
    return sorted(tags)


class PaperChunk(BaseModel):
    """One immutable, provenance-bound abstract chunk."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(pattern=r"^chunk-\d+-[a-z]+-\d+$")
    pmid: str = Field(pattern=r"^\d+$")
    title: str = Field(min_length=1)
    journal: str = Field(min_length=1)
    year: int = Field(ge=1900, le=2100)
    doi: str | None = None
    pmcid: str | None = None
    source_material: Literal["abstract", "methods"] = "abstract"
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    lane_tags: list[str] = Field(default_factory=list)
    disease_tags: list[str] = Field(default_factory=list)
    fetched_at: str = Field(default_factory=utc_now)
    digest: str = ""

    @model_validator(mode="after")
    def _finalize(self) -> "PaperChunk":
        computed = self.compute_digest()
        if not self.digest:
            self.digest = computed
        elif self.digest != computed:
            raise ValueError(f"paper chunk digest mismatch for {self.chunk_id}")
        return self

    def compute_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"digest", "fetched_at"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class PaperChunkHit(BaseModel):
    chunk: PaperChunk
    score: float
    matched_reason: list[str] = Field(default_factory=list)


def _score_chunk(
    chunk: PaperChunk,
    query_tokens: set[str],
    disease_tokens: set[str],
    lanes_available: set[str] | None,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    text_hay = _tokens(" ".join([chunk.title, chunk.text]))
    disease_hits = (
        (disease_tokens & text_hay) | (disease_tokens & set(chunk.disease_tags))
        if disease_tokens else set()
    )
    if disease_hits:
        score += 2.0 * len(disease_hits)
        reasons.append(f"disease tokens matched: {sorted(disease_hits)}")
    query_hits = query_tokens & text_hay if query_tokens else set()
    if query_hits:
        score += 1.0 * len(query_hits)
        reasons.append(f"query tokens matched: {sorted(query_hits)}")
    if not disease_hits and not query_hits:
        return -1.0, ["no disease or query match"]
    tags = set(chunk.lane_tags)
    if lanes_available is not None and tags:
        if tags <= lanes_available:
            score += 2.0
            reasons.append("all inferred evidence lanes are available")
        else:
            missing = sorted(tags - lanes_available)
            score -= 1.0 * len(missing)
            reasons.append(f"chunk lanes unavailable: {missing}")
    if chunk.year >= 2023:
        score += 1.5
        reasons.append("recent paper (>=2023)")
    elif chunk.year >= 2021:
        score += 1.0
        reasons.append("recent paper (>=2021)")
    premium = _journal_premium(chunk.journal)
    if premium:
        score += premium
        reasons.append(f"high-impact journal: {chunk.journal}")
    return score, reasons


def build_chunks(
    meta: Any,
    context_tags: Iterable[str] = (),
    *,
    chunk_size: int = 700,
    overlap: int = 90,
) -> list[PaperChunk]:
    """Chunk a PaperMeta (abstract) record into immutable PaperChunk records."""
    abstract = getattr(meta, "abstract", None) or (
        getattr(meta, "source_text", None)
        if getattr(meta, "source_material", "abstract") == "abstract" else None
    )
    if not abstract:
        return []
    disease_tags = sorted({
        tag for tag in (context_tags or ())
        if str(tag).strip() and str(tag).strip().lower() not in _DISEASE_STOP_TOKENS
    })
    parts = chunk_text(str(abstract), chunk_size=chunk_size, overlap=overlap)
    chunks: list[PaperChunk] = []
    for index, text in enumerate(parts):
        chunks.append(PaperChunk(
            chunk_id=f"chunk-{meta.pmid}-abstract-{index:03d}",
            pmid=meta.pmid,
            title=meta.title,
            journal=meta.journal,
            year=meta.year,
            doi=getattr(meta, "doi", None),
            pmcid=getattr(meta, "pmcid", None),
            source_material="abstract",
            chunk_index=index,
            text=text,
            lane_tags=infer_lane_tags(text, disease_tags),
            disease_tags=disease_tags,
        ))
    return chunks


class PaperMetaFetcher(Protocol):
    def fetch(self, pmid: str) -> Any | None: ...


def fetch_chunks(
    pmid: str,
    fetcher: PaperMetaFetcher,
    context_tags: Iterable[str] = (),
) -> list[PaperChunk]:
    meta = fetcher.fetch(pmid)
    if meta is None:
        return []
    return build_chunks(meta, context_tags)


class PaperRagStore:
    """Append-only JSONL chunk store with per-chunk digest validation."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().resolve()
        # Process-local chunk cache plus the file stat it was parsed from.
        # mtime/size changes invalidate it, so repeated searches/adds do not
        # re-read the whole corpus unless another writer changed the file.
        self._rows: list[PaperChunk] | None = None
        self._row_stat: tuple[int, int] | None = None
        self._row_ids: dict[str, PaperChunk] | None = None

    def _load(self) -> list[PaperChunk]:
        if not self.path.is_file():
            return []
        rows: list[PaperChunk] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    rows.append(PaperChunk.model_validate_json(line))
                except Exception as exc:
                    raise ValueError(f"invalid paper chunk at line {line_number}: {exc}") from exc
        return rows

    def _current_stat(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _cached_rows(self) -> list[PaperChunk]:
        stat = self._current_stat()
        if stat is None:
            self._rows = None
            self._row_stat = None
            self._row_ids = None
            return []
        if self._rows is not None and self._row_stat == stat:
            return self._rows
        rows = self._load()
        self._rows = rows
        self._row_stat = stat
        self._row_ids = {row.chunk_id: row for row in rows}
        return rows

    def _note_appended(self) -> None:
        self._row_stat = self._current_stat()

    def all(self) -> list[PaperChunk]:
        return self._cached_rows()

    def get(self, chunk_id: str) -> PaperChunk | None:
        self._cached_rows()
        return self._row_ids.get(chunk_id) if self._row_ids is not None else None

    def add(self, chunk: PaperChunk) -> bool:
        if chunk.digest != chunk.compute_digest():
            raise ValueError("paper chunk digest is not self-consistent")
        rows = self._cached_rows()
        known = self._row_ids
        if known is None:
            known = {row.chunk_id: row for row in rows}
            self._row_ids = known
        if chunk.chunk_id in known:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(chunk.model_dump_json() + "\n")
        rows.append(chunk)
        known[chunk.chunk_id] = chunk
        self._note_appended()
        return True

    def add_many(self, chunks: Iterable[PaperChunk]) -> dict[str, int]:
        rows = self._cached_rows()
        known = self._row_ids
        if known is None:
            known = {row.chunk_id: row for row in rows}
            self._row_ids = known
        added = 0
        skipped = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            for chunk in sorted(chunks, key=lambda row: row.chunk_id):
                if chunk.digest != chunk.compute_digest():
                    raise ValueError(f"paper chunk digest is not self-consistent: {chunk.chunk_id}")
                if chunk.chunk_id in known:
                    skipped += 1
                    continue
                handle.write(chunk.model_dump_json() + "\n")
                rows.append(chunk)
                known[chunk.chunk_id] = chunk
                added += 1
        self._note_appended()
        return {"added": added, "skipped": skipped, "total": len(rows)}

    def search(
        self,
        *,
        query: str = "",
        disease: str = "",
        lanes_available: Iterable[str] | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[PaperChunkHit]:
        query_tokens = _signal_tokens(query)
        disease_tokens = _signal_tokens(disease)
        available = {str(lane).lower() for lane in lanes_available} if lanes_available is not None else None
        scored: list[PaperChunkHit] = []
        for chunk in self._cached_rows():
            score, reasons = _score_chunk(chunk, query_tokens, disease_tokens, available)
            if score >= min_score:
                scored.append(PaperChunkHit(chunk=chunk, score=score, matched_reason=reasons))
        scored.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))
        return scored[: max(0, top_k)]

    def corpus_card(self) -> dict[str, Any]:
        rows = self._cached_rows()
        journals: dict[str, int] = {}
        years: dict[str, int] = {}
        lanes: dict[str, int] = {}
        for row in rows:
            journals[row.journal] = journals.get(row.journal, 0) + 1
            years[str(row.year)] = years.get(str(row.year), 0) + 1
            for lane in row.lane_tags:
                lanes[lane] = lanes.get(lane, 0) + 1
        return {
            "path": str(self.path),
            "contract_version": RAG_CONTRACT_VERSION,
            "chunks": len(rows),
            "papers": len({row.pmid for row in rows}),
            "journals": dict(sorted(journals.items(), key=lambda pair: (-pair[1], pair[0]))),
            "years": dict(sorted(years.items())),
            "lanes": dict(sorted(lanes.items(), key=lambda pair: (-pair[1], pair[0]))),
        }

    def write_manifest(self) -> dict[str, Any]:
        rows = self._cached_rows()
        manifest = {
            "contract_version": RAG_CONTRACT_VERSION,
            "chunks": len(rows),
            "papers": len({row.pmid for row in rows}),
            "records": [
                {
                    "chunk_id": row.chunk_id,
                    "pmid": row.pmid,
                    "sha256": hashlib.sha256(row.model_dump_json().encode("utf-8")).hexdigest(),
                }
                for row in rows
            ],
        }
        manifest_path = self.path.with_name("MANIFEST.json")
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        return manifest


def paper_rag_store_from_path(path: Path | str | None) -> PaperRagStore | None:
    if not path:
        return None
    store = PaperRagStore(path)
    if not store.path.is_file():
        return None
    return store


__all__ = [
    "RAG_CONTRACT_VERSION", "PaperChunk", "PaperChunkHit", "PaperMetaFetcher",
    "PaperRagStore", "build_chunks", "chunk_text", "fetch_chunks",
    "infer_lane_tags", "paper_rag_store_from_path",
]
