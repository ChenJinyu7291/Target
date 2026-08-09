from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".pytest_cache", "__pycache__", "runs", "cache", "artifacts", "models"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".js", ".css", ".html", ".example", ".sh", ".xml", ".mjs", ".jsonl", ".txt", ".rst", ".ini", ".cfg"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(?:api[_-]?key|token|secret)\s*[:=]\s*[\"'][A-Za-z0-9_./+-]{24,}[\"']"),
    re.compile(r"(?im)^(?:STEP_API_KEY|NCBI_API_KEY)\s*=\s*[A-Za-z0-9_./+-]{24,}\s*$"),
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC) PRIVATE KEY-----"),
]
LOCAL_ABSOLUTE = re.compile(
    r"(?:[A-Za-z]:\\Users\\|/Users/|/home/[^/\s]+/"
    r"|(?<![\w:/])[A-Za-z]:[\\/]"
    r"|(?<![\w:/])/data/[^/\s]+/"
    r"|DESKTOP-[A-Z0-9]+)"
)
# Test files that deliberately embed drive-letter path examples for
# redaction/policy tests; skip the local-absolute-path check for them.
LOCAL_ABSOLUTE_TEST_EXEMPTIONS = {"tests/test_share_portal.py", "tests/test_repo_policy.py"}
PRIVATE_IPV4 = re.compile(
    r"(?<!\d)(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!\d)"
)
INFRASTRUCTURE_ID = re.compile(r"(?i)\b(?:gpu|cpu|node|login|compute)[-_]?\d{2,4}\b")
PERSONAL_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "users.noreply.github.com"}
SSH_IDENTITY = re.compile(
    r"(?im)\b(?:ssh|scp|rsync)\b[^\r\n]{0,120}\b[A-Z0-9._-]+@[A-Z0-9.-]+\b"
)
KNOWN_PRIVATE_IDENTIFIERS = (
    "".join(("hy", "wang")),
    "".join(("agent", "test")),
    "".join(("2211", "1520044")),
    "\u738b\u6d77\u6d0b",
    "\u5f20\u709c\u6c11",
    "\u9648\u9526\u94b0",
    "\u94b1\u53ef",
    "\u7eaa\u5bb6\u704f",
    "\u9648\u653f\u7ff0",
)


def scan_repo(root: Path | str = ROOT) -> list[str]:
    root = Path(root)
    violations = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP for part in path.parts):
            continue
        relative = path.relative_to(root)
        if relative.as_posix() == "scripts/repo_policy_check.py":
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            violations.append(f"large file: {relative}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".env.example":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            violations.append(f"possible secret: {relative}")
        if (
            relative.as_posix() not in LOCAL_ABSOLUTE_TEST_EXEMPTIONS
            and LOCAL_ABSOLUTE.search(text)
        ):
            violations.append(f"local absolute path: {relative}")
        if PRIVATE_IPV4.search(text):
            violations.append(f"private network address: {relative}")
        if INFRASTRUCTURE_ID.search(text):
            violations.append(f"infrastructure identifier: {relative}")
        if SSH_IDENTITY.search(text):
            violations.append(f"SSH identity or host: {relative}")
        for email in PERSONAL_EMAIL.findall(text):
            if email.rsplit("@", 1)[1].lower() not in SAFE_EMAIL_DOMAINS:
                violations.append(f"personal email: {relative}")
                break
        folded = text.casefold()
        if any(identifier.casefold() in folded for identifier in KNOWN_PRIVATE_IDENTIFIERS):
            violations.append(f"known private identifier: {relative}")
    return violations


def main() -> None:
    violations = scan_repo(ROOT)
    if violations:
        raise SystemExit("\n".join(violations))
    print("REPO_POLICY=OK")


if __name__ == "__main__":
    main()
