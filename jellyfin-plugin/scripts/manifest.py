"""Add a release to manifest.json, the plugin repository Jellyfin reads."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Jellyfin saves this beside the plugin when it installs it, for its card.
IMAGE = "https://raw.githubusercontent.com/Copas/Curatarr/main/jellyfin-plugin/logo.png"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--checksum", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()

    meta = {}
    for line in (ROOT / "build.yaml").read_text().splitlines():
        key, _, value = line.partition(":")
        if value.strip() and not line.startswith(" "):
            meta[key.strip()] = value.strip().strip('"')

    path = ROOT / "manifest.json"
    plugins = json.loads(path.read_text())
    if not plugins:
        plugins.append(
            {
                "guid": meta["guid"],
                "name": meta["name"],
                "overview": meta["overview"],
                "description": "Delete in any Jellyfin app asks Curatarr first: a film "
                "or show Curatarr manages is marked Leaving Soon instead of deleted.",
                "owner": meta["owner"],
                "category": meta["category"],
                "versions": [],
            }
        )
    plugins[0]["imageUrl"] = IMAGE
    versions = [v for v in plugins[0]["versions"] if v["version"] != args.version]
    versions.insert(
        0,
        {
            "version": args.version,
            "changelog": f"See https://github.com/Copas/Curatarr/releases/tag/plugin-v{args.version}",
            "targetAbi": meta["targetAbi"],
            "sourceUrl": args.url,
            "checksum": args.checksum,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    plugins[0]["versions"] = versions
    path.write_text(json.dumps(plugins, indent=2) + "\n")


if __name__ == "__main__":
    main()
