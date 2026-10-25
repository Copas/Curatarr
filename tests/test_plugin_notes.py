"""Every Jellyfin plugin release has notes in CHANGELOG.md (jellyfin-plugin/scripts)."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "jellyfin-plugin" / "scripts"))

import notes


def test_every_released_version_and_the_next_have_notes():
    manifest = json.loads((ROOT / "jellyfin-plugin" / "manifest.json").read_text())
    released = [v["version"] for v in manifest[0]["versions"]]
    building = re.search(
        r"<Version>([^<]+)</Version>",
        (ROOT / "jellyfin-plugin" / "Directory.Build.props").read_text(),
    ).group(1)
    for version in {*released, building}:
        assert notes.notes(version), f"no notes for plugin {version} in CHANGELOG.md"
    # What Jellyfin shows is the notes, not a link.
    for entry in manifest[0]["versions"]:
        assert entry["changelog"] == notes.plain(notes.notes(entry["version"]))


def test_notes_are_read_from_the_plugin_section_only():
    text = (
        "# Changelog\n\n## Jellyfin plugin\n\n### 1.0.0.0 (d)\n\n- **New** *thing* `x`\n\n"
        "### 0.9.0.0 (d)\n\n- old\n\n## 0.1.0\n\n### 1.0.0.0 (d)\n\n- not this\n"
    )
    assert notes.notes("1.0.0.0", text) == "- **New** *thing* `x`"
    assert notes.notes("0.9.0.0", text) == "- old"
    assert notes.notes("2.0.0.0", text) == ""
    assert notes.plain("- **New** *thing* `x`") == "- New thing x"
