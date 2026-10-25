"""A plugin version's notes, from the Jellyfin plugin section of CHANGELOG.md.

    python3 jellyfin-plugin/scripts/notes.py 0.4.0.0            # Markdown
    python3 jellyfin-plugin/scripts/notes.py 0.4.0.0 --plain    # for Jellyfin

Exits with an error when the version has no notes, so a release can't go out
without them.
"""

import argparse
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


def notes(version: str, text: str | None = None) -> str:
    text = text if text is not None else CHANGELOG.read_text()
    section = text.split("\n## Jellyfin plugin\n", 1)
    if len(section) < 2:
        return ""
    plugin = section[1].split("\n## ", 1)[0]
    found = re.search(
        rf"^### {re.escape(version)}\b[^\n]*\n(.*?)(?=^### |\Z)", plugin, re.M | re.S
    )
    return found.group(1).strip() if found else ""


def plain(markdown: str) -> str:
    """Jellyfin shows the changelog as plain text: no emphasis marks."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", markdown)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)\*(?![\w*])", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--plain", action="store_true")
    args = parser.parse_args()
    found = notes(args.version)
    if not found:
        sys.exit(
            f"CHANGELOG.md has no notes for plugin {args.version}: add a "
            f"'### {args.version} (date)' entry under '## Jellyfin plugin'."
        )
    print(plain(found) if args.plain else found)


if __name__ == "__main__":
    main()
