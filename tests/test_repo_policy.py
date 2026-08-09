from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RPC_PATH = _REPO_ROOT / "scripts" / "repo_policy_check.py"


def _load_repo_policy():
    spec = importlib.util.spec_from_file_location("repo_policy_check", _RPC_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repo_policy = _load_repo_policy()
scan_repo = repo_policy.scan_repo


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_repo_detects_drive_letter_paths(tmp_path):
    _write(tmp_path, "drive.txt", "run from G:\\deepcamp\\Target\n")
    violations = scan_repo(tmp_path)
    assert any("local absolute path" in v and v.endswith("drive.txt") for v in violations)


def test_scan_repo_detects_hostname(tmp_path):
    _write(tmp_path, "host.txt", "host is DESKTOP-739CH55\n")
    violations = scan_repo(tmp_path)
    assert any("local absolute path" in v and v.endswith("host.txt") for v in violations)


def test_scan_repo_detects_data_root_paths(tmp_path):
    _write(tmp_path, "data.txt", "artifact at /data/foo/bar/result.json\n")
    violations = scan_repo(tmp_path)
    assert any("local absolute path" in v and v.endswith("data.txt") for v in violations)


def test_scan_repo_ignores_share_portal_redaction_examples(tmp_path):
    _write(
        tmp_path,
        "tests/test_share_portal.py",
        'note = "see D:\\tmp\\y"\n',
    )
    assert scan_repo(tmp_path) == []


def test_scan_repo_detects_drive_paths_in_utf16_text(tmp_path):
    path = tmp_path / "utf16.txt"
    path.write_bytes(("run from G:\\deepcamp\\Target\n").encode("utf-16"))
    violations = scan_repo(tmp_path)
    assert any("local absolute path" in v and v.endswith("utf16.txt") for v in violations)


def test_scan_repo_reads_utf16_without_false_negatives(tmp_path):
    path = tmp_path / "clean_utf16.txt"
    path.write_bytes(("all clear\n").encode("utf-16"))
    assert scan_repo(tmp_path) == []


def test_scan_repo_ignores_its_own_example_literals(tmp_path):
    _write(
        tmp_path,
        "tests/test_repo_policy.py",
        "G:\\deepcamp\\Target\nD:\\tmp\n",
    )
    assert scan_repo(tmp_path) == []
