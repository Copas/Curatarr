"""Section 99: tracked files must stay safe to publish."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(
    r"(^|/)\.env(\.(?!example$)[^/]+)?$"
    r"|(^|/)(instance/|logs/|backups?/|htmlcov/|\.coverage)"
    r"|\.(db|sqlite3?|log|pem|key|p12|pfx)$"
)
PRIVATE_IPV4 = re.compile(
    r"\b(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
)
# Jellyfin/Sonarr/Radarr keys are 32 hex characters; private keys have headers.
SECRET = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
MAX_BYTES = 1_000_000


def _tracked():
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [name for name in output.decode().split("\0") if name]


def test_no_runtime_or_secret_files_are_tracked():
    assert [name for name in _tracked() if FORBIDDEN.search(name)] == []


def test_tracked_files_are_small():
    assert [
        name for name in _tracked() if (ROOT / name).stat().st_size > MAX_BYTES
    ] == []


def test_no_private_addresses_or_key_material():
    findings = []
    for name in _tracked():
        if name == "tests/test_repository_hygiene.py":
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if PRIVATE_IPV4.search(line) or SECRET.search(line):
                findings.append(f"{name}:{number}")
    assert findings == []
