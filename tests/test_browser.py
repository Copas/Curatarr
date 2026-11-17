"""Page checks in a real browser (tests/browser/*.mjs): contrast in light and
dark, the theme following the device, parts of pages redrawn in place,
notes, the picture viewer, background checks, the security headers' effect.

They start a demo Curatarr on a scratch database and drive headless Chrome,
so they take a few minutes and need Chrome and Node 22 or newer. They run on
GitHub with every push, and locally when asked:

    CURATARR_BROWSER_CHECKS=1 .venv/bin/pytest tests/test_browser.py
"""

import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).parent / "browser"
ROOT = Path(__file__).parents[1]
CHECKS = sorted(p for p in HERE.glob("*.mjs") if p.name != "driver.mjs")
CHROME = next(
    (
        found
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        )
        if (found := shutil.which(name))
    ),
    None,
)
NODE = shutil.which("node")


def _node_major() -> int:
    try:
        out = subprocess.run(
            [NODE, "--version"], capture_output=True, text=True, check=False
        )
        return int(out.stdout.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, TypeError):
        return 0


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("CURATARR_BROWSER_CHECKS") != "1",
        reason="browser checks: set CURATARR_BROWSER_CHECKS=1",
    ),
    pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium"),
    pytest.mark.skipif(NODE is None or _node_major() < 22, reason="needs Node 22+"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """A demo Curatarr on a scratch database, in its own process."""
    folder = tmp_path_factory.mktemp("browser")
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{folder / 'demo.db'}",
        "CURATARR_DEMO_MODE": "true",
        "CURATARR_ALLOW_UNAUTHENTICATED": "true",
        "CURATARR_SECRET_KEY": secrets.token_urlsafe(32),
        "FLASK_APP": "curatarr",
    }
    flask = [sys.executable, "-m", "flask"]
    for args in (["db", "upgrade"], ["demo-seed"], ["worker", "--once"]):
        subprocess.run(
            flask + args, env=env, cwd=folder, check=True, capture_output=True
        )
    port = _free_port()
    log = (folder / "server.log").open("w")
    server = subprocess.Popen(
        flask + ["run", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=folder,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/login", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    else:
        server.kill()
        pytest.fail(
            "the demo server didn't start: " + (folder / "server.log").read_text()
        )
    yield base
    server.terminate()
    server.wait(10)


@pytest.mark.parametrize("check", CHECKS, ids=[p.stem for p in CHECKS])
def test_pages_in_a_browser(demo, check):
    result = subprocess.run(
        [NODE, str(HERE / "driver.mjs"), str(check)],
        env={**os.environ, "BASE": demo, "CHROME": CHROME},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
