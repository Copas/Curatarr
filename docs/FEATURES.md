# How Curatarr works

- [Signing in and who sees what](#signing-in-and-who-sees-what)
- [Keeping the right things on disk](#keeping-the-right-things-on-disk): retention and acquisition
- [Policies](#policies)
- [Chapters and subtitles](#chapters-and-subtitles)
- [Recaps, previews and skip markers](#recaps-previews-and-skip-markers)
- [English dubs](#english-dubs)
- [Problems: burned-in subtitles and broken files](#problems-burned-in-subtitles-and-broken-files)
- [Search what was said](#search-what-was-said)
- [Collections and suggestions](#collections-and-suggestions)
- [Fun facts](#fun-facts)
- [Sharing the machine with playback](#sharing-the-machine-with-playback)

The interface groups daily decisions under **Overview** and **Review**, title browsing under **Library**, and file improvements under **Enhance**, and administrator controls under **Activity** and **Settings**. The theme button in the header switches between dark and light for that browser.

---

## Signing in and who sees what

Sign in with a Jellyfin account that has the **Administrator** permission, or with an enabled non-admin account that an administrator has allowed in Curatarr. Curatarr checks the password with Jellyfin, ends the Jellyfin session it created, and keeps only the user's ID, name, role and verification time in a signed session cookie that lasts up to seven days. Passwords and Jellyfin user tokens are never stored.

- Every five minutes Curatarr re-checks the account using its own Jellyfin API key, so a removed, disabled or role-changed account loses access. If Jellyfin is unreachable, an existing session continues for up to one hour after its last successful check.
- After five failed sign-ins from one address within 15 minutes, further attempts are refused until the window passes.
- On first run, the sign-in page also asks for the Jellyfin server URL and binds Curatarr to it. Until that first administrator signs in, anyone who can reach the page could bind a different server, so keep Curatarr on a trusted network until setup is done, or set `CURATARR_JELLYFIN_URL` so the server is fixed from the start.

**Household access.** Administrators can enable specific non-admin Jellyfin users in Settings → Integrations → Household access. Those users can browse managed titles, search what was said, and choose **Keep**, **Snooze** or **Never Purge** in Review. They can't approve deletion, change rules or integrations, run discovery, reset or request a show, or see full History and Operations. Curatarr rechecks their Jellyfin account periodically and ends a household session as soon as its allowlist entry is removed.

`/health`, `/api/v1/status` and the token-protected webhook stay public for dashboards. Every other page and API endpoint requires sign-in, and operational APIs require administrator access (or the read-only API key; see [API](API.md)). Set `CURATARR_ALLOW_UNAUTHENTICATED=true` only when another layer, such as an authenticating reverse proxy, already restricts access: it grants the full admin interface.

Integration keys and the webhook token are encrypted in the database with a key derived from `CURATARR_SECRET_KEY`. Losing or changing that key makes them unreadable, so back up the key together with the database. Curatarr needs no media access for library management: deletion always goes through Sonarr or Radarr.

---

## Keeping the right things on disk

### Cleanup is driven by free space

Titles nobody has played are not removed just for being unwatched. (A library can opt into that with *Clean up unwatched titles even when space is fine*.) When free space falls below a library's **low** threshold, Curatarr picks titles in purge-strategy order (longest unwatched first by default), and only enough to get back to the threshold.

1. They show as **Leaving Soon** for the notice period (14 days by default), with a badge on their poster in Jellyfin.
2. They're removed when the notice ends, unless someone watches them. **Any playback** resets inactivity and rescues pending cleanup.
3. Below the **critical** threshold, removal happens without a notice.

The default grace period after adding a title is 30 days, and the inactivity period is 90 days. Movie cleanup is retention only: deleted movies are never automatically re-acquired.

### Before anything is deleted

**Dry run is on by default.** In dry run Curatarr goes through the whole process and records what it would remove in History, but never asks Sonarr or Radarr to delete anything. The banner at the top of every page says which libraries are in dry run; turn it off globally or per library.

Before each deletion Curatarr rechecks the mapping, recent activity, grace period, policy, download queue and integration health. If it can't verify these, it doesn't delete. Approving a deletion in Review has its own confirmation page. All decisions and outcomes are written to History.

The review queue offers **Delete**, **Keep**, **Snooze** and **Never Purge**.

### TV: one season ahead

- The default always-keep minimum for a show is the first three episodes of Season 1.
- A completed episode may request missing episodes in that season and the next known season. Requests for a whole season also monitor that season in Sonarr, so episodes announced later are downloaded too.
- Once viewers have watched the latest season (by the acquisition threshold), the next season is requested whenever it appears, without the recent-viewing limit. A show not continued although its next season was already out is treated as abandoned.
- While viewers are on a season that's still airing, Curatarr makes sure Sonarr monitors it even when every episode so far is downloaded.
- A show's title page has **Reset to minimum**, which trims it to its always-keep episodes as if nobody had watched it. The rest are deleted through Sonarr with the usual checks (except the grace period and recent viewing), all seasons are unmonitored, and viewing before the reset no longer drives acquisition. Dry run is respected.

---

## Policies

Policy resolves **title override → library → global → built-in default**. The Acquisition and Retention pages edit global defaults or any one library, and show each effective value with where it comes from.

- Every setting, including library-size limits, free-space enforcement and dry run, can be set globally or per library.
- Library-size limits apply to each library separately. Free space is measured per disk and shared by every library on it.
- A global change is rejected if it would leave any library with an invalid effective policy.
- **Review mode** can be set per library and per cleanup rule: inactivity, quota, and low/critical disk pressure each have their own mode, which inherits the library's when left blank. So one rule can run automatically while the others still require review. A title-level review mode overrides all rule modes.
- Title overrides include Never Purge, thresholds, minimum footprint and review behaviour.
- Quota rules require high-water bytes strictly above low-water bytes.
- Disk-pressure rules need an explicit path **as Sonarr or Radarr sees it**. When that path can't be matched to a reported volume, Curatarr doesn't select media on disk pressure alone.

---

## Chapters and subtitles

Optional, and off until you enable it per library in **Settings → Enhance**.

After Sonarr or Radarr finish importing a movie or episode of at least 20 minutes that has no chapters, Curatarr adds them:

- **Navigation Chapters** places one about every 7 minutes (4–10 minutes apart), moved to the nearest scene change.
- **Smart Chapters** also transcribes the audio locally with faster-whisper, asks an Ollama model where the story moves on and what to call each chapter, and falls back to Navigation Chapters if that fails.

Chapters are written into MKV files in place with `mkvpropedit`, which re-encodes nothing; other containers are reported as unsupported. Every file is verified afterwards, and only that item is refreshed in Jellyfin.

- **Existing chapters are preserved.** Curatarr regenerates or removes only chapters it can show it wrote, and keeps the originals of any it replaced.
- **Your existing library is never processed by surprise.** Enhance → Files scans it read-only, shows what's done and what's left, and processes only the files or libraries you confirm. Each file's page shows a preview: the scene changes, the plan from each method, and the exact write command, before anything is modified.
- **Subtitles.** The same transcript can produce English subtitles (`<video>.en.Curatarr.srt`), a separate per-library setting. They're added only when a file has no English subtitles of its own, never written over another file, and removable from the file's page.
- **Progress.** Enhance → Progress leads with what Curatarr has done (chapters and subtitles added, recaps and previews marked, bad markers hidden) and how much of the library is covered, then what's left to do, with an estimated finish based on the last day's pace.

Smart Chapters needs the `chapters` or `chapters-gpu` extra, Ollama with a model, and write access to the media files for the worker (see [Installing](INSTALL.md#optional-chapter-generation)). `flask --app curatarr chapters preview <file>` prints the full analysis for one file without changing it. Design notes: [decision 009](decisions/009-chapter-enrichment.md).

---

## Recaps, previews and skip markers

With the Jellyfin **Intro Skipper** plugin installed, Curatarr improves the skip buttons Jellyfin shows. It works on episodes in libraries with Smart Chapters. Press **Check skip markers** on Enhance → Progress to check every episode not yet checked (press it again later for newly added ones), or check one episode from its page. Checks reuse Smart Chapters transcripts where they exist and wait while a chapter backlog is running.

- **Recaps.** A *"Previously on…"* segment is found from the opening transcript: a known opener phrase ("previously on", "last time on", "here's what you missed", …), and an end found from the series' own closing line or from where the quoted clips of earlier episodes stop. It's marked as an Intro Skipper **Recap** segment and, if the chapters are Curatarr's, a *Previously On* chapter.
- **Previews.** A *"Next time on…"* segment at the end is marked as a **Preview** segment.
- **Bad markers hidden.** Intro Skipper sometimes guesses wrong: a short "credits" marker with lots of dialogue after it, credits in the first quarter of an episode, an "intro" that's mostly unique dialogue. Curatarr hides those, recording why.
- **Movies keep only their credits marker** (on by default). Opening chapter names like "Chapter 1" made Intro Skipper mark recaps and intros at the start of movies; they're hidden. Credits stay, for *Skip Credits* and *Next*.

Everything is listed on **Enhance → Skip markers**, newest first, with the span, the reason and a link to the episode, and each change has **Undo**.

Jellyfin decides per user whether to show a Skip button for each segment type (Settings → Playback → Media segments). Some clients, such as the Roku app, keep their own per-device setting.

---

## English dubs

For households that watch films dubbed rather than subtitled. On by default; settings in Settings → Enhance.

Radarr can grab an English dub of a film made in another language, but uploads are sometimes labelled English when the audio is really the original. For films whose original language (from Radarr) isn't English, and for English films whose file also carries other languages, Curatarr **listens**: five 20-second clips from the middle of the film, per audio track, through Whisper's language identification, with a voice filter so music isn't mistaken for speech. A track's language counts only when most clips agree with high confidence. This is not a transcription, and takes seconds per film.

What it does with the result:

| Found | Action |
|---|---|
| A track whose label is wrong or missing | Relabels it (in place, no re-encode) |
| A real English track that isn't the default, while the default is clearly another language | Makes the English track the default |
| No English audio at all | Lists the film on **English dubs**, to **Replace** (blocklists the release in Radarr, deletes the file and searches again), **Listen again** or **Keep** |
| *Optional:* audio heard clearly in another language, or subtitles labelled in another language | Removes them, once English audio is confirmed |

- **Replace automatically** is off by default; with it on, films with no English audio are replaced without asking. Dry run is respected.
- **Remove audio and subtitles in other languages** is off by default. The file is rewritten as a copy with `mkvmerge` (never re-encoded), checked (same length, chapters and video, and the expected tracks), and only then moved over the original. MKV only. Tracks it couldn't make out are kept.
- A film's chapter job waits briefly for its dub check, so its transcript comes from the right track the first time.

For Radarr to grab dubs in the first place, its quality profile's language must allow them (*Any*, with a custom format scoring non-English releases down), not *Original*.

---

## Problems: burned-in subtitles and broken files

On by default; settings in Settings → Enhance → File problems. A background check goes through every film and episode a few files at a time (films first), stands aside while someone is watching, and looks again only when a file is replaced. It reads a few dozen frames and a few seconds of each file and changes nothing.

| Looks for | How |
|---|---|
| **Subtitles burned into the picture** (films) | Frames from moments of speech and of silence, chosen from the saved transcript (or evenly spaced without one). Subtitles are a line or two of bright, dark-outlined text centred near the bottom, on most speech frames and few silent ones. Title cards, signs and corner captions are too tall, off-centre or don't follow the talking. |
| **Shorter than listed** | The file runs under three quarters of the runtime Radarr or Sonarr gives. |
| **Cut off** | The picture is there halfway through but missing at 80% and 90% of the file's length: an incomplete download. A soundtrack or subtitle track running a little past the picture doesn't count. |
| **Goes quiet** (films) | Nobody speaks after the first 60% of a film that has a transcript. |
| **Decoding errors** | Errors from the video or audio decoder at two or more of six points through the file. |

Problem files are listed on **Enhance → Problems** and on Overview, to **Replace** (blocklist the release in Radarr or Sonarr, delete the file, search again), **Check again** or **Keep**. **Replace problem files automatically** is off by default; dry run is respected.

---

## Search what was said

**Search** (the box in the header, or Library → Search) (and `GET /api/v1/transcripts/search?q=…`) finds an episode or film by a line or what's talked about in it, across every transcript Smart Chapters has made. An exact phrase ranks first, then short windows holding most of the words; each result shows the moment's lines and timestamp, linked to the episode. Words are matched by rough stem ("frying" finds "fry"). Household accounts can use it too.

---

## Collections and suggestions

**Library → Collections** (administrators) lists the film series you have part of, from Radarr's collections (TMDB's film series), nearly complete ones first: "You have 5 of 6". The missing films are ticked ready to add; films not yet released are listed as coming. **Add ticked** adds them to Radarr with the quality profile and root folder of the series' films already in the library, and starts a search. **Follow series** turns on Radarr's own collection monitoring, so the series' missing films and later ones are added as they're released. **Not interested** hides a series for good.

Below, **Suggested for you** takes Radarr's recommendations (films TMDB links to ones in the library), drops anything already there, unreleased or hidden, and ranks the rest by rating and by how well their genres match the films the household has favourited in Jellyfin. Tick and add, or hide a film with **Not for us**.

---

## Fun facts

**Library → Fun facts** (everyone, household accounts included) ranks films and shows by what's said in them, from the Smart Chapters transcripts: most swearing per hour, the f-bomb champions, squeaky-clean talkative films and the shows that swear least (handy for picking something for the kids), the most kill-and-blood talk, the most "help!" and "run!", the chattiest and quietest, the most "I love you"s, pleases and thank-yous, and questions. **Catchphrases** finds the phrase each show says in more of its episodes than other shows do. Each transcript is counted once in the background, with no GPU, and the page grows as more files are transcribed.

---

## Sharing the machine with playback

**Pause all work** (Enhance → Progress) is for when you need the machine for something else: for an hour, four hours or until you resume. Running jobs are stopped and put back in the queue (they start over on resume), nothing new starts (chapters, subtitles, skip-marker, dub and file checks, transcription), and the language model is unloaded from the graphics card. A banner on every page says until when, with **Resume now**.

Chapter work is heavy (scene detection, transcription, a language model), and the same server usually streams to the household. Curatarr adapts how many chapter jobs run, every worker cycle:

- It measures CPU use, GPU use (counted only while Jellyfin is transcoding), how many people are watching, and how many of those streams are transcoded.
- With nobody watching it aims to keep the machine under 85%. Each viewer lowers that by 15 points and each transcode by another 15, never below 35%.
- Below the target it adds a job, one a minute. Above it, nothing new starts and running jobs finish.
- With **Make room for people watching** on, if someone is watching and the machine stays well over the target for 30 seconds, running jobs are suspended for at least two minutes and resume once there's room.

**Chapter jobs at once** (1–8) is only the ceiling. The Activity card on Enhance → Progress says what was measured and decided, for example *Up to 3 chapter jobs right now · CPU 42%, 1 watching (aiming under 70%)*.
