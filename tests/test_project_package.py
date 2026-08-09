from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from target_agent.project_package import (
    export_project,
    export_project_bytes,
    import_project,
    inspect_package,
)
from target_agent.research_store import ResearchProjectStore

from .test_research_runtime import fake_research_runtime, research_project


def _run_fake_project(tmp_path, project_id: str = "project-pkg"):
    runtime, _ = fake_research_runtime(tmp_path)
    project = research_project(project_id)
    runtime.run(project)
    return runtime, project


def _read_report(store: ResearchProjectStore) -> str:
    report = next(row for row in store.read_artifacts() if row.logical_name == "research_report")
    return store.artifact_path(report).read_text(encoding="utf-8")


def test_export_import_roundtrip_preserves_project_state(tmp_path):
    runtime, project = _run_fake_project(tmp_path)
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    original_report = _read_report(store)
    original_events = len(store.read_events())

    package = tmp_path / "package.zip"
    summary = export_project(runtime.projects_dir, project.project_id, output=package)
    assert package.is_file()
    assert summary["project_id"] == project.project_id
    assert summary["file_count"] > 0

    metadata = inspect_package(package)
    assert metadata["project_id"] == project.project_id
    assert metadata["schema_version"] == "1.0.0"
    assert metadata["checksums_valid"] is True

    with zipfile.ZipFile(package) as zf:
        names = zf.namelist()
    assert "MANIFEST.json" in names
    assert "project_spec.json" in names

    second_root = tmp_path / "projects-imported"
    imported = import_project(second_root, package)
    assert imported == {
        "imported": True,
        "project_id": project.project_id,
        "file_count": summary["file_count"],
        "total_bytes": summary["total_bytes"],
    }
    imported_store = ResearchProjectStore(second_root, project.project_id)
    assert _read_report(imported_store) == original_report
    assert len(imported_store.read_events()) == original_events
    assert imported_store.project_dir.joinpath("import_record.json").is_file()


def test_export_bytes_matches_file_export(tmp_path):
    runtime, project = _run_fake_project(tmp_path)
    data, summary = export_project_bytes(runtime.projects_dir, project.project_id)
    assert summary["file_count"] > 0
    package = tmp_path / "from-bytes.zip"
    package.write_bytes(data)
    with zipfile.ZipFile(package) as zf:
        assert "MANIFEST.json" in zf.namelist()
    imported = import_project(tmp_path / "projects-bytes", package)
    assert imported["project_id"] == project.project_id


def test_import_rejects_tampered_checksum(tmp_path):
    runtime, project = _run_fake_project(tmp_path, "project-tamper")
    package = tmp_path / "tamper.zip"
    export_project(runtime.projects_dir, project.project_id, output=package)

    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(package) as zf:
        for name in zf.namelist():
            entries[name] = zf.read(name)
    entries["project_spec.json"] += b"\n# tampered\n"
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)

    with pytest.raises(ValueError, match="checksum mismatch"):
        import_project(tmp_path / "projects-tamper", tampered)
    assert not (tmp_path / "projects-tamper" / project.project_id).exists()


def test_import_refuses_overwrite(tmp_path):
    runtime, project = _run_fake_project(tmp_path, "project-overwrite")
    package = tmp_path / "overwrite.zip"
    export_project(runtime.projects_dir, project.project_id, output=package)
    with pytest.raises(ValueError, match="already exists"):
        import_project(runtime.projects_dir, package)


def test_export_refuses_secret_like_files(tmp_path):
    runtime, project = _run_fake_project(tmp_path, "project-secret")
    store = ResearchProjectStore(runtime.projects_dir, project.project_id)
    store.project_dir.joinpath(".env").write_text("STEP_API_KEY=top-secret\n", encoding="utf-8")
    with pytest.raises(ValueError, match="secret-like"):
        export_project(runtime.projects_dir, project.project_id, output=tmp_path / "secret.zip")


def test_web_export_endpoint_returns_zip(tmp_path):
    from target_agent.webapp import create_app

    from .test_research_web_api import _wait_for_project
    from .test_runtime import fake_runtime as fake_target_runtime

    research_runtime, _ = fake_research_runtime(tmp_path)
    app = create_app(fake_target_runtime(tmp_path), research_runtime=research_runtime)
    client = app.test_client()
    project = research_project("project-web-export")
    created = client.post("/api/projects", json=project.model_dump(mode="json"))
    assert created.status_code == 202
    _wait_for_project(client, project.project_id)

    response = client.get(f"/api/projects/{project.project_id}/export")
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert response.headers["X-Project-File-Count"].isdigit()
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        assert "MANIFEST.json" in zf.namelist()
        assert "project_spec.json" in zf.namelist()

def test_inspect_package_rejects_tampered_archive(tmp_path):
    runtime, project = _run_fake_project(tmp_path, "project-pkg-tamper")
    package = tmp_path / "package.zip"
    export_project(runtime.projects_dir, project.project_id, output=package)

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(package) as source:
        with zipfile.ZipFile(tampered, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "project_spec.json":
                    data = data.replace(b'"title"', b'"titlex"')
                target.writestr(info, data)

    with pytest.raises(ValueError, match="checksum mismatch"):
        inspect_package(tampered)

def test_import_rejects_member_not_listed_in_manifest(tmp_path):
    runtime, project = _run_fake_project(tmp_path, "project-unlisted")
    package = tmp_path / "package.zip"
    export_project(runtime.projects_dir, project.project_id, output=package)

    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(package) as zf:
        for name in zf.namelist():
            entries[name] = zf.read(name)
    entries["extra/unlisted.txt"] = b"not covered by the manifest\n"
    tampered = tmp_path / "unlisted.zip"
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)

    with pytest.raises(ValueError, match="not listed in manifest"):
        import_project(tmp_path / "projects-unlisted", tampered)
    assert not (tmp_path / "projects-unlisted" / project.project_id).exists()
