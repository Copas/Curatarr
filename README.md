<p align="center">
  <img src="curatarr/static/logo.svg" alt="" width="96">
</p>

<h1 align="center">Curatarr</h1>

<p align="center">
  <strong>A caretaker for your Jellyfin library.</strong><br>
  It watches what your household plays, keeps the right things on disk, and quietly makes every file nicer to watch.
</p>

<p align="center">
  <a href="docs/INSTALL.md">Install</a> ·
  <a href="docs/FEATURES.md">How it works</a> ·
  <a href="docs/API.md">API</a> ·
  <a href="CHANGELOG.md">Changelog</a> ·
  <a href="docs/DEVELOPMENT.md">Development</a>
</p>

---

Curatarr sits beside **Jellyfin**, **Sonarr** and **Radarr**. It learns from what people actually watch, frees space by removing what nobody will miss, fetches the next season before anyone asks for it, and adds what downloads usually lack: chapters, subtitles, skippable recaps, and audio in the right language. It runs on your own server, uses local AI only, and is free and open source (MIT).

![Overview](docs/images/overview.png)

## What it does

### Keeps the right things on disk
- **Cleans up by need, not by rule of thumb.** When a disk or library runs low, it picks the titles nobody has watched for longest, shows them as *Leaving Soon* (with a badge on the poster in Jellyfin), and removes them only if nobody presses play before the notice period ends.
- **Stays one season ahead.** Finish an episode and the rest of the season, and the next one, are requested from Sonarr. Shows nobody continued stop downloading.
- **Never surprises you.** Every library starts in **dry run**. Deletions go through Sonarr or Radarr after fresh safety checks, and every decision is in History with the reason.

![Review queue](docs/images/review.png)

### Makes every file nicer to watch
- **Chapters**: adds them to files that have none: evenly spaced and snapped to scene changes, or *Smart Chapters* that are named from what's said (local Whisper + Ollama).
- **Subtitles**: English subtitles from the same transcript, for files that have none.
- **Recaps and previews**: finds *"Previously on…"* and *"Next time on…"* and marks them so Jellyfin offers Skip.
- **Better skip markers**: hides Intro Skipper guesses that are plainly wrong (a "credits" marker in the middle of an episode, a recap at the start of a movie), each one undoable.
- **English dubs**: for films made in another language, listens to each audio track to check it's really what its label says, makes the real English track the default, optionally removes tracks in other languages, and flags files with no English audio to replace.
- **Search what was said**: find the episode where someone said *that line*.

All of it writes only into MKV files, in place, without re-encoding, and only when you switch it on.

![Chapter maintenance](docs/images/chapters.png)

### Stays out of the way
- **Adaptive workload**: measures CPU, GPU and who's watching, and runs as much work as the machine has room for. When someone starts a stream it backs off, and it resumes when they stop.
- **Jellyfin sign-in**: administrators get the full interface; household members you allow can browse titles and say *Keep*.
- **Works alone**: no cloud service and no account. A read-only API key lets a home dashboard show its status.

## Install

| Your server | How | Guide |
|---|---|---|
| **Linux** (Debian, Ubuntu, Fedora, …) | Native install with systemd. Simplest, and the easiest way to use an NVIDIA GPU for Smart Chapters. | [Linux](docs/INSTALL.md#linux) |
| **Linux** with Docker | Docker Compose, with PostgreSQL included. | [Docker on Linux](docs/INSTALL.md#docker-on-linux) |
| **Windows 10 / 11** | Docker Desktop (WSL 2). | [Windows](docs/INSTALL.md#windows) |
| **macOS** | Docker Desktop, following the Windows steps in Terminal. Not regularly tested; chapter AI runs on the CPU. | [Windows](docs/INSTALL.md#windows) |

Quick start on Linux:

```bash
git clone https://github.com/Copas/Curatarr.git curatarr-release
cd curatarr-release
git checkout v0.2.0rc4
sudo EXTRAS=chapters-gpu ./scripts/install.sh   # or leave EXTRAS out for library management only
```

Then open `http://<your-server>:8787` and sign in with a Jellyfin administrator account. The [first-run checklist](docs/INSTALL.md#first-run) walks you through connecting Jellyfin, Sonarr and Radarr.

### Requirements

| What | Needed for |
|---|---|
| Jellyfin, Sonarr and Radarr (current versions; Curatarr uses their API v3 and Jellyfin's REST API) | Everything |
| Jellyfin **Webhook** plugin | Instant playback updates (otherwise hourly) |
| FFmpeg and **MKVToolNix** | Chapters, subtitles, dub checks |
| **Ollama** with a model (e.g. `qwen3:14b`) and optionally an NVIDIA GPU | Smart Chapters, recap and preview detection |
| Jellyfin **Intro Skipper** plugin | Recap/preview skip buttons and marker clean-up |

## Is it safe to point at my real library?

Curatarr was built on a real 8,000-file library and is careful by design:

- **Dry run is on by default** for every library. Curatarr shows what it *would* remove and why, until you switch that off.
- **Deletions only go through Sonarr or Radarr**, after Curatarr rechecks playback, grace periods, the download queue and integration health. If anything can't be verified, nothing is deleted.
- **File edits never re-encode.** Chapters and track flags are written with `mkvpropedit`. Removing language tracks rewrites a copy that's verified before it replaces the original.
- **Everything is undoable or recorded.** Markers it hid can be restored, chapters it wrote can be removed (the originals are kept), and every action is in History.

It's still a release candidate: read [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for known gaps.

## Learn more

- [How it works](docs/FEATURES.md): retention rules, acquisition, sign-in, chapters, skip markers, dubs and search in detail.
- [Installing, upgrading and backing up](docs/INSTALL.md)
- [API, dashboards, logs and metrics](docs/API.md)
- [Developing Curatarr](docs/DEVELOPMENT.md) and [design decisions](docs/decisions/)

## License

[MIT](LICENSE)
