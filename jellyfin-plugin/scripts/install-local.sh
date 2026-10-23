#!/usr/bin/env bash
# Copy a build into a local Jellyfin's plugins folder and restart Jellyfin.
# Build first as yourself (dotnet publish ... -o artifacts), then run with sudo.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
dll="$here/artifacts/Jellyfin.Plugin.Curatarr.dll"
plugins="${JELLYFIN_PLUGINS:-/var/lib/jellyfin/plugins}"

[[ -f "$dll" ]] || { echo "No build at $dll: run 'dotnet publish Jellyfin.Plugin.Curatarr -c Release -o artifacts' first." >&2; exit 1; }
version="$(sed -n 's:.*<Version>\(.*\)</Version>.*:\1:p' "$here/Directory.Build.props")"
target="$plugins/Curatarr_$version"

# Older versions installed this way would load alongside the new one.
find "$plugins" -maxdepth 1 -type d -name 'Curatarr_*' ! -path "$target" -exec rm -rf {} +
install -d -o jellyfin -g jellyfin "$target"
install -o jellyfin -g jellyfin -m 0644 "$dll" "$here/logo.png" "$target/"
# Jellyfin reads the plugin's card picture from meta.json; installs from the
# repository get one written for them, this one says so itself.
cat > "$target/meta.json" <<JSON
{
  "category": "General",
  "guid": "42d6dc0d-a758-4b43-ab9c-303c6727aada",
  "name": "Curatarr",
  "owner": "Copas",
  "targetAbi": "$(sed -n 's:.*<JellyfinVersion>\(.*\)</JellyfinVersion>.*:\1:p' "$here/Directory.Build.props").0",
  "version": "$version",
  "status": "Active",
  "autoUpdate": true,
  "imagePath": "$target/logo.png",
  "assemblies": []
}
JSON
chown jellyfin:jellyfin "$target/meta.json"
systemctl restart jellyfin
echo "Installed Curatarr $version into $target and restarted Jellyfin."
