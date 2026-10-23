# Curatarr plugin for Jellyfin

Makes Jellyfin and Curatarr play nicely together. It lives in Curatarr's repository because it does nothing without Curatarr; it has its own releases (tagged `plugin-v…`), since each is built for a Jellyfin version.

## What it does

**Delete goes through Curatarr.** Without the plugin, Delete in a Jellyfin app removes the files behind Radarr's and Sonarr's backs: Radarr may download the film again, and Curatarr's notice period, Keep and Never Purge are skipped. With it, Delete in any Jellyfin app (web, phone, TV) asks Curatarr first:

- A **film or show Curatarr manages** is marked **Leaving Soon** instead of deleted. Its poster counts down, playing it or Keep in Curatarr saves it, and at the end of the notice period Curatarr deletes it through Radarr, or trims the show through Sonarr.
- A title Curatarr **has to keep** (Never Purge, no file in Radarr) makes the delete fail, and the reason is in Jellyfin's log and Curatarr's Activity.
- **Episodes, seasons, music and anything Curatarr doesn't know** are deleted by Jellyfin as usual.

Only people Jellyfin already lets delete an item can do any of this. Deletes made with an API key (by other tools) aren't changed.

## Requirements

- Jellyfin 12.2
- Curatarr 0.2.0 or later (the `/api/v1/jellyfin/delete` endpoint)

## Install

**From Curatarr** (easiest): Settings → **Curatarr plugin for Jellyfin** → **Install in Jellyfin**, then **Restart Jellyfin** there. Curatarr adds the plugin's repository to Jellyfin, installs the plugin, and once Jellyfin has loaded it, gives it Curatarr's address and the webhook token. It updates them whenever they change (a new token, a new address).

**By hand**: in Jellyfin, **Dashboard → Plugins → Repositories → +**, add

```
https://raw.githubusercontent.com/Copas/Curatarr/main/jellyfin-plugin/manifest.json
```

install **Curatarr** from the catalogue, restart Jellyfin, then under **Dashboard → Plugins → Curatarr** enter:

- **Curatarr address**: as the Jellyfin server reaches it, e.g. `http://127.0.0.1:8787`.
- **Webhook token**: the one in Curatarr's Settings → Integrations (the same token the Webhook plugin sends).
- **Test** checks both before you save.

Either way, updates come through Jellyfin's plugin catalogue.

**Refuse deletes while Curatarr can't be reached** is on by default: while Curatarr is down, deletes fail rather than removing files Curatarr would have kept. Untick **Send deletes to Curatarr** to turn the plugin off.

## How it works

The plugin adds an ASP.NET filter in front of Jellyfin's `DELETE /Items/{id}` and `DELETE /Items?ids=` endpoints, which every Jellyfin app uses. For each item the signed-in user may delete, it asks Curatarr (`POST /api/v1/jellyfin/delete`). Items Curatarr takes aren't passed on, so Jellyfin answers "deleted" (204) and the title stays, now Leaving Soon. Some apps hide it until their next refresh.

Jellyfin has no official hook for this, so the filter matches its delete actions by name (`LibraryController.DeleteItem`/`DeleteItems`). If a Jellyfin update renames them, deletes simply work as they do without the plugin, and Jellyfin's log says so.

## Build

From this folder:

```bash
dotnet test Jellyfin.Plugin.Curatarr.Tests
dotnet publish Jellyfin.Plugin.Curatarr -c Release -o artifacts
```

`scripts/install-local.sh` copies a build into a local Jellyfin's plugin folder (run with sudo after building).

## License

MIT, like Curatarr.
