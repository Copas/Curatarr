"""Write curatarr/static/icons/: the app's icons for a phone's home screen,
the web app manifest and old browsers asking for /favicon.ico, from
static/logo.svg (the C-arrow mark, in its dark-scheme colours, on Curatarr's
dark background, inside the middle 70% so a round or squircle mask keeps
all of it).

Needs Chrome or Chromium (it draws the SVG). Run after changing the logo:
    .venv/bin/python scripts/app_icons.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

STATIC = Path(__file__).resolve().parents[1] / "curatarr" / "static"
OUT = STATIC / "icons"
BACKGROUND = "#101923"  # --app-bg, dark
SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}

PAGE = """<!doctype html><html><head><style>
html, body {{ margin: 0; width: {size}px; height: {size}px; background: {bg}; }}
body {{ display: grid; place-items: center; }}
svg {{ width: 70%; height: 70%; }}
</style></head><body>{svg}</body></html>"""


def chrome():
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        if found := shutil.which(name):
            return found
    sys.exit("Needs Chrome or Chromium to draw the logo")


def draw(size: int, target: Path, folder: Path):
    # The logo's own dark-scheme colours, whatever the machine's setting.
    svg = (
        (STATIC / "logo.svg")
        .read_text()
        .replace("prefers-color-scheme: light", "not all")
    )
    page = folder / f"icon-{size}.html"
    page.write_text(PAGE.format(size=size, bg=BACKGROUND, svg=svg))
    subprocess.run(
        [chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--window-size={size},{size}", f"--screenshot={target}", page.as_uri()],
        check=True, capture_output=True,
    )  # fmt: skip


def main():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        for name, size in SIZES.items():
            draw(size, OUT / name, Path(folder))
    big = Image.open(OUT / "icon-512.png").convert("RGBA")
    big.save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])


if __name__ == "__main__":
    main()
