"""Check a clean source export before local setup or repository initialization.

This checks common accidental disclosures. Human review is still necessary for
new source text, fixture prose, intellectual property, and publication rights.
Only the Python standard library is required; no files or network are accessed
outside the selected export directory.
"""
import ast
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = {
    ".env.example", ".gitignore", "AGENTS.md", "README.md", "PUBLIC_RELEASE.md",
    "manage.py", "requirements.txt", "requirements-importers.txt",
}
SOURCE_SUFFIXES = {
    "config": {".py"},
    "opportunities": {".py"},
    "scripts": {".py", ".ps1"},
    "static": {".css", ".js"},
    "templates": {".html"},
}
FORBIDDEN_PARTS = {
    ".git", ".env", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".notion-browser", ".slack-browser",
    ".gmail-browser", ".logs", ".backups", ".aws", ".ssh", ".codex",
    ".agents", ".vscode", ".idea", "node_modules", "staticfiles", "media",
    "uploads", "htmlcov",
}
SECRET_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"-{5}BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-{5}",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{50,}\b",
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b",
    r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b",
))
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
PERSONAL_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"']+"
    r"|/(?:Users|home)/[^/\s\"']+)"
)


def check_export(root):
    """Return findings without echoing potentially sensitive matched text."""
    root = Path(root)
    findings = []
    source_text = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(folders):
            path = parent / name
            relative = path.relative_to(root)
            if path.is_symlink() or path.is_junction() or name.casefold() in FORBIDDEN_PARTS:
                findings.append(f"{relative}: forbidden directory or filesystem link")
                folders.remove(name)
        for name in files:
            path = parent / name
            relative = path.relative_to(root)
            if path.is_symlink() or path.is_junction():
                findings.append(f"{relative}: filesystem links are not permitted")
                continue
            if len(relative.parts) == 1:
                permitted = name in ROOT_FILES
            else:
                permitted = path.suffix in SOURCE_SUFFIXES.get(relative.parts[0], set())
                permitted |= name == ".gitkeep" and relative.parts[0] in {"templates", "static"}
            if not permitted or any(part.casefold() in FORBIDDEN_PARTS for part in relative.parts):
                findings.append(f"{relative}: unexpected file in source export")
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeError, OSError):
                findings.append(f"{relative}: unreadable or binary file")
                continue
            source_text[relative.as_posix()] = content
            if any(pattern.search(content) for pattern in SECRET_PATTERNS):
                findings.append(f"{relative}: possible credential material")
            if PERSONAL_PATH.search(content):
                findings.append(f"{relative}: possible personal filesystem path")
            for match in EMAIL.finditer(content):
                domain = match[1].lower()
                # Deliberate URL basic-auth examples exercise URL rejection.
                prefix = content[max(0, match.start() - 80):match.start()]
                if re.search(r"https?://[^\s\"'<>/]*$", prefix):
                    continue
                if domain not in {"example.com", "example.org", "example.net", "example.edu"} and not domain.endswith((".invalid", ".example", ".test")):
                    findings.append(f"{relative}: non-example email address")
                    break

    expected_env = {
        "DJANGO_SECRET_KEY": "replace-with-a-generated-secret",
        "DJANGO_DEBUG": "True",
        "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "SLACK_CHANNEL_URLS": "",
        "GMAIL_ACCOUNT_EMAIL": "",
    }
    actual_env = {}
    for line in source_text.get(".env.example", "").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, separator, value = line.partition("=")
            if not separator or key in actual_env:
                findings.append(".env.example: invalid or duplicate configuration")
            actual_env[key] = value
    if actual_env != expected_env:
        findings.append(".env.example: expected only local placeholders and empty import configuration")

    constants = (
        ("opportunities/services/slack_source.py", "WORKSPACE_HOST"),
        ("opportunities/services/notion_public.py", "PUBLIC_HOST"),
    )
    for filename, constant in constants:
        try:
            tree = ast.parse(source_text.get(filename, ""))
            value = next(
                node.value.value for node in tree.body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and any(isinstance(target, ast.Name) and target.id == constant for target in node.targets)
            )
            if not isinstance(value, str) or not value.endswith(".invalid"):
                raise ValueError
        except (SyntaxError, StopIteration, ValueError):
            findings.append(f"{filename}: expected a reserved placeholder source host")
    return sorted(set(findings)), len(source_text)


def main():
    findings, count = check_export(ROOT)
    if findings:
        print("Public export check failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print(f"Public export check passed ({count} UTF-8 source/documentation files).")
    print("No runtime artifacts or common credential patterns found; source hosts are placeholders.")
    print("This automated check does not replace review of new content before publication.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
