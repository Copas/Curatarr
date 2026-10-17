<p align="center">
  <img src="curatarr/static/logo.svg" alt="" width="96">
</p>

<h1 align="center">Curatarr</h1>

<p align="center">
  <strong>The caretaker your Jellyfin library has been missing.</strong><br>
  It learns what your household actually watches, keeps the disk from filling up, fetches the next season before anyone asks, and fixes the downloads that came out wrong.
</p>

<p align="center">
  <a href="docs/INSTALL.md">Install</a> ·
  <a href="docs/FEATURES.md">How it works</a> ·
  <a href="docs/API.md">API</a> ·
  <a href="CHANGELOG.md">Changelog</a> ·
  <a href="docs/DEVELOPMENT.md">Development</a>
</p>

---

## What you need

| | |
|---|---|
| **Jellyfin** | Required. Curatarr reads who watched what, and you sign in with your Jellyfin account. |
| **Sonarr** and/or **Radarr** | At least one. With Sonarr, Curatarr looks after your TV shows; with Radarr, your films; with both, everything. |

That's all you need to get started. Add these when you want more:

| Optional | Unlocks |
|---|---|
| Jellyfin **Webhook** plugin | Instant reactions to playback (otherwise Curatarr catches up hourly) |
| **FFmpeg** and **MKVToolNix** | Chapters, English-dub checks, file problem checks |
| **Local AI**: Whisper (installed with the `chapters` extra) and **Ollama** with a model (e.g. `qwen3:14b`), ideally on an NVIDIA GPU | Smart Chapters, subtitles, recap and preview detection, Fun facts, search by what was said |
| Jellyfin **Intro Skipper** plugin | Skip buttons for recaps and previews, and clean-up of bad skip markers |
| **Seerr** (or Jellyseerr/Overseerr) | Requested titles are kept until whoever asked has watched them |

Everything runs on your own server. No cloud service, no account, no tracking. Free and open source (MIT).

![Overview: everything that needs you, in one list](docs/images/overview.png)

## Why you'll want it

### 💾 Never run out of space, and never lose what someone's in the middle of
When a disk runs low, Curatarr picks the titles nobody has touched for longest and puts them on a **Leaving Soon** shelf, with a badge on the poster in Jellyfin. Anyone who presses play saves it. New arrivals get a grace period, favourites go to the back of the line, and you can keep, snooze or protect anything for good.

It knows what your household cares about, too:

- **Asked for it in Seerr?** It stays until whoever asked has watched it.
- **The comfort rewatches** go to the back of the line without anyone pressing Keep.
- **What an import list dragged in** and nobody played goes first.
- **Halloween films stay through October** (and Christmas ones through December), and can even come back before their season.
- **Shrink instead of delete:** swap a 25 GB copy of a film you keep for a 6 GB one, only if one exists.
- **A disk forecast** says when you'll run low at this week's pace, and what would go first.

![The review queue: why each title was picked, and what happens next](docs/images/review.png)

### 📺 The next episode is already there
Finish an episode and Curatarr asks Sonarr for the rest of the season, and the next one, before anyone has to. Shows nobody continued stop downloading. Every show keeps its first few episodes, so there's always something to start on; if one of those never arrived, Curatarr says so and searches once at a click.

### 🛠️ Bad downloads, caught and fixed
Curatarr checks every file so you don't find out halfway through a film:

- **Subtitles burned into the picture** that can't be turned off
- **Incomplete downloads** where the picture stops before the end, and files much shorter than they should be
- **The wrong show entirely**: a 1987 namesake filed under your 2019 series
- **No English audio** on a film that was sold as an English dub
- **Wrong episodes**: a file whose dialogue matches a different episode's summary

Click **Replace** and Curatarr first checks that a better copy actually exists. Only then does it blocklist the bad release, delete the file and grab the new one. If nothing better is out there, it keeps what you have and tells you so.

![Problems: each with a fix, and a status once you've clicked](docs/images/problems.png)

### ✨ Every file nicer to watch
- **Chapters** for files that have none, named from what's actually said (*Smart Chapters*, local Whisper + Ollama), or evenly spaced at scene changes.
- **English subtitles** made from the soundtrack, for files that have none.
- **Skip "Previously on…" and "Next time on…"** with Jellyfin's own Skip button.
- **Better skip markers**: Intro Skipper guesses that are plainly wrong are hidden, each one undoable.
- **The right audio first**: tracks labelled in the wrong language are relabelled, and the real English track becomes the default.
- **Audio every TV can play**: films and episodes with DTS, TrueHD or FLAC sound get an AC3 copy beside the original, so a Roku or TV plays them directly instead of stumbling over conversion.

The picture is never re-encoded, the only thing ever encoded is that optional extra audio track, and none of it happens until you switch it on.

![Enhance: what Curatarr has done for your library so far](docs/images/chapters.png)

### 🎉 Fun facts the whole household will argue about
Which film swears most per hour? Which show says "I love you" most? Who talks longest without a pause, and what's each show's catchphrase? **Fun facts** ranks your library by what's actually said in it, all per hour or per minute so a long-running show doesn't win just by being long. Handy, too, for finding the films that don't swear at all when the kids are around.

![Fun facts: superlatives from what's said in your films and shows](docs/images/fun.png)

### 🔎 Find the episode where someone said *that line*
Search every transcript for a quote or a topic and jump straight to the episode, with the lines around it and the timestamp.

### 📱 On your phone, for the whole household
Everything works on a phone. Household members sign in with their own Jellyfin accounts and can browse, search and say *Keep*; only administrators can approve deletions or change settings.

<p align="center"><img src="docs/images/overview-phone.png" alt="Curatarr on a phone" width="270"></p>

## Safe to point at your real library

Curatarr was built on a real 8,000-file library and is careful by design:

- **Dry run is on by default** for every library. Curatarr shows what it *would* remove and why, until you switch that off.
- **Deletions only go through Sonarr or Radarr**, after Curatarr rechecks playback, grace periods, the download queue and connection health. If anything can't be verified, nothing is deleted.
- **Replace never leaves you with nothing.** It confirms a better copy can be found before it deletes the one you have.
- **File edits never re-encode the picture.** Chapters and track flags are written in place with `mkvpropedit`. Removing language tracks, or adding a TV-friendly audio track (the one thing that encodes, and only that track), writes a copy that's checked before it replaces the original, which keeps every original track.
- **Everything is undoable or recorded.** Markers it hid can be restored, chapters it wrote can be removed (your originals are kept), and every action is in History with the reason.
- **It steps aside for viewers.** It measures CPU, GPU and who's watching, and backs off when someone starts a stream.

It's still a release candidate: [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) lists the known gaps.

## Install

| Your server | How | Guide |
|---|---|---|
| **Linux** (Debian, Ubuntu, Fedora, …) | Native install with systemd. Simplest, and the easiest way to use an NVIDIA GPU. | [Linux](docs/INSTALL.md#linux) |
| **Linux** with Docker | Docker Compose, with PostgreSQL included. | [Docker on Linux](docs/INSTALL.md#docker-on-linux) |
| **Windows 10 / 11** | Docker Desktop (WSL 2). | [Windows](docs/INSTALL.md#windows) |
| **macOS** | Docker Desktop, following the Windows steps in Terminal. Not regularly tested; the AI features run on the CPU. | [Windows](docs/INSTALL.md#windows) |

Quick start on Linux:

```bash
git clone https://github.com/Copas/Curatarr.git curatarr-release
cd curatarr-release
git checkout v0.2.0rc4
sudo EXTRAS=chapters-gpu ./scripts/install.sh   # or leave EXTRAS out for library management only
```

Then open `http://<your-server>:8787` and sign in with a Jellyfin administrator account. The [first-run checklist](docs/INSTALL.md#first-run) walks you through connecting Jellyfin, and Sonarr and/or Radarr.

## Learn more

- [How it works](docs/FEATURES.md): cleanup and acquisition rules, sign-in, chapters, skip markers, dubs, problems, search and fun facts in detail.
- [Installing, upgrading and backing up](docs/INSTALL.md)
- [API, dashboards, logs and metrics](docs/API.md)
- [Developing Curatarr](docs/DEVELOPMENT.md) and [design decisions](docs/decisions/)

## License

[MIT](LICENSE)
