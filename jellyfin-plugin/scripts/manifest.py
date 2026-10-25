"""Add a release to manifest.json, the plugin repository Jellyfin reads.

Each version's changelog comes from CHANGELOG.md (notes.py), so what Jellyfin
shows is what the release notes say. ``--refresh`` rewrites every listed
version's changelog and the plugin's description without adding a release.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from notes import notes, plain

ROOT = Path(__file__).resolve().parent.parent
# Jellyfin saves this beside the plugin when it installs it, for its card.
IMAGE = "https://raw.githubusercontent.com/Copas/Curatarr/main/jellyfin-plugin/logo.png"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version")
    parser.add_argument("--checksum")
    parser.add_argument("--url")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if not args.refresh and not (args.version and args.checksum and args.url):
        parser.error("--version, --checksum and --url are needed to add a release")

    meta = _meta()

    path = ROOT / "manifest.json"
    plugins = json.loads(path.read_text())
    if not plugins:
        plugins.append({"guid": meta["guid"], "name": meta["name"], "versions": []})
    plugin = plugins[0]
    plugin.update(
        overview=meta["overview"],
        description=meta["description"],
        owner=meta["owner"],
        category=meta["category"],
        imageUrl=IMAGE,
    )
    if not args.refresh:
        changelog = notes(args.version)
        if not changelog:
            sys.exit(f"CHANGELOG.md has no notes for plugin {args.version}")
        versions = [v for v in plugin["versions"] if v["version"] != args.version]
        versions.insert(
            0,
            {
                "version": args.version,
                "changelog": "",
                "targetAbi": meta["targetAbi"],
                "sourceUrl": args.url,
                "checksum": args.checksum,
                "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        plugin["versions"] = versions
    for entry in plugin["versions"]:
        found = notes(entry["version"])
        if found:
            entry["changelog"] = plain(found)
    path.write_text(json.dumps(plugins, indent=2, ensure_ascii=False) + "\n")


def _meta() -> dict:
    """build.yaml's top-level keys (a folded ">" value read as one line)."""
    meta, key = {}, None
    for line in (ROOT / "build.yaml").read_text().splitlines():
        if line.startswith(" ") and key:
            meta[key] = (meta[key] + " " + line.strip()).strip()
            continue
        name, _, value = line.partition(":")
        value = value.strip()
        if not name.strip() or line.startswith("-"):
            key = None
            continue
        key = name.strip()
        meta[key] = "" if value == ">" else value.strip('"')
    return meta


if __name__ == "__main__":
    main()
