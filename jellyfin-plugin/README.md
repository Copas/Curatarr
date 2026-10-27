# Curatarr plugin for Jellyfin

Makes Jellyfin and Curatarr play nicely together. It lives in Curatarr's repository because it does nothing without Curatarr; it has its own releases (tagged `plugin-v…`), since each is built for a Jellyfin version.

## What it does

**Delete goes through Curatarr.** Without the plugin, Delete in a Jellyfin app removes the files behind Radarr's and Sonarr's backs: Radarr may download the film again, and Curatarr's notice period, Keep and Never Purge are skipped. With it, Delete in any Jellyfin app (web, phone, TV) asks Curatarr first:

- A **film or show Curatarr manages** is marked **Leaving Soon** instead of deleted. Its poster counts down, playing it or Keep in Curatarr saves it, and at the end of the notice period Curatarr deletes it through Radarr, or trims the show through Sonarr.
- **Deleting it again** (a title already Leaving Soon) deletes it now, the same way, as Delete in Curatarr's Review does.
- A title Curatarr **has to keep** (Never Purge, no file in Radarr) makes the delete fail, and the reason is in Jellyfin's log and Curatarr's Activity.
- **Episodes, seasons, music and anything Curatarr doesn't know** are deleted by Jellyfin as usual.

**Leaving Soon in the web app's menus.** Jellyfin's apps build their menus themselves, so a plugin can't add to the Roku, phone or TV apps' menus. The web app can be changed, through the [File Transformation](https://github.com/IAmParadox27/jellyfin-plugin-file-transformation) plugin: with it installed, the web app's menu for a film or show Curatarr manages has **Leaving Soon** beside **Delete**, and **Delete** there deletes straight away (through Radarr or Sonarr). Without it, the web app works like the other apps. It can be switched off in the plugin's settings.

**Leave after I watch, in the web app's menus** (plugin 0.6.0.0, with the File Transformation plugin): for a film, show, season or episode Curatarr knows, the menu has **Leave after I watch**. It marks the item Leaving Soon once the account that chose it has watched it (a show or season once its last episode on disk is watched; one episode once it is). **Don't leave after watched** undoes it. The choice between each episode as it's watched and a whole season at once is on the title's page in Curatarr.

Only people Jellyfin already lets delete an item can do any of this.

**Each device plays what it can play as it is.** Jellyfin plays a title's default audio track and its first copy, whatever the device can decode: a browser has AC3 or E-AC3 converted to AAC while a stereo AAC track sits beside it, and with two copies of a film it plays the first though the other would play directly. When a player asks how to play something, the plugin asks Jellyfin's own stream builder, with that device's profile, how each copy and each audio track in the same language would play, and points the player at the one needing the least work (played as it is, then copied into another container, then converted). Commentary tracks and other languages are never chosen, and a track or copy picked by hand is left alone. Curatarr's *Add a stereo AAC track for web browsers* (Settings → Enhance) adds the tracks this makes use of. It can be switched off in the plugin's settings. Deletes made with an API key (by other tools) aren't changed.

**Each account can have posters of its own.** Jellyfin has one poster per title, for every account. With Curatarr's custom posters for one account (a title's Pictures panel, *Only for*, or Library → Posters → *Imported pictures are for*), that account sees its own in every app (web, phone, TV, Roku), and everyone else sees the usual one. Apps fetch posters from an address with a tag in it, taken from the item details they were sent; the plugin puts a tag standing for the account's poster in what's sent to that account, and when an app asks for an image with that tag, it serves that poster, resized and cached by Jellyfin as its own posters are. Rows of wide tiles (the Roku app's home rows, Continue Watching) use a title's landscape picture instead (its Thumb), so from 0.7.0.0 the plugin also swaps that for a landscape version Curatarr draws. From 0.8.0.0 an account can have each of a title's pictures of its own: its wide tile, background, logo (served as a PNG, see-through parts kept) and banner, including a show's on its episodes and seasons. The list comes from Curatarr every minute, and the pictures are kept in the plugin's data folder. It can be switched off in the plugin's settings.

## Requirements

- Jellyfin 12.2
- Curatarr 0.2.0 or later (the `/api/v1/jellyfin/delete` endpoint; posters for one account need Curatarr 0.2.0rc6 or later, and favourite ribbons' page version and Leave after I watch 0.2.0rc7 or later, wide tiles 0.2.0rc8 or later, other pictures 0.2.0rc9 or later)

## Install

**From Curatarr** (easiest): Settings → **Curatarr plugin for Jellyfin** → **Install in Jellyfin**. Curatarr adds the plugin's repository to Jellyfin, installs the plugin and restarts Jellyfin; as soon as Jellyfin is back, it finds an address Jellyfin can reach it at (tested through the plugin) and gives the plugin that address and the webhook token, again whenever the token changes. A plugin installed by hand is connected with **Connect** on the same page. With **Install plugin updates automatically** on (the same page), a newer plugin is installed as soon as Curatarr needs it, and Jellyfin is restarted once nobody has watched anything for a while.

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

## Releasing

1. Set `<Version>` in `Directory.Build.props`.
2. Add the version's notes to [CHANGELOG.md](../CHANGELOG.md), under **Jellyfin plugin**, as `### 0.5.0.0 (date)` with a bullet per change. They become the GitHub release's notes and the changelog Jellyfin shows for the version.
3. Push a tag `plugin-v0.5.0.0`. The release workflow refuses a version without notes; with them it runs the tests, builds the plugin, attaches it to a release and adds it to `manifest.json`.

`python3 scripts/manifest.py --refresh` rewrites every listed version's changelog, and the plugin's description from `build.yaml`, after notes are edited.

## License

MIT, like Curatarr.
