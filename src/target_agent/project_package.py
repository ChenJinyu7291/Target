"""Portable research-project packages: export, import, checksum verification.

A package is a zip archive containing MANIFEST.json, README.txt and the
whole durable project directory (spec, plans, attempts, events, artifacts,
report, ...). The manifest records the SHA-256 of every shipped file, so an
imported project can be verified before it is committed to the store.

Rules:
- files whose names look like secrets abort the export;
- an import never overwrites an existing project directory;
- every manifest-listed file is hash-verified before extraction is committed;
- archive members not listed in the manifest are rejected;
- project_spec.json must parse as ResearchProjectSpec before acceptance.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .research_contracts import RESEARCH_CONTRACT_VERSION, ResearchProjectSpec
from .research_store import ResearchProjectStore

PACKAGE_FORMAT = "target-project-package"
PACKAGE_SCHEMA_VERSION = "1.0.0"
_SECRET_RE = re.compile(
    r"(^|/)(\.env|\.env\..*|id_rsa|id_ed25519|credentials?|secrets?)(\.|$)|"
    r"\.(key|pem|p12|pfx|gpg|asc)$",
    re.IGNORECASE,
)


class PackageFile(BaseModel):
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)


class ProjectPackageManifest(BaseModel):
    format: Literal["target-project-package"]
    schema_version: str
    exported_at: str
    project_id: str = Field(min_length=1)
    research_contract_version: str = Field(min_length=1)
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    files: list[PackageFile] = Field(default_factory=list)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _sha256_zip_member(archive: zipfile.ZipFile, member: str) -> str:
    digest = hashlib.sha256()
    with archive.open(member, "r") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def _iter_project_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_dir).as_posix()
        if _SECRET_RE.search(rel):
            raise ValueError(f"refusing to export secret-like file: {rel}")
        files.append(path)
    return files


def _package_readme(project_id: str) -> str:
    return (
        f"Target research project package: {project_id}\n"
        "\n"
        "This archive is a fully reproducible project snapshot. To use it:\n"
        f"  1. target-agent project-import --input {project_id}.target-project.zip\n"
        f"  2. target-agent project-status --project-id {project_id}\n"
        f"  3. target-agent project-run --input project.yaml --resume   (or via Web)\n"
        "  4. target-agent serve --port 8888  to open the workbench\n"
        "\n"
        "The MANIFEST.json contains SHA-256 checksums for every file; import verifies them.\n"
    )


def _build_manifest(project_dir: Path, project_id: str) -> ProjectPackageManifest:
    files = _iter_project_files(project_dir)
    entries: list[PackageFile] = []
    total = 0
    for path in files:
        rel = path.relative_to(project_dir).as_posix()
        if rel in {"MANIFEST.json", "README.txt", "import_record.json"}:
            continue
        digest, size = _sha256_file(path)
        entries.append(PackageFile(path=rel, sha256=digest, size=size))
        total += size
    return ProjectPackageManifest(
        format=PACKAGE_FORMAT,
        schema_version=PACKAGE_SCHEMA_VERSION,
        exported_at=datetime.now(timezone.utc).isoformat(),
        project_id=project_id,
        research_contract_version=RESEARCH_CONTRACT_VERSION,
        file_count=len(entries),
        total_bytes=total,
        files=entries,
    )


def _write_zip(project_dir: Path, manifest: ProjectPackageManifest, target: Path | io.BytesIO) -> dict:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", _package_readme(manifest.project_id))
        archive.writestr("MANIFEST.json", manifest.model_dump_json(indent=2))
        for path in sorted(project_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(project_dir).as_posix()
            if rel in {"MANIFEST.json", "README.txt", "import_record.json"}:
                continue
            archive.write(path, rel)
    return {
        "project_id": manifest.project_id,
        "schema_version": manifest.schema_version,
        "file_count": manifest.file_count,
        "total_bytes": manifest.total_bytes,
    }


def export_project(
    projects_dir: Path | str, project_id: str, output: Path | None = None
) -> dict:
    """Export a durable project to a zip file (or a generated default path)."""
    store = ResearchProjectStore(projects_dir, project_id)
    if not (store.project_dir / "project_spec.json").is_file():
        from .research_service import ResearchProjectNotFound

        raise ResearchProjectNotFound(project_id)
    manifest = _build_manifest(store.project_dir, project_id)
    target = output or (Path.cwd() / f"{project_id}.target-project.zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    summary = _write_zip(store.project_dir, manifest, target)
    summary["package_path"] = str(target.resolve())
    return summary


def export_project_bytes(projects_dir: Path | str, project_id: str) -> tuple[bytes, dict]:
    """Export a durable project into memory (used by the Web API)."""
    store = ResearchProjectStore(projects_dir, project_id)
    if not (store.project_dir / "project_spec.json").is_file():
        from .research_service import ResearchProjectNotFound

        raise ResearchProjectNotFound(project_id)
    manifest = _build_manifest(store.project_dir, project_id)
    buffer = io.BytesIO()
    summary = _write_zip(store.project_dir, manifest, buffer)
    return buffer.getvalue(), summary


def _safe_member(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("..") or "/../" in normalized:
        raise ValueError(f"unsafe archive member: {path!r}")
    return normalized


def import_project(projects_dir: Path | str, archive: Path) -> dict:
    """Verify and import a portable project package into a projects store."""
    archive = Path(archive).expanduser().resolve()
    projects_root = Path(projects_dir).expanduser().resolve()
    projects_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        names = [_safe_member(name) for name in zf.namelist()]
        if "MANIFEST.json" not in names:
            raise ValueError("package is missing MANIFEST.json")
        if "project_spec.json" not in names:
            raise ValueError("package is missing project_spec.json")
        manifest = ProjectPackageManifest.model_validate_json(
            zf.read("MANIFEST.json").decode("utf-8")
        )
        if manifest.format != PACKAGE_FORMAT:
            raise ValueError(f"unsupported package format: {manifest.format}")
        project_id = manifest.project_id
        ResearchProjectStore._safe_component(project_id, "project_id")
        final_dir = (projects_root / project_id).resolve()
        if not final_dir.is_relative_to(projects_root):
            raise ValueError("project id escapes projects root")
        if final_dir.exists():
            raise ValueError(f"project {project_id} already exists; refusing to overwrite")
        by_path = {entry.path: entry for entry in manifest.files}
        if "project_spec.json" not in by_path:
            raise ValueError("manifest does not list project_spec.json")
        member_set = set(names)
        manifest_paths = {_safe_member(entry.path) for entry in manifest.files}
        unlisted = [
            member for member in names
            if member not in manifest_paths and member not in {"MANIFEST.json", "README.txt"}
        ]
        if unlisted:
            raise ValueError(
                f"package contains member not listed in manifest: {unlisted[0]}"
            )
        for member in names:
            if _SECRET_RE.search(member):
                raise ValueError(f"package contains secret-like file: {member}")
        for entry in manifest.files:
            member = _safe_member(entry.path)
            if member not in member_set:
                raise ValueError(f"manifest file missing from archive: {entry.path}")
            digest = _sha256_zip_member(zf, member)
            if digest != entry.sha256:
                raise ValueError(f"checksum mismatch for {entry.path}")
        temp_dir = Path(tempfile.mkdtemp(prefix=f".import-{project_id}-", dir=projects_root))
        try:
            zf.extractall(temp_dir)
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        spec_path = temp_dir / "project_spec.json"
        try:
            ResearchProjectSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
        except Exception as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise ValueError(f"project_spec.json is invalid: {exc}") from exc
        for path in temp_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(temp_dir).as_posix()
            if _SECRET_RE.search(rel):
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise ValueError(f"package contains secret-like file: {rel}")
        import_record = {
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "source_archive_sha256": _sha256_file(archive)[0],
            "package_schema_version": manifest.schema_version,
        }
        (temp_dir / "import_record.json").write_text(
            json.dumps(import_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temp_dir.rename(final_dir)
    return {
        "imported": True,
        "project_id": project_id,
        "file_count": manifest.file_count,
        "total_bytes": manifest.total_bytes,
    }


def inspect_package(archive: Path) -> dict:
    """Read-only review: parse metadata and verify every manifest checksum.

    No files are extracted and no project store is touched, so a reviewer can
    validate a share package without importing it.
    """
    with zipfile.ZipFile(archive, "r") as zf:
        names = set(zf.namelist())
        if "MANIFEST.json" not in names:
            raise ValueError("package is missing MANIFEST.json")
        manifest = ProjectPackageManifest.model_validate_json(
            zf.read("MANIFEST.json").decode("utf-8")
        )
        for entry in manifest.files:
            member = _safe_member(entry.path)
            if member not in names:
                raise ValueError(f"manifest file missing from archive: {entry.path}")
            digest = hashlib.sha256(zf.read(member)).hexdigest()
            if digest != entry.sha256:
                raise ValueError(f"checksum mismatch for {entry.path}")
    return {
        "project_id": manifest.project_id,
        "schema_version": manifest.schema_version,
        "exported_at": manifest.exported_at,
        "file_count": manifest.file_count,
        "total_bytes": manifest.total_bytes,
        "research_contract_version": manifest.research_contract_version,
        "checksums_valid": True,
    }
