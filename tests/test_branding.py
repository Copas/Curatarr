from pathlib import Path
from xml.etree import ElementTree

STATIC = Path(__file__).resolve().parents[1] / "curatarr" / "static"
SVG = "{http://www.w3.org/2000/svg}"


def _arrow_paths(filename):
    root = ElementTree.parse(STATIC / filename).getroot()
    groups = root.findall(f".//{SVG}g")
    arrows = [
        group for group in groups if group.attrib.get("class", "").startswith("arrow-")
    ]
    assert len(arrows) == 2
    return [
        [path.attrib["d"] for path in group.findall(f"{SVG}path")] for group in arrows
    ]


def test_icon_and_wordmark_use_the_same_two_arrows():
    icon = _arrow_paths("logo.svg")
    wordmark = _arrow_paths("wordmark.svg")
    assert icon == wordmark
    assert all(paths for paths in icon)


def test_navbar_uses_arrows_as_the_c(client):
    page = client.get("/")
    assert page.status_code == 200
    assert b'aria-label="Curatarr home"' in page.data
    assert b">uratarr</text>" in page.data
    assert b"curatarr-arrow-top" in page.data
    assert b"curatarr-arrow-bottom" in page.data
