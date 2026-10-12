# 009: Chapter generation is post-import enrichment

Curatarr can add chapters to long movies and episodes that have none. This is media enrichment after an import, never part of acquisition or retention. It runs only when enabled for a library.

## What is generated

Three methods, tried in order until one produces valid chapters:

1. **Smart Chapters (semantic).** Scene changes come from FFmpeg's scene filter: 5 frames per second, scaled down, threshold 0.3 by default. A timestamped transcript comes from faster-whisper (local, GPU when available). For each boundary, Ollama is shown only the transcript around the allowed window and up to ten candidates (scene changes and pauses in speech, merged when they coincide). It picks the one where the story or topic moves on. Each chapter is then named separately, after a one-sentence summary of its dialogue. Naming every chapter in one request mostly produced character names.
2. **Scene-aligned.** Each boundary moves to the scene change nearest its target spacing. A window with no scene change keeps the target time instead of failing the plan.
3. **Fixed intervals.** Every target spacing.

All methods share one spacing rule (`chapters.next_window`). A chapter starts at 00:00:00. Each following boundary falls between the minimum and maximum spacing after the previous one (4/10 minutes by default, target 7), and never within two minutes of the end. Every list is validated before writing: it starts at zero, increases strictly, lies within the duration, and has one-line names.

Navigation Chapters (the user-facing name for scene-aligned with a fixed fallback) needs no transcription or language model. Smart Chapters falls back to it, so a missing faster-whisper, an unreachable Ollama, or a file without speech still produces chapters.

## Writing without touching the media

Only Matroska is written, in place with `mkvpropedit --chapters`. It edits the container's header elements and leaves audio, video, and subtitle data alone. Other containers (MP4 above all) would need a full remux, so they are recorded as `unsupported_container`. Rewriting a large file to add metadata is not worth the risk.

After each write the file is probed again. The stream list and the duration must be unchanged, and the chapters must read back exactly. Otherwise the job is marked failed and not retried. Only then is the single Jellyfin item refreshed (a default metadata refresh, which re-probes a changed file); the library is never rescanned.

Before writing, a job waits (it does not fail) while any of these hold: the file changed in the last ten minutes; Sonarr/Radarr still has a queue entry for that title or episode, or an import, rename, move, or rescan command is running for it; someone is playing the item in Jellyfin. If any of those cannot be checked, the job waits too.

## Existing chapters and ownership

Existing chapters are preserved by default. Curatarr regards chapters as its own only while the file still carries exactly what it wrote: `written_fingerprint`, a hash of times and names as read back from the file. Regenerate and remove are refused otherwise, in the worker and on the page, so chapters someone edited afterwards are safe too. If a library turns off "preserve existing chapters", the original chapters are first exported with `mkvextract` and stored, and removing Curatarr's chapters restores them.

## Idempotency and when files are processed

`chapter_states` has one row per media file. A file is processed automatically once, and again only when Sonarr/Radarr report a newer import of it (an upgrade replaces the file). Its date added then moves past Curatarr's last processing time. An unchanged file is skipped by size and modification time. Algorithm changes raise `GENERATION_VERSION`, and settings that shape chapters are hashed into `chapter_config_hash`. Neither triggers regeneration by itself: the owner asks for it.

Enabling a library records the moment it was enabled. Only files imported after that are queued automatically, so switching the feature on never sweeps an existing library. The existing library is handled from Chapter maintenance: a read-only scan with counts, then dry runs or processing of selected files or a whole library, with a confirmation showing the count.

## Jobs

The worker queues new imports after each reconciliation. On every cycle it starts due jobs in background threads, so a long transcription never delays lifecycle work. Concurrency defaults to 1 and is counted in the database, so the limit holds across processes. Each job holds a renewable lease (`chapters:<state id>`), and a job left running by a stopped worker is requeued. Scans only read files, so they run in a separate batch thread. Transcription runs in a child process, which releases the model's GPU memory after each file. Transcripts and scene lists are cached by file identity, so retries and previews do not repeat them.

## Pausing during playback

Chapter jobs read whole files from the media share and use the GPU. On the owner's server, three jobs running at once coincided with a Roku stream on 2.4 GHz Wi-Fi stalling repeatedly. So by default (a server setting), no chapter job starts while anything is playing in Jellyfin. Running jobs are frozen in place: each external tool runs in its own process group, which is sent SIGSTOP, then SIGCONT when playback ends. Language-model requests wait before they are sent. Time spent paused does not count toward a tool's time limit, so nothing is lost or restarted. If Jellyfin cannot be reached, work continues, so an outage cannot stall the backlog indefinitely. The maintenance page shows when work is paused.

## Generated subtitles

The Smart Chapters transcript also yields English subtitles at almost no extra cost. This is a separate per-library setting, off by default. A chapter job chains into a subtitle job for the same file, while the transcript is cached. A subtitle job first checks for English subtitles inside the file or beside it (`.en`, `.eng`, or untagged; forced tracks don't count). Untagged tracks count as English, so a second English track is never added beside a real one. Only if none is found is `<video>.en.Generated.srt` written, which Jellyfin lists as "Generated - English - SUBRIP - External". It is written to a temporary name and then renamed into place. A file Curatarr did not write is never replaced; ownership is the SHA-256 of the file's contents. Audio detected as another language is skipped. Cues are at most two lines of about 42 characters, at most 7 seconds, and never overlap. Sidecar subtitles work for any container, MP4 included. On a 40-show sample of the owner's library, only one episode had no subtitles at all, so this fills gaps rather than duplicating Bazarr.

Transcripts are keyed by path, duration, and audio tracks, not by file size and modification time. Adding chapters changes the latter, and the earlier key made the subtitle step transcribe every file a second time. Only one transcription runs at a time. With three concurrent jobs, Ollama, and Jellyfin's transcoding on a 12 GB GPU, 6 of 14 transcriptions ran out of GPU memory and fell back to the CPU, which took about 10 minutes instead of 1–2.

The owner's SMB share is case-insensitive, so `.en.generated.srt` and `.en.Generated.srt` name the same file there. The CIFS client's name cache also briefly maps a renamed name to the old file. Curatarr never renames subtitle files for this reason.

## Measured on the owner's server (2026-10-11)

For a 58-minute 1080p MKV episode on the NAS: scene detection took about 3 minutes (mostly the network read), transcription with faster-whisper `medium` on an RTX 3080 Ti about 3 minutes, and 18 Ollama requests to `qwen3:14b` about 8 minutes. That is about 14 minutes in all; the owner judged the chapters very good. The library's backlog was about 3,200 MKV files (2,040 hours) without chapters, roughly 20 days of Smart Chapters at concurrency 1. The owner accepted that: the backlog catches up, and new imports then take minutes each.

## Requirements

FFmpeg/ffprobe (Jellyfin's bundled copy is found automatically), MKVToolNix, and for Smart Chapters `pip install "curatarr[chapters]"`: `chapters-gpu` adds the CUDA libraries. Ollama with a chosen model is also needed. faster-whisper 1.2 is pinned to PyAV below 16, which removed an argument it still uses. The worker's service user must be able to modify the media files.
