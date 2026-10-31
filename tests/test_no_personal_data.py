"""Nothing in the repository may identify a household (AGENTS.md).

This catches the generic kinds of personal data: home folders, network
addresses, email addresses and AI session links, in tracked files and commit
messages. A household's own names can't be listed here without publishing
them; a local git hook (docs/DEVELOPMENT.md, "Keeping personal data out")
checks those from a list kept outside the repository.
"""

import ipaddress
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Made-up or standard values that are fine anywhere.
HOME_OK = {"user", "you", "alex", "sam", "pat", "curatarr", "runner"}
EMAIL_OK = re.compile(
    r"@(?:[a-z0-9.-]+\.)?(?:example\.(?:com|org|net|local)|example|invalid|test)$"
    r"|@users\.noreply\.github\.com$|^noreply@"
)
IP_OK = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}
DOC_NETS = [
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
]

HOME = re.compile(r"/home/([A-Za-z0-9_.-]+)")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
SESSION = re.compile(r"Claude-Session|claude\.ai/code/session", re.IGNORECASE)


def problems(text: str) -> list[str]:
    found = []
    for name in HOME.findall(text):
        if name.lower() not in HOME_OK:
            found.append(f"home folder /home/{name}")
    for email in EMAIL.findall(text):
        if not EMAIL_OK.search(email.lower()):
            found.append(f"email address {email}")
    for match in IPV4.finditer(text):
        ip = match.group(1)
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            continue  # a version number such as 1.2.3.400
        if ip in IP_OK or any(address in net for net in DOC_NETS):
            continue
        if ip.endswith(".0"):
            continue  # a version (0.8.1.0) or a network, not a machine
        around = text[max(0, match.start() - 3) : match.end() + 1]
        in_link = (
            around.startswith(("//", "/", "@"))
            or "://" in around
            or (text[match.end() : match.end() + 1] == ":")
        )
        local = address.is_private or address in ipaddress.ip_network("100.64.0.0/10")
        if local or in_link:
            found.append(f"IP address {ip}")
    if SESSION.search(text):
        found.append("an AI session link")
    return found


def _git(*args) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def test_the_checks_catch_what_they_should():
    assert problems("see /home/robin/media and robin@gmail.com") == [
        "home folder /home/robin",
        "email address robin@gmail.com",
    ]
    assert problems("http://192.168.1.20:8096") == ["IP address 192.168.1.20"]
    assert problems("connect to 10.0.4.7 then") == ["IP address 10.0.4.7"]
    assert problems("https://93.184.215.14/x") == ["IP address 93.184.215.14"]
    assert problems("Claude-Session: https://claude.ai/code/session_x") == [
        "an AI session link"
    ]
    fine = (
        "/home/alex/Media, sam@example.com, 1+Pat@users.noreply.github.com, "
        "http://127.0.0.1:8787, http://192.0.2.10, version 1.2.3.4567, "
        "plugin 0.8.1.0 for Jellyfin 10.9.0.0, Python 3.12.1.2"
    )
    assert problems(fine) == []


def test_tracked_files_hold_no_personal_data():
    listed = _git("ls-files", "-z")
    if listed is None:
        pytest.skip("not a git checkout")
    found = []
    for name in filter(None, listed.split("\0")):
        path = ROOT / name
        if not path.is_file() or path.suffix.lower() in {
            ".png",
            ".jpg",
            ".ico",
            ".dll",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        found += [f"{name}: {problem}" for problem in problems(text)]
    assert found == []


def test_commit_messages_hold_no_personal_data():
    log = _git("log", "--format=%H%n%an <%ae>%n%B%x00")
    if log is None:
        pytest.skip("not a git checkout")
    found = []
    for entry in filter(None, (e.strip() for e in log.split("\0"))):
        sha, rest = entry.split("\n", 1)
        found += [f"{sha[:10]}: {problem}" for problem in problems(rest)]
    assert found == []
