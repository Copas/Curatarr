# Curatarr plugin for Jellyfin

Makes Jellyfin and Curatarr play nicely together. It lives in Curatarr's repository because it does nothing without Curatarr; it has its own releases (tagged `plugin-v…`), since each is built for a Jellyfin version.

## What it does

**Delete goes through Curatarr.** Without the plugin, Delete in a Jellyfin app removes the files behind Radarr's and Sonarr's backs: Radarr may download the film again, and Curatarr's notice period, Keep and Never Purge are skipped. With it, Delete in any Jellyfin app (web, phone, TV) asks Curatarr first:

- A **film or show Curatarr manages** is marked **Leaving Soon** instead of deleted. Its poster counts down, playing it or Keep in Curatarr saves it, and at the end of the notice period Curatarr deletes it through Radarr, or trims the show through Sonarr.
- **Deleting it again** (a title already Leaving Soon) deletes it now, the same way, as Delete in Curatarr's Review does.
- A title Curatarr **has to keep** (Never Purge, no file in Radarr) makes the delete fail, and the reason is in Jellyfin's log and Curatarr's Activity.
- **Episodes, seasons, music and anything Curatarr doesn't know** are deleted by Jellyfin as usual.

**Leaving Soon in the web app's menus.** Jellyfin's apps build their menus themselves, so a plugin can't add to the Roku, phone or TV apps' menus. The web app can be changed, through the [File Transformation](https://github.com/IAmParadox27/jellyfin-plugin-file-transformation) plugin: with it installed, the web app's menu for a film or show Curatarr manages has **Leaving Soon** beside **Delete**, and **Delete** there deletes straight away (through Radarr or Sonarr). Without it, the web app works like the other apps. It can be switched off in the plugin's settings.

Only people Jellyfin already lets delete an item can do any of this.

**Each device plays what it can play as it is.** Jellyfin plays a title's default audio track and its first copy, whatever the device can decode: a browser has AC3 or E-AC3 converted to AAC while a stereo AAC track sits beside it, and with two copies of a film it plays the first though the other would play directly. When a player asks how to play something, the plugin asks Jellyfin's own stream builder, with that device's profile, how each copy and each audio track in the same language would play, and points the player at the one needing the least work (played as it is, then copied into another container, then converted). Commentary tracks and other languages are never chosen, and a track or copy picked by hand is left alone. Curatarr's *Add a stereo AAC track for web browsers* (Settings → Enhance) adds the tracks this makes use of. It can be switched off in the plugin's settings. Deletes made with an API key (by other tools) aren't changed.

**Each account can have posters of its own.** Jellyfin has one poster per title, for every account. With Curatarr's custom posters for one account (a title's Poster panel, *Only for*, or Library → Posters → *Imported posters are for*), that account sees its own in every app (web, phone, TV, Roku), and everyone else sees the usual one. Apps fetch posters from an address with a tag in it, taken from the item details they were sent; the plugin puts a tag standing for the account's poster in what's sent to that account, and when an app asks for an image with that tag, it serves that poster, resized and cached by Jellyfin as its own posters are. The list comes from Curatarr every minute, and the pictures are kept in the plugin's data folder. It can be switched off in the plugin's settings.

## Requirements

- Jellyfin 12.2
- Curatarr 0.2.0 or later (the `/api/v1/jellyfin/delete` endpoint; posters for one account need the `/api/v1/jellyfin/posters` endpoint, from the custom posters release)

## Install

**From Curatarr** (easiest): Settings → **Curatarr plugin for Jellyfin** → **Install in Jellyfin**. Curatarr adds the plugin's repository to Jellyfin, installs the plugin and restarts Jellyfin; as soon as Jellyfin is back, it finds an address Jellyfin can reach it at (tested through the plugin) and gives the plugin that address and the webhook token, again whenever the token changes. A plugin installed by hand is connected with **Connect** on the same page.

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
