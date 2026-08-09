"""Central configuration with dotenv loading and secret-safe diagnostics."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
        env_file_encoding="utf-8",
    )

    step_api_key: SecretStr | None = Field(default=None, alias="STEP_API_KEY", repr=False)
    step_base_url: str = Field(default="https://api.stepfun.com/v1", alias="STEP_BASE_URL")
    step_model: str = Field(default="step-3.7-flash", alias="STEP_MODEL")
    step_connect_timeout_seconds: int = Field(default=10, alias="STEP_CONNECT_TIMEOUT_SECONDS", ge=1, le=120)
    step_read_timeout_seconds: int = Field(default=90, alias="STEP_READ_TIMEOUT_SECONDS", ge=1, le=600)
    step_max_retries: int = Field(default=3, alias="STEP_MAX_RETRIES", ge=0, le=5)
    llm_provider: str = Field(default="step", alias="LLM_PROVIDER", pattern="^(step|openai)$")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY", repr=False)
    openai_model: str | None = Field(default=None, alias="OPENAI_MODEL")

    ncbi_api_key: SecretStr | None = Field(default=None, alias="NCBI_API_KEY", repr=False)
    ncbi_email: str | None = Field(default=None, alias="NCBI_EMAIL")
    runs_dir: Path = Field(default=PROJECT_ROOT / "runs", alias="TARGET_AGENT_RUN_DIR")
    projects_dir: Path = Field(default=PROJECT_ROOT / "projects", alias="RESEARCH_AGENT_PROJECT_DIR")
    cache_dir: Path = Field(default=PROJECT_ROOT / "cache", alias="TARGET_AGENT_CACHE_DIR")
    input_root: Path = Field(default=PROJECT_ROOT / "data" / "input", alias="TARGET_AGENT_INPUT_ROOT")
    tool_registry_path: Path = Field(
        default=PROJECT_ROOT / "configs" / "tool_registry.yaml",
        alias="TARGET_AGENT_TOOL_REGISTRY",
    )
    cache_only: bool = Field(default=False, alias="TARGET_AGENT_CACHE_ONLY")
    enable_limma: bool = Field(default=False, alias="TARGET_AGENT_ENABLE_LIMMA")
    enable_census_expression: bool = Field(default=False, alias="TARGET_AGENT_ENABLE_CENSUS_EXPRESSION")
    web_workers: int = Field(default=2, alias="TARGET_AGENT_WEB_WORKERS", ge=1, le=16)
    web_queue_size: int = Field(default=8, alias="TARGET_AGENT_WEB_QUEUE_SIZE", ge=1, le=100)
    web_token: SecretStr | None = Field(default=None, alias="TARGET_AGENT_WEB_TOKEN", repr=False)
    gsea_permutations: int = Field(default=1000, alias="TARGET_AGENT_GSEA_PERMUTATIONS", ge=100, le=10000)
    random_seed: int = Field(default=123, alias="TARGET_AGENT_RANDOM_SEED")
    reviewer_lora_base: Path | None = Field(default=None, alias="TARGET_AGENT_REVIEWER_LORA_BASE")
    reviewer_lora_adapter: Path | None = Field(default=None, alias="TARGET_AGENT_REVIEWER_LORA_ADAPTER")
    pattern_store_path: Path | None = Field(
        default=PROJECT_ROOT / "paper_strategy" / "patterns.jsonl",
        alias="TARGET_AGENT_PATTERN_STORE",
    )
    pattern_few_shot_top_k: int = Field(
        default=3, alias="TARGET_AGENT_PATTERN_FEW_SHOT_TOP_K", ge=0, le=8,
    )
    paper_rag_path: Path | None = Field(
        default=PROJECT_ROOT / "paper_strategy" / "rag" / "chunks.jsonl",
        alias="TARGET_AGENT_PAPER_RAG_STORE",
    )
    paper_rag_top_k: int = Field(
        default=2, alias="TARGET_AGENT_PAPER_RAG_TOP_K", ge=0, le=8,
    )
    pattern_curation_path: Path = Field(
        default=PROJECT_ROOT / "paper_strategy" / "corpus" / "curation.jsonl",
        alias="TARGET_AGENT_PATTERN_CURATION",
    )
    pattern_nomination_path: Path = Field(
        default=PROJECT_ROOT / "paper_strategy" / "nominations.jsonl",
        alias="TARGET_AGENT_PATTERN_NOMINATION",
    )
    pattern_review_ledger_path: Path = Field(
        default=PROJECT_ROOT / "paper_strategy" / "reviews.jsonl",
        alias="TARGET_AGENT_PATTERN_REVIEW_LEDGER",
    )
    pattern_extraction_audit_path: Path = Field(
        default=PROJECT_ROOT / "paper_strategy" / "extractions.jsonl",
        alias="TARGET_AGENT_PATTERN_EXTRACTION_AUDIT",
    )
    skill_catalog_path: Path = Field(
        default=PROJECT_ROOT / "skills",
        alias="TARGET_AGENT_SKILL_CATALOG",
    )
    skill_hint_top_k: int = Field(
        default=3, alias="TARGET_AGENT_SKILL_HINT_TOP_K", ge=0, le=8,
    )
    workflow_catalog_path: Path = Field(
        default=PROJECT_ROOT / "workflows",
        alias="TARGET_AGENT_WORKFLOW_CATALOG",
    )

    kernel_enabled: bool = Field(default=True, alias="TARGET_AGENT_KERNEL_ENABLED")
    kernel_idle_timeout_seconds: int = Field(
        default=600, alias="TARGET_AGENT_KERNEL_IDLE_TIMEOUT_SECONDS", ge=0, le=86400,
    )
    kernel_exec_timeout_seconds: float = Field(
        default=60.0, alias="TARGET_AGENT_KERNEL_EXEC_TIMEOUT_SECONDS", ge=1.0, le=600.0,
    )
    kernel_max_output_chars: int = Field(
        default=20000, alias="TARGET_AGENT_KERNEL_MAX_OUTPUT_CHARS", ge=1000, le=200000,
    )
    kernel_max_code_chars: int = Field(
        default=100000, alias="TARGET_AGENT_KERNEL_MAX_CODE_CHARS", ge=1000, le=1000000,
    )
    kernel_python_bin: str = Field(default="", alias="TARGET_AGENT_KERNEL_PYTHON")
    kernel_r_bin: str = Field(default="", alias="TARGET_AGENT_KERNEL_R")
    kernel_port: int = Field(default=8765, alias="TARGET_AGENT_KERNEL_PORT", ge=1024, le=65535)
    kernel_token: SecretStr | None = Field(default=None, alias="TARGET_AGENT_KERNEL_TOKEN", repr=False)

    @property
    def step_configured(self) -> bool:
        return bool(self.step_api_key and self.step_api_key.get_secret_value().strip() and self.step_model.strip())

    @property
    def llm_provider_name(self) -> str:
        return (self.llm_provider or "step").strip().lower()

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider_name == "openai":
            return bool(
                self.openai_api_key
                and self.openai_api_key.get_secret_value().strip()
                and (self.openai_base_url or "").strip()
                and (self.openai_model or "").strip()
            )
        return self.step_configured

    def with_keyring_secrets(self) -> "Settings":
        """Fill missing API keys from the optional OS keyring (lowest priority).

        Process environment and dotenv values are already materialized by
        pydantic-settings; this only fills fields that are still None. The
        keyring backend is failure-soft: unavailability returns self unchanged.
        """
        from . import secret_store

        updates: dict[str, Any] = {}
        for field_name, alias in (
            ("step_api_key", "STEP_API_KEY"),
            ("openai_api_key", "OPENAI_API_KEY"),
            ("ncbi_api_key", "NCBI_API_KEY"),
        ):
            current = getattr(self, field_name)
            if current is not None:
                continue
            stored = secret_store.get_secret(alias)
            if stored:
                updates[field_name] = SecretStr(stored)
        if not updates:
            return self
        return self.model_copy(update=updates)

    def public_summary(self) -> dict[str, Any]:
        return {
            "llm_provider": self.llm_provider_name,
            "llm_configured": self.llm_configured,
            "step_configured": self.step_configured,
            "step_model": self.step_model,
            "step_base_url": self.step_base_url,
            "cache_only": self.cache_only,
            "limma_enabled": self.enable_limma,
            "census_expression_enabled": self.enable_census_expression,
            "reviewer_lora_configured": bool(self.reviewer_lora_base and self.reviewer_lora_adapter),
            "pattern_store_configured": bool(self.pattern_store_path and self.pattern_store_path.is_file()),
            "pattern_few_shot_top_k": self.pattern_few_shot_top_k,
            "paper_rag_configured": bool(self.paper_rag_path and self.paper_rag_path.is_file()),
            "paper_rag_top_k": self.paper_rag_top_k,
            "skill_catalog_configured": bool(self.skill_catalog_path and self.skill_catalog_path.is_dir()),
            "skill_hint_top_k": self.skill_hint_top_k,
            "kernel_enabled": self.kernel_enabled,
            "kernel_backends": {
                "python": True,
                "r": bool(shutil.which(self.kernel_r_bin or "Rscript")),
            },
            "runs_dir_writable": _writable_parent(self.runs_dir),
            "projects_dir_writable": _writable_parent(self.projects_dir),
            "cache_dir_writable": _writable_parent(self.cache_dir),
            "input_root_writable": _writable_parent(self.input_root),
        }


def _writable_parent(path: Path) -> bool:
    candidate = path.expanduser()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate.exists() and os.access(candidate, os.W_OK)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load process environment first, then one explicit/default dotenv file."""
    selected = env_file if env_file is not None else PROJECT_ROOT / ".env"
    return Settings(
        _env_file=selected if selected and selected.exists() else None
    ).with_keyring_secrets()
