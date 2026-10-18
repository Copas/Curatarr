# Changelog

## Unreleased

- **Library → Free up space**: mark films and shows Leaving Soon by hand, sorted by space freed, untouched longest, oldest or most recently watched, and filtered to watched or unwatched titles. Nobody plays it in the notice period and it goes (shows are trimmed to their always-keep episodes).
- **Remove this film** on a film's title page: marks it Leaving Soon on request, with the poster badge and notice period, then deletes it through Radarr unless someone plays it or you choose Keep.
- **Fixed: background jobs failing with "database is locked"** on SQLite. Smart Chapters and skip-marker checks on TV episodes left a change unsaved before reading the rest of the series, which then held the database's write lock through transcription and every Ollama call, often for minutes. File checks, dub checks and the web app's saves failed in the meantime. Changes are now saved before any slow step.
- **Fixed: "Reset to minimum" monitored specials that were never downloaded.** Specials are left alone, so they're kept, but monitoring them had Sonarr fetch them for a show that was meant to stay at a few episodes. Only specials already downloaded are monitored again now.
- **TV-friendly audio no longer forgets a track it added** when the database is briefly busy: the record is retried once, since the file is already rewritten and Radarr then reports the new track.

## 0.2.0rc5 (2026-10-17)

- **TV-friendly audio** (Settings → Enhance, off by default): films and episodes whose main audio is DTS, TrueHD or FLAC get an AC3 copy as the default track, beside the original, so Rokus and TVs that can't play those play the film directly instead of converting it (which can leave the Roku app restarting at one spot). Only that track is encoded; the file is replaced only after the copy checks out.
- **Automatic fixes** (Settings → Automatic, all off by default): replace problem files, films with no English audio and files of another show with the same name, and search once for always-keep episodes that never arrived, without asking. Each is tried once; when it doesn't work (no better copy, or still missing after the search) the item is dropped quietly and Activity says so. Overview shows one line about what was handled instead of a row per item.
- **Needs you only lists what needs you.** Replacements being looked into and searches in progress stay on Enhance → Problems, off Overview.
- **Cleanup listens to the household.** Settings → Household, with a note on each title's page and in Review saying why:
  - **Seerr requests** (optional integration): a requested title is kept until whoever asked has played it, for up to 90 days.
  - **Rewatched titles** (Jellyfin play counts) go to the back of the line, like favourites.
  - **Import-list arrivals** nobody has played get a 7-day grace period and go first. Radarr's lists are recognised directly; Sonarr's by a tag on the list.
  - **Seasonal shelves:** Halloween and Christmas titles (TMDB keywords, or watched mostly in that season before) are kept through their season and the two weeks before; optionally, ones Curatarr removed are added back to Radarr before their season.
- **Shrink instead of delete.** Films of 4 GB or more can be swapped for a copy half the size or smaller, from Review or the film's page. Curatarr checks such a copy exists first and changes nothing otherwise.
- **Disk forecast** on Overview: how fast each disk is filling, roughly when it reaches the low free-space mark, and what cleanup would take first then.

- **Sonarr or Radarr is enough.** A TV-only or film-only setup no longer needs the other: setup is complete with Jellyfin and either one, discovery skips the one that isn't set up, and Overview doesn't warn about it. One that's set up but stops answering still stops discovery, so nothing it manages is unmatched by mistake.
- **A new README** leading with what Curatarr needs and what it does, with new screenshots.
- **Replace checks there's something better first.** Replace (English dubs, Problems, wrong shows) used to blocklist the release, delete the file and then search, which could leave no copy at all when nothing better existed. Now the request is queued and the worker asks Radarr or Sonarr for releases first; the file is replaced only if one would be taken once the current file is gone (not the same release, and not one more part of a split film). Automatic replacement works the same way.
- **Buttons show what they started.** After **Replace** the row says "Looking for a better copy…" with **Cancel**; if nothing better exists it says so, with the date checked, and **OK** drops it (Replace isn't offered again); a replaced file leaves the list. **Search once** shows "Searching…" and later "Still missing after searching", **Request now** on a show's page shows "Requested …, waiting for N episodes" until they arrive, and **Check skip markers** and a scan waiting on file discovery show a status until the worker picks them up.
- **Changing a key or token asks first.** Revoking or regenerating the API key, and replacing the webhook token, ask in place before cutting off whatever uses them.
- **Shows whose files are another show with the same name** are flagged on Problems and Overview: most files' names carry a year that isn't the show's, or the audio is in another language with no English track. **Replace** checks the right show's releases exist first, then blocklists these, deletes the files, searches again and has Sonarr ignore releases carrying the wrong year.
- **Always-keep episodes that never arrived** are listed on Problems and Overview, with **Search once** (monitors just those episodes, so nothing else downloads) and **Leave them missing**. Monitoring a show no longer downloads leftover episodes Sonarr still had monitored inside it.
- **Wrong episodes reach Overview.** Episodes whose transcript fits another episode, or whose default audio isn't English, are listed on Overview with their buttons, and the Wrong episodes tab appears (with a count) whenever any are flagged.
- **Series cleanup is called a reset**, not a deletion, in History and on Overview. Reset unwatched no longer offers shows the worker would refuse, and says which always-keep episodes are missing when it skips one.
- **Split films.** Replacing one part of a film split across files ("Part.2", "CD1", "Part.II") has Radarr ignore split releases for that film, so the other part doesn't come straight back.
- **Progress groups failed jobs by why**, each group with its own **Try these again**. Files waiting for their share count as waiting, not failed, and **Try all again** also clears queued failures.
- **Jellyfin links** beside every title name: Overview, Problems, English dubs, search, transcript search, activity and Fun facts.
- **Fun facts are rates.** Every ranking is per hour or per minute, so long-running shows don't win by size. New: **Who gets talked about** (names said most per hour), **Shouty**, **Biggest vocabularies** (distinct words per thousand), **Slow burners** (longest wait for the first word), **Sounds of silence** (longest gap with nobody speaking) and **Non-stop talkers** (longest stretch without a pause). Theme songs, other-language transcripts and generic phrases are left out. **Catchphrases** finds each show's longest phrase that recurs across its episodes, without the show's own name, for every show, ordered by what's watched and then by how often it's said.
- Fixed: Overview's "Needs you" rows could run outside their card, and title pages' episode rows could widen past a phone screen.
- **Fixed: "database is locked" for minutes at a time.** Removing other languages from a film saved nothing before rewriting the whole file, which takes minutes on a network share, so SQLite's write lock was held throughout and the web app, webhooks, the worker and chapter jobs waited and failed. It saves first now. Tests check from a second connection that the database is free while files are rewritten, frames are read and Radarr is asked.
- **A network share that drops no longer fails hundreds of jobs.** When a file can't be found and Radarr or Sonarr still have it (or can't be reached either), the job waits and retries every ten minutes, failing only after a day. Jobs that failed this way before are retried by themselves once their file can be read again, and the dub and file checks skip unreachable files instead of recording them as checked. Progress's Failed list has **Try all again**.
- **Pause all work** (Enhance → Progress) for 1 hour, 4 hours or until resumed, to free the graphics card and processor for something else: running jobs are stopped and requeued, nothing new starts, and Ollama unloads its model. A banner on every page shows the pause with Resume now.
- **A file that's gone on purpose isn't a failure.** When a job can't find its file, Curatarr now asks Radarr or Sonarr first: if they have no file either (Curatarr replaced it for having no English audio, say, or it was deleted), the job closes quietly and the next copy is processed when it arrives; if the file was renamed or upgraded, Curatarr follows it. Failures recorded before this are cleared the same way in the background. Each failed job on Progress now has **Try again**, **Find another copy** (a Radarr or Sonarr search) and **Ignore**, and says when Curatarr itself replaced the file.
- **Fixed: the Progress tab's count of failed jobs didn't match the page.** The badge counted every failed chapter job; the page left out files that couldn't be read at all, and never listed any. Both now count the same files, and Progress lists them with the reason. A file that's shorter than listed because its name marks it as one part of a split release ("Part.2", "CD1") now says so on Problems.
- **Clicks show they registered.** Any button or link shows a spinner and a loading bar runs along the top while the next page loads, and a form can't be sent twice by clicking again.
- **Fixed: pages hanging and "Internal Server Error" while the new background checks ran.** The file check held SQLite's write lock while it read frames (and the dub check while it listened), so web requests and Jellyfin webhooks waited until they timed out. Both now save before the slow part, transcript counting writes in short batches, and the web app serves several requests at once (`--threads 4`; takes effect on install).
- **Library → Fun facts**, for everyone in the household: films and shows ranked by what's said in them (most swearing per hour, f-bomb champions, squeaky-clean films and the cleanest shows, bloodthirsty talk, panic stations, chatterboxes and strong silent types, hopeless romantics, manners and questions), each show's catchphrase, and library-wide totals. Counted once per transcript in the background, without the GPU.
- **Enhance → Problems: burned-in subtitles and broken files.** A background check (on by default, a few files at a time, standing aside during playback) looks at films for subtitles drawn into the picture, which can't be turned off: a line or two of outlined text centred near the bottom while people talk, and not during silences (using the saved transcript, or evenly spaced frames without one). It also looks at films and episodes for files shorter than Radarr or Sonarr lists, pictures that stop well before the end (incomplete downloads), films nobody speaks in after the first part, and decoding errors. Problem files are listed on Problems and Overview with **Replace** (blocklist the release, delete the file, search again through Radarr or Sonarr) and **Keep**; Settings → Enhance → File problems can replace them automatically. Only frames and a few seconds are read; nothing is re-encoded.
- **Library → Collections.** Film series you have part of, nearly complete ones first ("You have 5 of 6"), with the missing films ticked ready to add to Radarr using the series' own quality profile and folder; unreleased films are listed as coming. **Follow series** turns on Radarr's collection monitoring so new films are added when released. Below, **Suggested for you** ranks Radarr's recommendations by rating and by the genres the household favourites in Jellyfin. Anything can be hidden for good with Not interested.
- **Buttons explain what they'll do.** Hover over (or tab to) a button to see a tip; on a phone, press and hold. **Process all…** now counts only the files it would actually add: files that are already queued (with automatic processing they all are) aren't counted again, and when there's nothing left to add the button gives way to an "All N already queued" note.
- **The chapter-job estimate goes by hours of video**, not file count, so a run of long films after short episodes no longer reads as a slowdown.
- **A new interface layout.** The top bar is Overview, Review (with a count), Library, **Enhance**, Activity and **Settings** (formerly Manage), with a search box and a dry-run pill; the full dry-run notice stays on Review and the retention pages. Enhance gathers what was under Manage → Chapters into Progress, Files, Skip markers and English dubs tabs. Settings → Enhance is a set of collapsible feature cards with one-line summaries, switches and a save bar that stays on screen. One search page covers titles and what was said. On phones a bottom tab bar replaces the Menu toggle, with a More page for the rest, and Review cards keep Keep and Delete visible with the rest in a menu.
- **Overview leads with what needs you**: titles leaving soon (with Keep), the review queue, films with no English audio (Replace / Keep), failed chapter jobs, integrations that aren't responding and unfinished setup; and a Library care card with coverage bars and what's running.
- **Each title's page has an Enhancements panel**: per file (episodes grouped by season), its chapters, generated subtitles, the recap and preview Curatarr marked, skip markers it fixed, and its English-audio check.

- **Credits start when the talking stops.** A credits marker people are still talking in isn't credits: Intro Skipper's fingerprint matching can latch onto a recurring music cue and mark minutes of the episode (on one show, the last 7½ minutes of ten episodes, each exactly 447 s). The skip-marker check now finds the last line of dialogue inside each credits marker (ignoring theme lyrics heard in other episodes and a marked preview) and, when there's at least 20 s of it, moves the credits to start after it, or hides the marker when the talking runs to within 10 s of the end. Trims are listed on Review skip markers; Undo restores Intro Skipper's original. Episodes already checked are checked again (only the marker step); press Check skip markers to queue them.

## 0.2.0rc4 (2026-10-15)

- **English dubs.** For films made in another language (from Radarr), and English films whose file carries other languages, Curatarr samples each audio track and has Whisper identify its language (no transcription). It relabels tracks whose language tag is wrong or missing, makes a real English track the default when the default is clearly another language, and lists films with no English audio on a new English dubs page to Replace (blocklist in Radarr, delete, search again), Listen again or Keep. Options: *Replace films with no English audio automatically* and *Remove audio and subtitles in other languages* (an mkvmerge copy, verified before it replaces the file; MKV only), both off by default. A foreign film's chapter job waits briefly for its dub check so it's transcribed from the right track.
- **What Curatarr has done.** Chapter maintenance leads with chapters and subtitles added, recaps and previews marked and bad markers hidden, plus coverage bars splitting each total into what the library already had and what Curatarr added. The library card keeps only file counts.
- The estimated finish for chapter jobs uses the last 24 hours' pace (72 when there's too little to go on) instead of 3, so evenings of viewing and long films don't swing it.
- Fixed demo mode's worker failing every cycle on the movie skip-marker check (the demo Jellyfin had no answer for Intro Skipper's segments).
- Documentation: a new README with screenshots, step-by-step install guides for Linux, Docker and Windows, and docs/FEATURES.md, docs/API.md and docs/DEVELOPMENT.md.

- The Activity card's pause notice says why it paused (what was measured: CPU, GPU, who's watching, the target) and that jobs resume once there's room, rather than the old "continue when playback stops". When not paused, it leads with the current limit ("Up to 2 chapter jobs right now", or "No new chapter jobs right now").

- "Chapter jobs at once" goes up to 8 (was 4) and the field shows its range. With the adaptive limit it's only a ceiling; 4 had been a guard from when the number was fixed and every job meant GPU transcription.

- **Adaptive chapter jobs.** Instead of a fixed number, Curatarr adjusts how many chapter jobs run every worker cycle from what it measures: CPU use, GPU use (only while Jellyfin is transcoding, since direct play doesn't use it and otherwise it's Curatarr's own transcription), how many people are watching, and how many of those streams are transcoded. With nobody watching it aims to keep the machine under 85%, each viewer lowers that by 15 points and each transcode by another 15 (never below 35%). Below the target it adds a job, one a minute; above it, nothing new starts and running jobs finish; if someone is watching and the machine stays well over the target for 30 seconds, running jobs are suspended for at least two minutes and resume once there's room. "Chapter jobs at once" is now the ceiling, and "Make room for people watching" (formerly "Pause chapter work while someone is watching") turns the playback part on or off. The Activity card says what it measured and decided.

- The episode check (does the transcript fit the episode's own summary; is the default audio English) is now **off by default**: Chapter settings → "Check episodes against their summaries". It found real mix-ups, but with vague summaries it also flagged episodes that were fine, and speech recognition mislabelled the language of music-heavy audio. While off, skip-marker checks don't run it, and the list, the To do count, the episode notice and `/api/v1/identity-flags` stay empty.

- Fixed Possible wrong episodes failing (Internal Server Error) after a flag was cleared: a cleared flag can be stored as JSON null, which still passed the "has a flag" filter.
- Chapter maintenance's "Queued or running" counts chapter jobs only (now "Chapter jobs queued or running"); skip-marker checks, subtitle checks and scans aren't included, matching the estimated finish.

- **Check skip markers** no longer hangs and fails with "database is locked". The page used to mark thousands of episodes queued in one long write while the worker was writing too; now it just leaves a request, and the worker queues them on its next cycle in batches of 500, each committed at once.

- Stricter wrong-episode checks. A flag still means the transcript fits another episode's summary 4x better than its own (reliable: the file isn't that episode), but it now records whether *which* episode is certain (the best match well clear of the runner-up); uncertain ones say so. A file only counts as positively confirmed as its own episode when its own summary is the clear best match, not merely when it isn't flagged: Wonder Woman season 1 turned out shifted by one, with each file nearly fitting. Recaps and previews are left out of the scoring, since they're clips of other episodes. Earlier checks are redone (dismissed and audio flags are kept); press Check skip markers to queue them.

- Fixed Chapter maintenance's Activity card failing (Internal Server Error) when a skip-marker check finished between two of its queries.
- The transcript-linking pass no longer holds the database's write lock while it reads files on the network share, which made Jellyfin's webhook time out ("database is locked").

- Search covers every Smart Chapters transcript, not just ones made since transcripts were linked to their files: a background pass (20 files per worker cycle) works out each older file's transcript and links it.
- A wrong-episode flag is no longer dropped when the same file's path is written differently (discovery and Sonarr use different forms); it's compared by file name.

- **Search what was said** (Library → Search what was said, and `GET /api/v1/transcripts/search?q=…` with the read-only API key): find an episode or film by a line or what's talked about in it, across every transcript Smart Chapters has made. An exact phrase ranks first, then windows of three lines holding most of the words; each result shows the moment's lines and timestamp. Household accounts can use it too. Words are matched by rough stem ("frying" finds "fry", "tribes" finds "tribe"). Transcripts are held in memory and refreshed every five minutes; a search over 1,445 transcripts takes milliseconds for distinctive words.

- Wrong-episode flags are found through the file's own part rather than the identity stored on its chapter state, which can point at one discovery has since replaced; such flags were missing from the list and the API.

- Skip-marker checks no longer slow chapter jobs. Checking an episode with no saved transcript means transcribing its opening and ending, which takes the GPU; alongside a chapter backlog it had doubled the time per chapter job (about 5 to 10 minutes) and cut throughput from ~40 files an hour to ~12. While chapter jobs are waiting, only checks that need no transcription run (episodes Smart Chapters has transcribed, plus checks asked for from an episode's page); the rest wait until the backlog is done.

- `GET /api/v1/identity-flags` (read-only, API key): wrong-episode flags with the Sonarr series, the episode the file is filed as and the one it fits, and whether that one's own file is confirmed (so the flagged file is a duplicate) or flagged the other way (the two were swapped). A companion tool can use it to offer the fix in Sonarr. A flag on a file that has since moved or been replaced no longer shows.

- **Make English the default**: an episode flagged because its default audio isn't English, but which has an English track, gets a button that makes that track the default in place (mkvpropedit, no re-encode) and checks it read back. Curatarr's chapters and subtitles from the foreign track are queued to be redone. Transcripts of a track other than the first now have their own cache key, so the new default track isn't served the old track's transcript.

- **Possible wrong episodes.** The skip-marker check also checks that an episode with a full transcript is the episode it's filed as: its transcript is scored against every title and summary in its season from Sonarr (rare words count most), and it's flagged only when another episode fits at least 4x better than its own. It also flags a default audio track that isn't English (from the transcript's language), noting whether an English track exists. Flags show as "Possible wrong episodes" in Chapter maintenance's To do card, on the episode's page, and on a list with what was said beside both summaries, each with Dismiss. On 278 transcribed episodes this flagged 10, all real: 9 misfiled or mislabelled episodes (The Simpsons S14E21 is "Brake My Wife, Please"; Good Eats season 11 file names disagree with Sonarr) and one Japanese default audio track.

- **Movies keep only their credits marker** (Chapter settings, on by default). Intro Skipper had marked 978 movie "recaps" and a few "intros" from opening chapter names like "Chapter 1"; turning on Skip Recap made them show at the start of movies. The worker hides them (25 movies per cycle, outside the job queue so chapter jobs aren't held up), each listed on Review skip markers with Undo. Credits stay, for Skip Credits and Next.

- **Review skip markers** (Manage → Chapter maintenance → Review skip markers): every recap and preview Curatarr marked and every Intro Skipper marker it hid, newest first, filterable, each linked to the episode in Jellyfin with the span and the reason. **Undo** removes a recap or preview (and its "Previously On" chapter) or restores a hidden marker in Intro Skipper; an undone recap or preview isn't marked again unless the episode is checked again from its page. Recaps and previews Intro Skipper had guessed and Curatarr replaced or removed are listed too.

- A job's lease no longer stops renewing after one "database is locked" (SQLite has one writer; skip-marker checks added writes). It tries again on the next beat, logging a one-line warning instead of a traceback.

- Recap ends no longer come from the local model. Tested on episodes not used to tune it (The Last Man on Earth), the model pushed every montage recap about 30 seconds into the new episode, and confirmed its own choice; ending at the last clip quoted from earlier episodes never ran long (13 recaps, 0 into the episode). Recaps with a series closing line are unchanged. A preview end the model picks is capped at 45 seconds after "next time on", so it can't run into a final scene.

- Fixed recap and preview ends running long (Battlestar Galactica S01E03 was marked to 3:06 instead of about 1:05): a line under five words no longer counts as quoted from another episode (common phrases match by chance), the quoted clips must form a run with no gap over 30 seconds, the model may only extend 30 seconds past the last clip, and a line's end is when its words stop (speech recognition had stretched "I'm fine. I'm just..." over two minutes of music).
- Skip-marker checks use saved scene changes only; detecting them for a whole episode took several minutes per check.

- **Skip markers.** The recap check is now a skip-marker check per episode (Chapter maintenance: **Check skip markers**; an episode's page shows what was found and has its own button, which goes ahead of any backlog):
  - **Previews.** A "next time on" preview is found like a recap: announced by "Stay tuned for scenes from our next episode", "Next time on…", "Next week on…", "On the next episode of…" or "Next episode:" in the last six minutes. It ends at the last clip quoted in the next episode, or where the local model says the preview stops (before a voted-out player's final words, say), or at the end of the file. It's marked as Intro Skipper's Preview segment.
  - **Intro Skipper's credits** are hidden (only hidden; Intro Skipper can restore them) when plainly wrong: under 45 s and over a minute before another credits marker (an 11-second music match under Silo's finale, real credits two minutes later); in the first quarter of the runtime; under a minute with minutes of dialogue after it; or mostly dialogue (not theme lyrics) with other credits after it. Each episode lists what was hidden and why.
  - **Intro Skipper's intros** taken from chapter names or an old analysis are hidden when they're mostly dialogue no other episode has (theme lyrics repeat every week; dialogue doesn't), like IT: Welcome to Derry S01E02's intro, 90 seconds late because the release's "Intro" chapter was.
  - Recap and preview detection use only phrases a sample of 1,095 transcripts showed to be reliable ("Previously on…", "Last week on…", "Next time on…"); bare "previously", "next week" or "last time" turn up in ordinary dialogue. An end the local model picks is only used if the model also agrees the span is a recap or preview.
  - **Guessed recaps on episodes without one are hidden.** When an episode has no "previously on" (or its series never has one), a recap Intro Skipper guessed is a cold open, so it's hidden.
  - Transcription stays minimal: saved transcripts are reused, otherwise only the opening, the last six minutes, or the minute or two around a suspect marker are transcribed.

- **Find recaps** only shows when some unchecked episodes aren't already queued; it stayed up the whole time a check was running, as if something needed attention. The "Episodes to check for recaps" count says how many are queued.

- Recap checks run back to back in a single job slot, like scans. Queued ahead of chapter jobs, a "Find recaps" backlog had taken every slot and stopped chapter writing, which also pushed the estimated finish further out; and since most checks finish in milliseconds (a series without recaps is skipped), one-per-cycle scheduling left the slots idle and the card saying "Nothing running right now". The Activity card now shows the latest recap check and how many are left.

- Restarting the worker (every install) no longer stalls chapter jobs for up to ten minutes: jobs the previous worker left running are requeued at startup instead of waiting for their claims to expire.

- **Recaps.** Smart Chapters finds an episode's "previously on" recap from its transcript and marks it twice: as a first chapter named "Previously On", and as a recap segment in Intro Skipper (when installed), so Jellyfin's Skip Recap button skips exactly the recap. A recap is only marked when the episode says "Previously on…" or "Last time on…" in its first minute, and its end must be clear:
  - Narrated recaps end with the same line every week ("…12 are left. Who will be voted out tonight?"). Curatarr learns that line from the series' other episodes (a whole sentence said once, early, at about the same point each week).
  - Otherwise the recap is anchored on the last clip quoted from the previous episodes (ignoring their "next time on" previews), and the local model picks the narrator's closing line among the few after it.
  - When neither applies nothing is marked, since a wrong recap skips part of the episode.

  Curatarr's recap replaces any recap Intro Skipper guessed for that episode (usually a cold open); it's added as a user segment, which Intro Skipper's analysis leaves alone. Chapter maintenance has **Find recaps** for episodes not yet checked: saved transcripts are reused, and episodes without one (for example ones that came with their own chapters) get only their first five minutes transcribed. A series whose first four checked episodes never open with a recap is skipped. New episodes are checked automatically after their other jobs, and once a series' closing line is learned, episodes that missed it are checked again. Episodes with their own chapters keep them; only Curatarr's chapters get "Previously On" added.

- Fixed: the chapter-jobs progress bar stayed empty. Its width was an inline style, which the page's Content-Security-Policy blocks; it's now a native `<progress>` styled for the theme.

- Fixed: queued subtitle jobs waiting on an import showed "Waiting until …: Show · S01E01 —" with no reason; the card read the chapter error instead of the subtitle one. Retries after a failure now say "Retrying".

- Chapter maintenance splits its numbers into **To do** (needing chapters or subtitles, queued, not scanned, redo with Smart Chapters, failed — highlighted, with a check mark at zero, and the action buttons alongside) and **Your library** (files, scanned, already had chapters or subtitles, added by Curatarr, too short, not MKV). The sentence repeating some of those counts is gone.

- Chapter maintenance's Activity card shows a progress bar and estimated finish for queued chapter jobs: how many are done since the oldest waiting job was queued, the recent pace (files an hour over the last 3 hours, or 24 when that's too few), and roughly when the queue will be empty. Quick subtitle checks and scans aren't counted. It updates live with the rest of the card.

- Smart Chapters no longer gives up on episodes with no scene changes or speech pauses to choose from (common in older shows with a continuous score). Those chapters are evenly spaced and still get real names from the transcript, instead of falling back to "Chapter 1, 2, 3". A model that refuses every boundary it's offered still falls back.

- Scene detection retries with software decoding when hardware decoding fails, which happened when Ollama's model filled the GPU ("Scene detection failed: Conversion failed!").

- Smart Chapters no longer falls back to Navigation chapters when faster-whisper isn't installed. That's a setup problem, not something about the file, so jobs in Smart libraries now wait, and Chapter maintenance says "Smart Chapters is stopped" with the reinstall command. (A redeploy without `EXTRAS` had quietly written Navigation chapters to hundreds of files.) Files with a saved transcript still get Smart Chapters.

- `scripts/install.sh` remembers its optional features in `$PREFIX/extras` and, the first time, detects them from the running install, so upgrading without repeating `EXTRAS` keeps Smart Chapters transcription. It says which features it's installing. Pass `EXTRAS=` (empty) to drop them.

- Chapter maintenance's stat cards were nine boxes in a fixed 4-column grid, leaving the last row mostly empty. Replaced with a single compact card that packs the numbers tightly regardless of how many there are.

- Chapter maintenance now shows "Needing subtitles" alongside "Needing chapters" — files never checked for subtitles, or whose check failed, weren't counted anywhere on the page before.

- Chapter maintenance's Activity card updates itself every 5 seconds instead of needing a manual refresh, and no longer says "Nothing running right now" while it's actually racing through subtitle existence checks (each finishes in under a second, too fast for the old "running" indicator to catch) — it now shows the file just checked, and a "Last N subtitle checks" list. Opening "Last N finished" or "Last N subtitle checks" stays open across refreshes instead of closing on the next update.

- Large counts on Chapter maintenance (files needing chapters, queued jobs, and the rest) now show thousands separators (e.g. "2,857") instead of running digits together.

- A read-only API key (Manage → Integrations → API key) lets other tools, such as a home dashboard, read `/api/v1/` status and history without signing in. It is shown once and stored only as a hash. `/api/v1/history` now includes each action's title, library, Jellyfin item ID, and links to Curatarr's pages, and filters by action type with `type=` and `exclude=`.

- Times are shown in your own time zone (from the browser) instead of UTC, everywhere: chapter activity, Overview, History, Review, Settings, and title pages. Hover a time to see its zone. Without JavaScript the UTC time still shows.

- The worker no longer exits when the database stays locked for more than 30 seconds (it did six times one night, abandoning the chapter jobs it was running). It logs a warning and tries the cycle again 15 seconds later.

- Smart Chapters no longer fall back to Navigation chapters when the GPU is busy. Transcription and Ollama now take turns on the GPU: before a transcription starts, Curatarr waits for running Ollama requests and asks Ollama to unload its model. A failed Ollama request is retried, and a job whose requests still fail waits 15 minutes and tries again; only the third attempt settles for Navigation chapters. Chapter maintenance has **Redo with Smart Chapters** for files that got Navigation chapters in a Smart library, including files waiting only for their subtitle job. Queued jobs now run quickest first: subtitles (which reuse the saved transcript), then redos, then automatic jobs, then the backlog you queued.

- Jellyfin sends a playback-progress notification every few seconds while something is playing. These were stored and processed as favorite changes — about 4,000 rows a day on a single viewer — and are now acknowledged and dropped without showing as a webhook problem. Actual favorite toggles are unaffected.

- Every title Curatarr shows links to the same item in Jellyfin: an "Open in Jellyfin" button on title and chapter pages, and a small Jellyfin link beside titles in Library, Review, Shows by size, Overview, History, cleanup previews, and Chapter maintenance (episodes link to the episode). When Curatarr reaches Jellyfin over localhost, links use the host the page was opened on, so they work from other devices.

- Optional generated English subtitles from the Smart Chapters transcript (Chapter settings → Subtitles, per library). They are written as `<video>.en.Curatarr.srt` only when a file has no English subtitles inside it or beside it, never over another file, and removable from the file's page. Chapter maintenance can queue every file that is missing them. Transcripts now survive adding chapters to a file, and one transcription runs at a time, so jobs no longer fall back to slow CPU transcription when the GPU is full (migration f1b6d2e8c047).

- Chapter generation pauses while anything is playing in Jellyfin (on by default; Chapter settings → This server). Running jobs are suspended where they are and resume when playback stops; no new jobs start. Chapter maintenance shows the pause, what each job is working on, what is queued, and the last files finished.

- Optional chapter generation (Manage → Chapters). Long movies and episodes without chapters get them after import: Navigation Chapters (about every 7 minutes, aligned to scene changes) or Smart Chapters (local faster-whisper transcript plus an Ollama model choose the boundaries and name them), with fixed intervals as the last fallback. MKV files are edited in place with mkvpropedit, with no re-encoding, then verified, and only that item is refreshed in Jellyfin. Existing chapters are preserved, and Curatarr removes or regenerates only chapters it wrote. Enabling it never sweeps the existing library; Chapter maintenance scans, shows counts, and processes what you confirm. Each file has a preview page. Install with `EXTRAS=chapters-gpu` (or `chapters`) for Smart Chapters. Discovery now also records each file's path from Sonarr/Radarr (migration e7c3a9f15d42).

- The light/dark theme button is now a sun icon in dark theme and a moon icon in light theme, with a "Switch to … theme" tooltip and label.

- Shows by size has "Reset all unwatched shows…" (administrators only). Its confirmation page lists every show nobody has ever started that would be reset, with the files and space removed, and the unwatched shows it skips and why: daily shows, Never Purge, a cleanup already running, already at the minimum, and shows added in the last 30 days (adjustable). It warns separately when any affected library is not in dry run. Confirming requires typing the exact phrase (e.g. "reset 143 shows"), and a list that changed since the page was opened is refused. Each show is then reset by the worker with the usual checks.

## 0.2.0rc3 (2026-10-09)

- Document Docker Desktop installation and upgrades on Windows. Keep shell entrypoints in LF format when cloned on Windows.
- Accept Windows drive and UNC paths in free-space rules, and match Sonarr/Radarr disk records across slash and letter-case differences. An unreported disk still cannot trigger free-space cleanup.

## 0.2.0rc2 (2026-10-09)

- Fix native upgrades failing at database migration: the installer now makes the staged release directory accessible to the `curatarr` service user. The failed `rc1` upgrade rolled back to the previous running version.

## 0.2.0rc1 (2026-10-09)

- Refreshed the interface with grouped navigation, light and dark themes, a browsable title library, compact review cards, clearer policy sections, and a dedicated Operations page. Deletion approval now has a confirmation page. Administrators can allowlist non-admin Jellyfin household users for browsing and Keep/Snooze/Never Purge decisions; all admin routes and actions enforce the role server-side.

- New "Shows by size" page (navigation bar): every TV show on disk, largest first, with what Reset to minimum would free, last watched and by whom, a library filter, totals, and a Reset… button that opens the confirmation page. Daily shows are flagged on this page and on the confirmation page, because a reset keeps none of their episodes and Curatarr never refills daily shows.

- Title page actions are grouped in an Actions panel, one row per action with its description beside the button, instead of help text trailing after the button. Reset to minimum opens a confirmation page that lists what is deleted and kept, with Cancel and a red confirm button.

- Reset to minimum and the Review queue's Delete no longer run the deletions inside the web request: they approve the cleanup and the worker carries it out within a cycle (about 15 seconds). A long show (six seasons of I Love Lucy) outlasted gunicorn's 30-second request timeout, the page showed Internal Server Error, and the reset stopped after one file. An interrupted deletion is still reconciled against Sonarr and marked blocked rather than repeated.

- Works with Jellyfin 12.2: looking up one Jellyfin item now uses the item list (`/Items?Ids=`) because 12.2 rejects `GET /Items/{id}` from an API key with no user ("Guid can't be empty"). Before this fix every deletion check failed with "External integration cannot be verified", and poster badge updates could not read items.

- Reset to minimum now works on any show, including ones Sonarr has unmonitored (for example by a nightly job that unmonitors ended, complete shows) and ones with an always-keep episode missing. A show Curatarr has reset or cleaned up is refilled when someone watches it even if Sonarr has it unmonitored: the request monitors the series in Sonarr first, since Sonarr ignores unmonitored series. Other unmonitored shows are still left alone. The reset button on the title page is now a clearly separate red button.

- Title pages for TV shows have "Reset to minimum": trims the show to its always-keep episodes as if nobody had watched it. It runs through the normal approved-deletion path with every check except the grace period and recent viewing, then unmonitors all seasons in Sonarr (keeping the always-keep episodes monitored) and records the reset so earlier viewing no longer requests seasons. Watching again after a reset works as usual. Dry run is respected. Requires `db upgrade` (migration `d2f6a8c41e93` adds `viewing_reset_at`).

- A show whose latest season viewers have watched (by the acquisition threshold) gets its next season whenever Sonarr lists it, no matter how long ago that viewing was; before, the next season was only requested within the recent-viewing window (90 days by default), so a season premiering a year later was missed. A show not continued although the next season was already out is still treated as abandoned.
- While viewers are on a season that is still airing (its latest episode aired in the last 30 days), Curatarr makes sure Sonarr monitors that season even if every episode so far is downloaded, so new episodes download when they air. Queued once per season.

- Withdrew the `curatarr-pilot` Sonarr tag rule added earlier the same day: shows added by a list get Curatarr's configured always-keep fill like any other show. Migration `a1e5d9c3b7f2` drops the `arr_tags` column again (`db upgrade`).

- When Curatarr requests a whole season (the rest of the season being watched, or the next season), it now also monitors that season in Sonarr. Sonarr monitors an episode it learns about later only when the season is monitored, so episodes announced after the request (common for a season still airing) were added unmonitored and never downloaded. Partial requests, such as the first episodes of Season 1, still leave the season alone.

- Discovery and watch-history reconciliation no longer miss movies that belong to a Jellyfin collection. Jellyfin collapsed collection members into the collection in library listings, which hid 849 of 979 movies in the owner's Movies library (and 92 of 199 Classic Movies) from tracking, sizes, and cleanup.
- Reloading Settings after generating a webhook token no longer replaces the token.

- The stored policy format is versioned (migration `c4e8a1f07d36`); a database with settings from a newer version is refused with a clear message instead of being reinterpreted.

- Cleanup preview (Retention → "Preview what free-space cleanup would pick"): pretend free space is at a given level and see, per disk, the titles that would be picked in order with sizes and a running total, using the same rules and live queue as the worker. Nothing is changed.

- Settings shows the last webhook delivery (what it was, who, and whether it matched a title), the total received, and the last rejected or ignored delivery with the reason. Notification types Curatarr does not use are acknowledged and recorded as ignored instead of rejected.

- Title pages have "Request now", which applies the always-keep and season-ahead rules to one show immediately and sends the result to Sonarr, plus Keep/Snooze/Never Purge actions and an admin deletion confirmation link for a title in Review or Leaving Soon.

- Jellyfin libraries that are not TV or movies (e.g. collections, mixed libraries) are listed on Overview as not managed instead of showing as empty 0 TB libraries, and are left out of rule scopes, the dry-run banner, and library counts.

- Download requests explain themselves, e.g. "Alex finished S01E01 of XYZ, so Curatarr is searching for Season 2." Overview lists recent requests under "Downloads requested", with their status.

- Every reconciliation now requests missing always-keep episodes (new setting "Download always-keep episodes that are missing", on by default) and catches up the next season for shows watched in the last 90 days, skipping shows Sonarr has unmonitored and daily shows, with at most 5 new searches per run. Requires `db upgrade` (adds `arr_monitored` and `series_type`).

- Size limits are entered in MB, GB, or TB (TB by default) instead of bytes, and every size in the interface uses the same units (binary, matching Sonarr, Radarr, Jellyfin, and df).

- TV cleanup unmonitors each trimmed episode in Sonarr just before deleting its file. Previously the episodes stayed monitored, so Sonarr's missing-episode search would have downloaded them straight back (Sonarr's own "unmonitor deleted episodes" does not apply to API deletions and is off here).

- Library-size limits, free-space enforcement, and dry run can be set as global defaults. The dry-run banner states exactly which libraries are in dry run and which can delete, and explains that dry run rehearses everything but never asks Sonarr/Radarr to delete.

- Libraries on the same disk share one free-space selection, so four libraries on one NAS no longer each pick enough titles to cover the whole shortfall.

- Disk-pressure checks no longer fall back to the `/` record for other paths. Sonarr/Radarr omit network mounts, so a NAS library was being measured against the local drive. When the arr apps report no matching disk, free space is measured directly if the path is visible to Curatarr.

- Discovery records each file's acquisition time from Sonarr/Radarr `dateAdded` instead of the discovery time, which had made an entire existing library look newly acquired. Existing installs are corrected at the next discovery.

- Cleanup is driven by free space. Unwatched titles are no longer cleanup candidates on their own (new per-library switch `inactivity_cleanup`, off by default). Low free space selects only enough to return to the low threshold, with a Leaving Soon notice (14 days by default) before removal. Critical free space removes without a notice. Dry run still gates all removal.

- The grouped top navigation highlights the section being viewed, including sub-pages under Library, Activity, and Manage.
- The global rules page shows real values instead of "Built-in default" placeholders and saves only values that differ from the built-in defaults. Blank options remain only where they mean something: the media-type-specific purge strategy and rule review modes that follow the main review mode. Library pages read "Inherit (value)".
- Policy choices use plain labels: "Always keep for each show" offers "First N episodes of Season 1", "All of Season 1", and "Entire series (never trimmed)"; review modes, expiry, strategies, and Yes/No read as words.
- Rule pages name what a blank field resolves to: "Built-in default (value)" on global defaults, which have nothing to inherit from, and "Inherit global default (value)" or "Inherit built-in default (value)" on a library.
- Acquisition ignores episodes that exist only in Jellyfin. Split or differently numbered episodes Sonarr does not track looked missing and triggered season searches that re-downloaded seasons already present (seen live with Tires S2).
- Acquisition actions record which episodes were actually switched to monitored (`newly_monitored`). Episodes Sonarr already monitored are no longer implied as changes.
- Overview shows a live reconciliation banner (queued, running with elapsed time, failed with the reason, interrupted, or last finished with counts) and refreshes itself while work is queued or running. Health reports the worker as busy during a long reconciliation instead of offline.
- Discover and Reconcile now queue work for the background worker instead of running inside the web request. A real library's discovery (about 45 seconds) exceeded the web worker's 30-second timeout and returned Internal Server Error. Overview shows reconciliation state and the last finish time.
- Jellyfin Webhook plugin support: completion via PlaybackStop/PlayedToCompletion, marking watched and favorites via UserDataSaved, string values from templates, dashed GUIDs, and bodies not labelled as JSON. Settings shows step-by-step plugin setup with a ready-made template.
- Jellyfin API keys are sent in the `Authorization: MediaBrowser ... Token="..."` header. Jellyfin 12.1 rejects the bare `X-Emby-Token` header with 401, so saving a Jellyfin key failed with "could not be reached".
- Worker: heartbeat recorded every cycle, reconciliation waits for API keys during first-run setup, failed reconciliation retries after 5 minutes instead of every 15 seconds, and integrations without a key report "unconfigured".
- Native installer (`scripts/install.sh`, `scripts/uninstall.sh`) and `docs/INSTALL.md`. Installed copies keep code, configuration, and data outside the source checkout.
- Packaging fix: migrations moved into the package (`curatarr/migrations`) and templates, static files, and migrations are declared as package data, so installed copies run without the source tree. The Docker image now runs the installed package as a non-root user.
- Repository hygiene test for publishable tracked files.
- Review queue shows library, media type, proposed action, last watcher, acquisition date, and meaningful-watch state.
- Sign-in with Jellyfin accounts. Administrator access is available by default, while non-admin household users require an explicit allowlist entry. `CURATARR_ALLOW_UNAUTHENTICATED=true` remains for deployments behind an authenticating proxy. New optional `CURATARR_JELLYFIN_URL`.
- First-run setup checklist; Overview shows scheduled deletions and recent cleanup.
- Acquisition and Retention rule pages with global defaults and per-library scopes. Policy saves now merge with stored values instead of replacing them.
- Structured JSON operational logs, `CURATARR_LOG_LEVEL`, durable metric counters, `/api/v1/metrics`, and a dedicated Operations page. Requires `flask --app curatarr db upgrade` (adds `metric_counters`).
- Tests keep instance data in temporary directories.

## 0.1.0 (2026-10-04)

First tagged development release. Validated against demo and mocked integrations only; see IMPLEMENTATION_STATUS.md before connecting real services.

- Initial Flask application, database migration, integration clients, event ingestion, policy engine, review queue, guarded dry-run deletion, and worker.
- Per-rule automatic review modes for inactivity and quota cleanup.
- Quota cleanup continues to the low-water target instead of stopping at high water.
- Late playback rescues expiring Leaving Soon notices; deferred approvals are retried by the worker.
- Demo Sonarr adapter serves episode inventory so demo TV deletions are fully validated.
